"""Ordinary queue, dependency, and resource regressions using offline transports."""
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from llm_verifier import pipeline, scheduler, ready_scheduler
from llm_verifier.config import normalize
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.provider import HttpTransport
from tests.support import completion, request_parts, reseal
from tests.test_committees import committee, report
from tests.test_escalation import PROFILE, execute, verdict
from tests.test_regions import plan_for, region_report
from tests.test_scheduler import providers, Callback


class ReadySchedulerTests(unittest.TestCase):
    def test_optional_cap_validation_and_absent_normalization(self):
        with committee() as (root, _, _):
            raw = json.loads((root/'verifier.json').read_text())
            self.assertNotIn('max_concurrent_calls', normalize(raw)['limits'])
            for cap in (1, 2, 4, 64):
                raw.setdefault('limits', {})['max_concurrent_calls'] = cap
                self.assertEqual(normalize(raw)['limits']['max_concurrent_calls'], cap)
            for cap in (0, -1, True, 1.5, None, 65, '4'):
                raw['limits']['max_concurrent_calls'] = cap
                with self.subTest(cap=cap), self.assertRaises(VerifierError):
                    normalize(raw)
            raw['limits'].pop('max_concurrent_calls')
            raw.pop('judging')
            self.assertNotIn('max_concurrent_calls', normalize(raw)['limits'])

    def test_default_primary_planner_proof_then_ten_ready_calls(self):
        with committee(judging=PROFILE) as (root, _, intake):
            # Make eight source regions while preserving each file's coverage.
            path = next(p for p in intake['data']['files'] if p != 'submission.json')
            (root/'submission'/path).write_text('\n'.join('line '+str(i) for i in range(14))+'\n')
            from llm_verifier.config import load_config
            from llm_verifier.artifacts import intake_snapshot
            effective = load_config(root, 'verifier.json', 'submission')
            intake = intake_snapshot(effective, root/'submission')
            initial = execute(intake)
            plan = plan_for(intake)
            while len(plan['regions']) < 8:
                region = next(r for r in plan['regions'] if r['end_line'] > r['start_line'])
                end = region['end_line']; region['end_line'] = region['start_line']
                at = plan['regions'].index(region)
                plan['regions'].insert(at+1, {**region, 'start_line':region['end_line']+1, 'end_line':end})
            for i, r in enumerate(plan['regions']): r['id'] = 'r'+str(i)
            initial_ready = threading.Barrier(3, timeout=8)
            all_ready = threading.Barrier(10, timeout=8)
            lock = threading.Lock(); active = peak = 0; calls = []
            def transport(body, *_):
                nonlocal active, peak
                _, member, context = request_parts(body)
                stage = member['stage']; primary = member['id'] == 'reviewer'
                with lock:
                    active += 1; peak = max(peak, active); calls.append(member['id'])
                try:
                    if primary or stage in ('planning', 'review'):
                        initial_ready.wait()
                    if primary or stage in ('review', 'region'):
                        all_ready.wait()
                    if primary: value = verdict(root)
                    elif stage == 'planning': value = plan
                    elif stage == 'region': value = region_report(context['region'])
                    elif stage == 'adjudication': value = {'decision': verdict(root), 'resolutions': []}
                    else: value = report()
                    return completion(value)
                finally:
                    with lock: active -= 1
            with patch.dict('os.environ', {'OPENAI_API_KEY':'offline'}), patch.object(HttpTransport, 'request', side_effect=transport):
                first = pipeline.review(intake, initial)
            self.assertEqual(peak, 10)
            self.assertEqual(len(calls), 12)
            self.assertEqual([r['member_id'] for r in first['data']['escalation']['review']['reviews'][:2]], ['proof','planner'])
            pipeline.dossier(intake, initial, first)

    def test_caps_refill_without_waiting_for_straggler(self):
        spec = {'profile':'builtin:math-committee-v1', 'add_members':[
            {'id':n,'stage':'review','persona':'builtin:proof-reviewer-v1'} for n in ('extra1','extra2')]}
        for cap in (1,2,4):
            with self.subTest(cap=cap), committee(judging=spec, limits={'max_concurrent_calls':cap}) as (root,effective,intake):
                child=effective['judging']['committee']; initial=execute(intake)
                names=[m['id'] for m in child['members'] if m['stage']=='review']
                first=threading.Event(); release=threading.Event(); last=threading.Event()
                rendezvous=threading.Barrier(cap,timeout=6)
                lock=threading.Lock(); active=peak=0
                def visit(i):
                    def callback():
                        nonlocal active,peak
                        with lock: active+=1;peak=max(peak,active)
                        try:
                            if i<cap:rendezvous.wait()
                            if i==0:
                                first.set();self.assertTrue(release.wait(8))
                            if i==len(names)-1:last.set()
                        finally:
                            with lock:active-=1
                    return callback
                supplied=providers(child,intake,verdict(root),{n:visit(i) for i,n in enumerate(names)})
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(pipeline.review,intake,initial,providers=supplied)
                    try:
                        self.assertTrue(first.wait(6))
                        if cap==1:self.assertFalse(last.wait(.05))
                        else:self.assertTrue(last.wait(6),'released slots did not refill past straggler')
                    finally:release.set()
                    value=future.result(10)
                self.assertEqual(peak,cap)
                self.assertEqual([r['member_id'] for r in value['data']['reviews'][:-1]],names)
                pipeline.dossier(intake,initial,value)

    def test_queued_expiry_does_not_reserve_another_attempt(self):
        with committee(limits={'max_concurrent_calls':1,'total_review_seconds':10}) as (root,effective,intake):
            child=effective['judging']['committee'];initial=execute(intake);now=[0.0];calls=[]
            def expire():calls.append('proof');now[0]=10.0
            supplied=providers(child,intake,verdict(root),{'proof':expire})
            args=(child,effective['config'],intake['data']['manifest'],pipeline.evidence_refs(intake,initial),0,False,
                  pipeline._committee_material(intake,initial,None,None),{})
            with patch('time.monotonic',side_effect=lambda:now[0]),self.assertRaises(InfrastructureError) as caught:
                scheduler.run(*args,providers=supplied)
            state=caught.exception.review_state
            self.assertEqual(state['provenance']['attempts'],1)
            self.assertEqual(state['provenance']['allotted_completion_tokens'],8192)
            self.assertEqual([r['member_id'] for r in state['reviews']],['proof'])
            scheduler.verify(state,*args,True,failed=True)

    def test_planner_failure_drains_independent_proof(self):
        with committee(judging=PROFILE) as (root,effective,intake):
            initial=execute(intake);profile=effective['judging']['committee']
            supplied={n:providers(profile[n],intake,verdict(root)) for n in ('primary','escalation')}
            entered=threading.Barrier(2,timeout=5)
            supplied['escalation']['proof']=Callback(profile['escalation']['members'][1]['provider'],report(),entered.wait)
            def fail(*_):entered.wait();raise InfrastructureError('provider_timeout')
            supplied['escalation']['planner'].review=fail
            result=pipeline.review(intake,initial,providers=supplied)
            state=result['data']['escalation']['failure']['state']
            self.assertEqual([r['member_id'] for r in state['reviews']],['proof'])
            self.assertEqual(state['provenance']['attempts'],2)
            pipeline.dossier(intake,initial,result)

    def test_replay_enforces_region_and_adjudication_dependencies(self):
        with committee(judging={'profile':'builtin:math-decomposed-v1'}) as (root,effective,intake):
            initial=execute(intake);child=effective['judging']['committee']
            value=pipeline.review(intake,initial,providers=providers(child,intake,verdict(root)))
            for group_index in (2,3):
                bad=copy.deepcopy(value)
                groups=bad['data']['execution']['groups']
                groups[group_index]['queued_seconds']=0
                with self.subTest(group=group_index),self.assertRaises(VerifierError):
                    pipeline.verify_initial_review(intake,initial,reseal(bad))

    def test_original_four_wave_verifier_retains_its_version_rules(self):
        with committee() as (root,effective,intake):
            child=effective['judging']['committee'];initial=execute(intake)
            args=(child,effective['config'],intake['data']['manifest'],pipeline.evidence_refs(intake,initial),0,False,
                  pipeline._committee_material(intake,initial,None,None),{})
            value,_=scheduler._wave_run(*args,providers=providers(child,intake,verdict(root)))
            self.assertEqual(value['execution']['version'],'independent-waves-v1')
            self.assertEqual(len(value['execution']['waves'][0]['calls']),4)
            scheduler.verify(value,*args,True)

    def test_native_cap_two_is_shared_during_both_child_retries(self):
        spec = {**PROFILE,
                'primary': {'profile':'builtin:single-v1', 'overrides':{'reviewer':{'provider':{'model':'primary-model'}}}},
                'escalation': {'profile':'builtin:single-v1', 'overrides':{'reviewer':{'provider':{'model':'audit-model'}}}}}
        with committee(judging=spec, limits={'max_concurrent_calls':2}) as (root, _, intake):
            initial=execute(intake); gate=threading.Barrier(2,timeout=5)
            lock=threading.Lock(); active=peak=0; counts={}
            def transport(body,*_):
                nonlocal active,peak
                request,_,_=request_parts(body); model=request['model']
                with lock:
                    active+=1;peak=max(peak,active);counts[model]=counts.get(model,0)+1
                    attempt=counts[model]
                try:
                    gate.wait()
                    if attempt==1:
                        from llm_verifier.provider import HttpResponse
                        return HttpResponse(503,b'')
                    return completion(verdict(root),model=model)
                finally:
                    with lock:active-=1
            with patch.dict('os.environ',{'OPENAI_API_KEY':'offline'}), patch.object(HttpTransport,'request',side_effect=transport), patch('llm_verifier.provider.time.sleep'):
                value=pipeline.review(intake,initial)
            self.assertEqual((peak,counts),(2,{'primary-model':2,'audit-model':2}))
            self.assertEqual(value['data']['provenance']['attempts'],4)
            self.assertEqual(value['data']['provenance']['allotted_completion_tokens'],4*8192)
            pipeline.dossier(intake,initial,value)

    def test_preparation_failure_drains_already_submitted_proof(self):
        with committee(judging={'profile':'builtin:math-decomposed-v1'}) as (root,effective,intake):
            child=effective['judging']['committee'];initial=execute(intake)
            base=pipeline._committee_material(intake,initial,None,None)
            def material(member,*args):
                if member['stage']=='planning':raise InfrastructureError('full_evidence_context_budget')
                return base(member,*args)
            args=(child,effective['config'],intake['data']['manifest'],pipeline.evidence_refs(intake,initial),0,False,material,{})
            with self.assertRaises(InfrastructureError) as caught:
                scheduler.run(*args,providers=providers(child,intake,verdict(root)),files=intake['data']['files'])
            state=caught.exception.review_state
            self.assertEqual([r['member_id'] for r in state['reviews']],['proof'])
            self.assertEqual(state['provenance']['attempts'],1)
            scheduler.verify(state,*args,True,files=intake['data']['files'],failed=True)

    def test_defense_and_adjudication_wait_for_their_required_reports(self):
        from tests.test_committees import finding, response
        with committee() as (root,effective,intake):
            child=effective['judging']['committee'];initial=execute(intake)
            supplied=providers(child,intake,verdict(root))
            supplied['proof'].decision=report(findings=[finding()])
            supplied['defender'].decision={'responses':[response()]}
            supplied['adjudicator'].decision={'decision':verdict(root),'resolutions':[response('dismissed')]}
            value=pipeline.review(intake,initial,providers=supplied)
            groups=value['data']['execution']['groups']
            self.assertEqual([g['calls'][0]['stage'] for g in groups],['review','defense','adjudication'])
            for index in (1,2):
                self.assertGreaterEqual(groups[index]['queued_seconds'],groups[index-1]['finished_seconds'])
                bad=copy.deepcopy(value);bad['data']['execution']['groups'][index]['queued_seconds']=0
                with self.subTest(stage=index),self.assertRaises(VerifierError):
                    pipeline.verify_initial_review(intake,initial,reseal(bad))

    def test_frozen_clock_trace_is_stable_across_native_completion_orders(self):
        with committee(judging=PROFILE) as (root,_,intake):
            initial=execute(intake);plan=plan_for(intake)
            def transport(body,*_):
                _,member,context=request_parts(body)
                if member['id']=='reviewer':value=verdict(root)
                elif member['stage']=='planning':value=plan
                elif member['stage']=='region':value=region_report(context['region'])
                elif member['stage']=='adjudication':value={'decision':verdict(root),'resolutions':[]}
                else:value=report()
                return completion(value)
            from llm_verifier.jsonutil import canonical
            values=[]
            with patch.dict('os.environ',{'OPENAI_API_KEY':'offline'}), patch.object(HttpTransport,'request',side_effect=transport), patch('time.monotonic',return_value=0):
                for _ in range(5):values.append(canonical(pipeline.review(intake,initial)))
            self.assertTrue(all(v==values[0] for v in values))
