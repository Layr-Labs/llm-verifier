"""Bounded offline regressions for the three 87f9420 PR review findings."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

from llm_verifier import committee as runtime, pipeline, scheduler
from llm_verifier.diagnostics import ReviewDiagnostics
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.files import read_json
from llm_verifier.jsonutil import canonical, loads
from llm_verifier.provider import HttpResponse, HttpTransport
from llm_verifier.runner import DockerRunner, output_record
from tests.support import completion, reseal, request_parts
from tests.test_committees import committee, report
from tests.test_escalation import PROFILE, SINGLES, execute, verdict
from tests.test_scheduler import providers, wrapper_providers


class ReviewFixes(unittest.TestCase):
    def test_current_resume_cannot_select_historical_child_or_wrapper(self):
        spec = {**PROFILE, 'mode':'override', 'escalation':{'profile':'builtin:math-committee-v1'}}
        with committee(judging=spec) as (root,effective,intake):
            initial=execute(intake); profile=effective['judging']['committee']
            supplied=wrapper_providers(profile,intake,verdict(root))
            def fail(*_):raise InfrastructureError('provider_timeout')
            supplied['escalation']['proof'].review=fail
            with self.assertRaises(InfrastructureError):
                pipeline.review(intake,initial,providers=supplied,failure_out=root/'failure.json')
            saved=read_json(root/'failure.json')
            original=saved['data']['escalation_checkpoint']['rounds'][0]['entries']
            self.assertEqual(original['escalation']['failure']['state']['provenance']['attempts'],4)
            self.assertEqual(len(original['escalation']['failure']['state']['reviews']),3)
            for edit in ('missing_wrapper_version','legacy_wrapper_version','legacy_child','serial_child'):
                bad=copy.deepcopy(saved); checkpoint=bad['data']['escalation_checkpoint']
                state=checkpoint['rounds'][0]['entries']['escalation']['failure']['state']
                if edit=='missing_wrapper_version':checkpoint.pop('version')
                elif edit=='legacy_wrapper_version':checkpoint['version']='serial-v1'
                elif edit=='legacy_child':state['execution']['version']=scheduler.VERSION
                else:state.pop('execution')
                with self.subTest(edit=edit), patch.object(HttpTransport,'request') as http, self.assertRaises(VerifierError):
                    pipeline.review(intake,initial,resume=reseal(bad))
                http.assert_not_called()
            # Ordinary current recovery keeps all original work charged.
            supplied=wrapper_providers(profile,intake,verdict(root))
            supplied['primary']['reviewer'].review=lambda *_:self.fail('repeated primary')
            result=pipeline.review(intake,initial,providers=supplied,resume=saved)
            self.assertEqual(result['data']['primary'],original['primary'])
            self.assertEqual(result['data']['provenance']['attempts'],10)
            pipeline.dossier(intake,initial,result)

    def test_current_review_requires_wrapper_and_child_execution(self):
        with committee(judging=SINGLES) as (root,effective,intake):
            initial=execute(intake);profile=effective['judging']['committee']
            value=pipeline.review(intake,initial,providers=wrapper_providers(profile,intake,verdict(root)))
            for edit in ('missing_wrapper','legacy_wrapper','missing_child','legacy_child'):
                bad=copy.deepcopy(value)
                if edit=='missing_wrapper':bad['data'].pop('execution')
                elif edit=='legacy_wrapper':bad['data']['execution']['version']='serial-v1'
                elif edit=='missing_child':bad['data']['primary']['review'].pop('execution')
                else:bad['data']['primary']['review']['execution']['version']=scheduler.VERSION
                with self.subTest(edit=edit), self.assertRaises(VerifierError):
                    pipeline.verify_initial_review(intake,initial,reseal(bad))

    def test_legacy_failure_needs_explicit_trusted_low_level_mode(self):
        with committee() as (root,effective,intake):
            initial=execute(intake); child=effective['judging']['committee']
            supplied=providers(child,intake,verdict(root))
            def fail(*_):raise InfrastructureError('provider_timeout')
            supplied['proof'].review=fail
            args=(child,effective['config'],intake['data']['manifest'],pipeline.evidence_refs(intake,initial),0,False,
                  pipeline._committee_material(intake,initial,None,None),{})
            with self.assertRaises(InfrastructureError) as caught:
                scheduler._wave_run(*args,providers=supplied)
            state=caught.exception.review_state
            with self.assertRaisesRegex(VerifierError,'committee_execution_version'):
                scheduler.verify(state,*args,True,failed=True)
            scheduler.verify(state,*args,True,failed=True,execution_version=scheduler.VERSION)
            with self.assertRaises(InfrastructureError) as caught:
                runtime.run(*args,providers=supplied,serial=True,deadline=10**12)
            state=caught.exception.review_state
            with self.assertRaisesRegex(VerifierError,'committee_execution_version'):
                runtime.verify_failure(state,*args,True)
            runtime.verify_failure(state,*args,True,execution_version=runtime.SERIAL_EXECUTION)

    def test_snapshot_write_failure_collects_peers_and_preserves_original_error(self):
        for mode in ('record-only','override'):
            for first_child in ('primary','escalation'):
                for persistent in (False,True):
                    with self.subTest(mode=mode,first=first_child,persistent=persistent), committee(judging={**SINGLES,'mode':mode}) as (root,effective,intake):
                        initial=execute(intake); profile=effective['judging']['committee']
                        supplied=wrapper_providers(profile,intake,verdict(root)); release=threading.Event()
                        held='escalation' if first_child=='primary' else 'primary'
                        original_review=supplied[held]['reviewer'].review
                        def delayed(*args):
                            self.assertTrue(release.wait(5))
                            return original_review(*args)
                        supplied[held]['reviewer'].review=delayed
                        original_write=ReviewDiagnostics.write; original_error=OSError('first snapshot failed')
                        writes=[]
                        def persist(capture,path,exc):
                            writes.append(capture.envelope(exc))
                            if len(writes)==1:
                                self.assertEqual(set(capture.checkpoint['completed']),{first_child})
                                release.set();raise original_error
                            if persistent:raise OSError('later write failed')
                            return original_write(capture,path,exc)
                        with patch.object(ReviewDiagnostics,'write',side_effect=None,autospec=True) as writing:
                            writing.side_effect=persist
                            with self.assertRaises(OSError) as caught:
                                pipeline.review(intake,initial,providers=supplied,failure_out=root/'failure.json')
                        self.assertIs(caught.exception,original_error)
                        self.assertEqual(len(writes),2)
                        saved=caught.exception.escalation_checkpoint
                        if not persistent:self.assertEqual(read_json(root/'failure.json'),saved)
                        checkpoint=saved['data']['escalation_checkpoint']
                        self.assertEqual(checkpoint['status'],'in-progress')
                        self.assertEqual(checkpoint['running'],[])
                        self.assertEqual(set(checkpoint['completed']),{'primary','escalation'})
                        self.assertTrue(all(e['status']=='completed' for e in checkpoint['completed'].values()))
                        self.assertEqual(checkpoint['reserved_attempts'],2)
                        self.assertEqual(checkpoint['reserved_completion_tokens'],2*8192)
                        self.assertEqual(len(saved['data']['calls']),2)
                        with self.assertRaisesRegex(VerifierError,'in_progress'):
                            pipeline.review(intake,initial,providers=supplied,resume=saved)

    def test_nested_invalid_audit_keeps_primary_receipts_and_replay(self):
        for final in (False,True):
            for mode in ('record-only','override'):
                for save in (False,True):
                    with self.subTest(final=final,mode=mode,save=save), committee('computation' if final else 'proof',judging={
                        **SINGLES,'mode':mode,
                        'primary':{'profile':'builtin:single-v1','overrides':{'reviewer':{'provider':{'model':'primary-test'}}}},
                        'escalation':{'profile':'builtin:single-v1','overrides':{'reviewer':{'provider':{'model':'audit-test','response_format':'json_object'}}}}
                    }) as (root,effective,intake):
                        initial=execute(intake); first=followup=None; script=read_json(root/'simulation.json')
                        if final:
                            with patch.dict('os.environ',{'OPENAI_API_KEY':'offline-only'}),patch.object(HttpTransport,'request',return_value=completion(script['initial_decision'])):
                                first=pipeline.review(intake,initial)
                            with patch.object(DockerRunner,'run',return_value=output_record('completed',0,b'{"checked_n":25}',b'')):
                                followup=pipeline.execute(intake,first,initial)
                        decision=script['final_decision'] if final else script['initial_decision']
                        malformed={'unexpected':'invalid report'}
                        for _ in range(24):malformed={'nested':malformed}
                        self.assertEqual(loads(canonical(malformed)),malformed)
                        def transport(body,*_):
                            request=json.loads(body)
                            return completion(malformed if request['model']=='audit-test' else decision,model=request['model'])
                        kwargs={'failure_out':root/'failure.json'} if save else {}
                        with patch.dict('os.environ',{'OPENAI_API_KEY':'offline-only'}),patch.object(HttpTransport,'request',side_effect=transport) as http:
                            if mode=='override':
                                with self.assertRaisesRegex(InfrastructureError,'review_output_structure_limit') as caught:
                                    pipeline.review(intake,initial,first,followup,**kwargs)
                                failure=caught.exception.escalation_checkpoint
                                entries=failure['data']['escalation_checkpoint']['rounds'][-1]['entries']
                            else:
                                result=pipeline.review(intake,initial,first,followup,**kwargs)
                                entries=result['data']
                                dossier=pipeline.dossier(intake,initial,first or result,followup,result if final else None)
                                self.assertTrue(dossier['data']['judgment']['valid'])
                                pipeline.export_results(dossier)
                                failure=read_json(root/'failure.json') if save else None
                        self.assertEqual(http.call_count,2)
                        primary=entries['primary'];audit=entries['escalation']['failure']
                        self.assertEqual(primary['status'],'completed')
                        self.assertEqual(audit['state']['error'],'review_output_structure_limit')
                        self.assertEqual(audit['state']['reviews'],[])
                        self.assertEqual(audit['state']['provenance']['attempts'],1)
                        self.assertEqual(audit['state']['provenance']['allotted_completion_tokens'],8192)
                        call=audit['calls'][0]
                        self.assertEqual(call['grants'],[{'attempt':1,'completion_tokens':8192}])
                        self.assertEqual(call['outcome'],{'attempts':1,'error':'review_output_structure_limit','record_sha256':None})
                        self.assertEqual(json.loads(call['events'][0]['response']['outputs'][0]),malformed)
                        if failure is not None:
                            with patch.dict('os.environ',{'OPENAI_API_KEY':'offline-only'}),patch.object(HttpTransport,'request',return_value=completion(decision)) as retry:
                                resumed=pipeline.review(intake,initial,first,followup,resume=failure)
                            self.assertEqual(retry.call_count,1)
                            self.assertEqual(resumed['data']['primary'],primary)
                            self.assertEqual(resumed['data']['provenance']['attempts'],3)
                            pipeline.dossier(intake,initial,first or resumed,followup,resumed if final else None)

    def test_direct_child_coordinator_keeps_memory_evidence_on_write_error(self):
        from llm_verifier import parallel_escalation
        with committee(judging=SINGLES) as (root,effective,intake):
            initial=execute(intake);profile=effective['judging']['committee']
            supplied=wrapper_providers(profile,intake,verdict(root))
            original=OSError('persistent storage error')
            with patch.object(ReviewDiagnostics,'write',side_effect=original),self.assertRaises(OSError) as caught:
                parallel_escalation.run(intake,initial,providers=supplied,failure_out=root/'failure.json')
            self.assertIs(caught.exception,original)
            checkpoint=original.escalation_checkpoint['data']['escalation_checkpoint']
            self.assertEqual(set(checkpoint['completed']),{'primary','escalation'})
            self.assertEqual(checkpoint['running'],[])
            self.assertEqual(checkpoint['reserved_attempts'],2)

    def test_diagnostic_encoding_error_does_not_replace_original_write_error(self):
        with committee(judging=SINGLES) as (root,effective,intake):
            initial=execute(intake);profile=effective['judging']['committee']
            supplied=wrapper_providers(profile,intake,verdict(root))
            original=OSError('first storage error')
            with patch.object(ReviewDiagnostics,'write',side_effect=original),patch.object(ReviewDiagnostics,'envelope',side_effect=VerifierError('json_complexity_limit')),self.assertRaises(OSError) as caught:
                pipeline.review(intake,initial,providers=supplied,failure_out=root/'failure.json')
            self.assertIs(caught.exception,original)

    def test_seven_call_budget_rejects_downgrade_and_charges_legitimate_resume(self):
        spec={**PROFILE,'mode':'override','escalation':{'profile':'builtin:math-committee-v1'}}
        for mixed in (False,True):
            with self.subTest(mixed=mixed),committee(judging=spec,limits={'total_provider_requests':7}) as (root,_,intake):
                initial=execute(intake);resuming=False;calls=[]
                def transport(body,*_):
                    _,member,_=request_parts(body);calls.append(member['id'])
                    if member['id']=='reviewer':return completion(verdict(root))
                    if not resuming and (not mixed or member['id']=='proof'):return HttpResponse(401,b'')
                    return completion(report())
                with patch.dict('os.environ',{'OPENAI_API_KEY':'offline-only'}),patch.object(HttpTransport,'request',side_effect=transport):
                    with self.assertRaises(InfrastructureError):
                        pipeline.review(intake,initial,failure_out=root/'failure.json')
                    saved=read_json(root/'failure.json')
                    self.assertEqual(len(calls),5)
                    for mutation in ('legacy_shortened','missing_mode'):
                        bad=copy.deepcopy(saved);checkpoint=bad['data']['escalation_checkpoint']
                        if mutation=='missing_mode':checkpoint.pop('version')
                        else:
                            failure=checkpoint['rounds'][0]['entries']['escalation']['failure']
                            state=failure['state'];old=state['execution'];group=old['groups'][0]
                            call={k:v for k,v in group['calls'][0].items() if k!='started_seconds'}
                            state['execution']={k:v for k,v in old.items() if k in ('offset_seconds','time_limit_seconds')}
                            state['execution'].update(version=scheduler.VERSION,waves=[{
                                'started_seconds':group['queued_seconds'],'finished_seconds':group['finished_seconds'],'calls':[call]}])
                            state['reviews']=[];state['calls']=state['calls'][:1];failure['calls']=copy.deepcopy(state['calls'])
                            state['provenance'].update(attempts=1,allotted_completion_tokens=8192)
                        with self.subTest(mutation=mutation),self.assertRaises(VerifierError):
                            pipeline.review(intake,initial,resume=reseal(bad))
                        self.assertEqual(len(calls),5,'altered checkpoint made a request')
                    resuming=True
                    with self.assertRaisesRegex(InfrastructureError,'committee_review_budget_exhausted') as caught:
                        pipeline.review(intake,initial,resume=saved)
                self.assertEqual(len(calls),7)
                self.assertEqual(calls.count('reviewer'),1)
                history=caught.exception.escalation_checkpoint['data']['escalation_checkpoint']['rounds']
                from llm_verifier.parallel_escalation import _history_spent
                self.assertEqual(_history_spent(history)['attempts'],7)
                self.assertEqual(_history_spent(history)['allotted_completion_tokens'],7*8192)
                self.assertEqual(history[-1]['entries']['primary'],history[0]['entries']['primary'])
