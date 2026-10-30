"""Offline full-chain coverage of sampled escalation and saved evidence replay."""
import copy
from fractions import Fraction
import json
import unittest
from unittest.mock import patch

from llm_verifier import pipeline, escalation
from llm_verifier.artifacts import intake_snapshot
from llm_verifier.cli import main
from llm_verifier.config import load_config
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.escalation import selection
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical, digest
from llm_verifier.provider import HttpResponse, HttpTransport
from llm_verifier.runner import DockerRunner, output_record
from llm_verifier.simulation import ScriptedProvider
from tests.support import completion, reseal, scripted_model, request_parts
from tests.test_committees import committee
from tests.test_regions import plan_for, outputs

PROFILE = {"profile": "builtin:probabilistic-escalation-v1", "probability": 1}
SINGLES = {**PROFILE, "escalation": {"profile": "builtin:single-v1"}}


def verdict(root, valid=True, reason=None):
    decision = read_json(root / 'simulation.json')['initial_decision']
    decision['judgment']['valid'] = valid
    if reason:
        decision['judgment']['reason'] = reason
    return decision


def execute(intake):
    with patch.object(DockerRunner, 'run', return_value=output_record('completed', 0, b'{"checked_n":10,"all_equal":true}', b'')):
        return pipeline.execute(intake)


class Escalation(unittest.TestCase):
    def test_default_profile_and_minimum_combined_budget(self):
        with committee(judging={"profile": PROFILE['profile']}) as (_, effective, intake):
            profile = effective['judging']['committee']
            self.assertEqual((profile['probability'], profile['mode']), (0.1, 'record-only'))
            self.assertEqual(profile['primary']['protocol'], 'single-v1')
            self.assertEqual(profile['escalation']['protocol'], 'decomposed-review-v1')
        for limits in ({'total_provider_requests': 12}, {'total_review_tokens': 12 * 8192}):
            with self.subTest(limits=limits), self.assertRaisesRegex(VerifierError, 'committee_minimum_budget'):
                with committee(judging=PROFILE, limits=limits):
                    pass
        with committee(judging={**PROFILE, 'probability': 0}, limits={'total_provider_requests': 1, 'total_review_tokens': 8192}):
            pass

    def test_invalid_config_recursive_selectors_and_source_cap(self):
        invalid = [{**PROFILE, 'probability': x} for x in (-.01, 1.01, True, '0.1')]
        invalid += [{**PROFILE, 'mode': 'sometimes'}, {**PROFILE, 'seed': ''},
                    {**PROFILE, 'primary': PROFILE}, {**PROFILE, 'escalation': {'path': 'hidden.json'}},
                    {**PROFILE, 'escalation': {**PROFILE, 'probability': 0}}]
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(VerifierError):
                with committee(judging=spec):
                    pass
        for probability in (0, .1, 1):
            with self.assertRaisesRegex(VerifierError, 'region_source_count'):
                with committee(judging={**PROFILE, 'probability': probability,
                                        'escalation': {'profile': 'builtin:math-decomposed-v1', 'max_regions': 1}}):
                    pass

    def test_zero_probability_never_calls_escalation(self):
        with committee(judging={**PROFILE, 'probability': 0}) as (root, _, intake):
            initial = execute(intake)
            with scripted_model([verdict(root)]), patch.object(HttpTransport, 'request', wraps=HttpTransport.request) as request:
                first = pipeline.review(intake, initial)
            self.assertEqual(request.call_count, 1)
            self.assertIsNone(first['data']['escalation'])
            self.assertEqual(first['data']['authority'], {'judge': 'primary', 'reason': 'not_selected'})
            pipeline.dossier(intake, initial, first)

    def test_both_modes_decomposed_disagreement_and_export(self):
        for mode, expected in (('record-only', True), ('override', False)):
            with self.subTest(mode=mode), committee(judging={**PROFILE, 'mode': mode}) as (root, _, intake):
                initial = execute(intake)
                responses = [verdict(root, True)] + outputs(plan_for(intake), verdict(root, False))
                requests = []
                def transport(body, *args):
                    request, member, context = request_parts(body)
                    requests.append(request)
                    values = outputs(plan_for(intake), verdict(root, False))
                    value = (verdict(root, True) if member['id'] == 'reviewer' else
                             values[0] if member['stage'] == 'planning' else
                             values[1] if member['stage'] == 'review' else
                             values[-1] if member['stage'] == 'adjudication' else
                             values[2 + [r['id'] for r in plan_for(intake)['regions']].index(context['region']['id'])])
                    return completion(value)
                with patch.dict('os.environ', {'OPENAI_API_KEY': 'offline-only'}), patch.object(HttpTransport, 'request', side_effect=transport):
                    first = pipeline.review(intake, initial)
                self.assertEqual(len(requests), len(responses))
                self.assertEqual(first['data']['decision']['judgment']['valid'], expected)
                self.assertTrue(first['data']['comparison']['valid_disagreement'])
                self.assertEqual(first['data']['provenance']['attempts'], 6)
                self.assertEqual(len(first['data']['escalation']['review']['reviews']), 5)
                for request in requests[1:]:
                    self.assertNotIn('"initial_decision":{', request['messages'][1]['content'])
                result = pipeline.finalize(intake, initial, first, root/'score.json', root/'dossier.json', results_path=root/'accepted.json')
                self.assertEqual(read_json(root/'score.json')['valid'], expected)
                self.assertEqual(read_json(root/'dossier.json'), result)
                if expected:
                    self.assertEqual(pipeline.export_results(result), read_json(root/'accepted.json'))
                else:
                    self.assertFalse((root/'accepted.json').exists())

    def test_selection_recomputable_stable_across_intakes_and_retry(self):
        with committee(judging={**SINGLES, 'probability': .5}) as (root, effective, intake):
            profile = effective['judging']['committee']
            other = intake_snapshot(effective, root/'submission')
            a, b = selection(profile, intake), selection(profile, other)
            self.assertNotEqual(intake['bindings']['evaluation_id'], other['bindings']['evaluation_id'])
            self.assertEqual(a, b)
            self.assertEqual(a['sha256'], digest(a['inputs']))
            self.assertEqual(a['selected'], int(a['sha256'], 16) < int(Fraction('.5') * 2**256))
            seen = set()
            for i in range(40):
                raw = read_json(root/'verifier.json'); raw['judging']['seed'] = str(i); write_json(root/'verifier.json', raw)
                config = load_config(root, 'verifier.json', 'submission')
                candidate = intake_snapshot(config, root/'submission')
                chosen = selection(config['judging']['committee'], candidate)
                seen.add(chosen['selected'])
                initial = execute(candidate)
                with scripted_model([verdict(root)] * (2 if chosen['selected'] else 1)):
                    first = pipeline.review(candidate, initial)
                self.assertEqual(first['data']['selection'], chosen)
                if len(seen) == 2:
                    break
            self.assertEqual(seen, {True, False})

    def test_transport_retry_is_same_request_and_selection(self):
        with committee(judging=SINGLES) as (root, effective, intake):
            initial = execute(intake)
            bodies = []
            replies = iter([HttpResponse(503, b''), completion(verdict(root)), completion(verdict(root, False))])
            def transport(body, *args):
                bodies.append(body)
                return next(replies)
            with patch.dict('os.environ', {'OPENAI_API_KEY':'offline-only'}), patch.object(HttpTransport, 'request', side_effect=transport), patch('time.sleep'):
                first = pipeline.review(intake, initial)
            self.assertEqual(bodies[0], bodies[1])
            self.assertEqual(first['data']['selection'], selection(effective['judging']['committee'], intake))
            self.assertEqual(first['data']['provenance']['attempts'], 3)

    def test_audit_failure_visible_preserves_primary_and_can_export(self):
        with committee(judging=SINGLES) as (root, _, intake):
            initial = execute(intake)
            with scripted_model([verdict(root), {'invalid': True}]):
                first = pipeline.review(intake, initial, failure_out=root/'failed.json')
            self.assertEqual(first['data']['escalation']['status'], 'failed')
            self.assertFalse(first['data']['comparison']['available'])
            self.assertTrue(first['data']['decision']['judgment']['valid'])
            failure = read_json(root/'failed.json')
            self.assertEqual(failure['stage'], 'review_failure')
            self.assertEqual(failure['data']['escalation_checkpoint']['rounds'][-1]['entries']['primary'], first['data']['primary'])
            self.assertEqual(first['data']['escalation']['failure']['state']['reviews'][0]['report'], {'invalid': True})
            self.assertTrue(first['data']['escalation']['failure']['calls'])
            pipeline.finalize(intake, initial, first, root/'score.json', root/'dossier.json', results_path=root/'accepted.json')
            self.assertTrue((root/'accepted.json').exists())
            with self.assertRaisesRegex(VerifierError, 'artifact_stage'):
                pipeline.dossier(intake, initial, failure)

    def test_override_failure_and_resume_only_escalation(self):
        with committee(judging={**SINGLES, 'mode': 'override'}) as (root, _, intake):
            initial = execute(intake)
            with scripted_model([verdict(root), {'invalid': True}]), self.assertRaises(InfrastructureError):
                pipeline.review(intake, initial, failure_out=root/'failed.json')
            failed = read_json(root/'failed.json')
            saved = failed['data']['escalation_checkpoint']['rounds'][-1]['entries']['primary']
            with scripted_model([verdict(root, False)]) as _, patch.object(HttpTransport, 'request', wraps=HttpTransport.request) as transport:
                first = pipeline.review(intake, initial, resume=failed)
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(first['data']['primary'], saved)
            self.assertEqual(first['data']['provenance']['attempts'], 3)
            self.assertFalse(first['data']['decision']['judgment']['valid'])
            pipeline.dossier(intake, initial, first)

    @patch('llm_verifier.escalation.verify', new=escalation._serial_verify)
    @patch('llm_verifier.escalation.run', new=escalation._serial_run)
    def test_legacy_test_http_failure_conservative_reservation_and_budget_exhaustion(self):
        for limits in ({'total_provider_requests': 3}, {'total_review_tokens': 3*8192}):
            with self.subTest(limits=limits), committee(judging={**SINGLES, 'mode': 'override'}, limits=limits) as (root, _, intake):
                initial = execute(intake)
                with patch.dict('os.environ', {'OPENAI_API_KEY':'offline-only'}), patch.object(HttpTransport, 'request', side_effect=[completion(verdict(root)), HttpResponse(401,b'')]), self.assertRaises(InfrastructureError):
                    pipeline.review(intake, initial, failure_out=root/'failed.json')
                failed = read_json(root/'failed.json')
                state = failed['data']['escalation_checkpoint']['failures'][0]['state']
                self.assertEqual(state['interrupted']['attempts'], 2)
                with patch.object(HttpTransport, 'request') as transport, self.assertRaises(InfrastructureError):
                    pipeline.review(intake, initial, resume=failed)
                transport.assert_not_called()

    def test_tampered_selection_authority_budget_child_or_failure_rejected(self):
        with committee(judging=SINGLES) as (root, _, intake):
            initial = execute(intake)
            with scripted_model([verdict(root), verdict(root, False)]):
                first = pipeline.review(intake, initial)
            mutations = [lambda d: d['selection'].update(selected=False),
                         lambda d: d['selection']['inputs'].update(seed='another'),
                         lambda d: d['authority'].update(judge='escalation'),
                         lambda d: d.update(decision=d['escalation']['review']['decision']),
                         lambda d: d['provenance'].update(attempts=1),
                         lambda d: d['primary'].update(carried=True),
                         lambda d: d['escalation']['review']['reviews'][0]['provenance'].update(prompt_sha256='0'*64)]
            for mutate in mutations:
                bad = copy.deepcopy(first); mutate(bad['data'])
                with self.assertRaises(VerifierError):
                    pipeline.dossier(intake, initial, reseal(bad))

    def test_followup_union_retains_terminal_primary_without_repeat(self):
        with committee('computation', judging=SINGLES) as (root, _, intake):
            initial = execute(intake)
            script = read_json(root/'simulation.json')
            terminal = copy.deepcopy(script['final_decision'])
            terminal['judgment']['evidence_refs'] = ['source:proof.md']
            follow = script['initial_decision']
            with scripted_model([terminal, follow]):
                first = pipeline.review(intake, initial)
            self.assertEqual(first['data']['decision'], follow)
            with patch.object(DockerRunner, 'run', return_value=output_record('completed',0,b'{"checked_n":10,"all_equal":true}',b'')):
                followup = pipeline.execute(intake, first, initial)
            with scripted_model([terminal]), patch.object(HttpTransport, 'request', wraps=HttpTransport.request) as transport:
                final = pipeline.review(intake, initial, first, followup)
            self.assertEqual(transport.call_count, 1)
            self.assertTrue(final['data']['primary']['carried'])
            self.assertEqual(final['data']['primary']['review'], first['data']['primary']['review'])
            self.assertEqual(final['data']['provenance']['attempts'], 1)
            pipeline.dossier(intake, initial, first, followup, final)

    def test_followup_deduplicates_and_does_not_leak_primary(self):
        with committee('computation', judging=SINGLES) as (root, _, intake):
            initial = execute(intake)
            script = read_json(root/'simulation.json')
            with scripted_model([script['initial_decision']] * 2):
                first = pipeline.review(intake, initial)
            self.assertEqual(first['data']['decision']['requests'], script['initial_decision']['requests'])
            with patch.object(DockerRunner, 'run', return_value=output_record('completed',0,b'{"checked_n":10,"all_equal":true}',b'')):
                followup = pipeline.execute(intake, first, initial)
            bodies = []
            def transport(body, *args):
                bodies.append(json.loads(body))
                return completion(script['final_decision'])
            with patch.dict('os.environ', {'OPENAI_API_KEY':'offline-only'}), patch.object(HttpTransport, 'request', side_effect=transport):
                final = pipeline.review(intake, initial, first, followup)
            for body in bodies:
                self.assertIn('"initial_decision":null', body['messages'][1]['content'])
            pipeline.dossier(intake, initial, first, followup, final)

    def test_followup_retry_never_repeats_carried_primary_or_plan(self):
        profile = {**PROFILE, 'mode': 'override', 'escalation': {'profile': 'builtin:math-decomposed-v1', 'max_regions': 3}}
        with committee('computation', judging=profile) as (root, _, intake):
            initial = execute(intake)
            script = read_json(root/'simulation.json')
            terminal = copy.deepcopy(script['final_decision']); terminal['judgment']['evidence_refs'] = ['source:proof.md']
            plan = plan_for(intake)
            responses = outputs(plan, terminal)
            # Whole proof requests the one batch, still collecting region reports.
            responses[1]['requests'] = script['initial_decision']['requests']
            responses.pop()  # no adjudication until follow-up
            with scripted_model([terminal] + responses):
                first = pipeline.review(intake, initial)
            with patch.object(DockerRunner, 'run', return_value=output_record('completed',0,b'{"checked_n":25,"all_equal":true}',b'')):
                followup = pipeline.execute(intake, first, initial)
            with scripted_model([{'bad': True}]), self.assertRaises(InfrastructureError):
                pipeline.review(intake, initial, first, followup, failure_out=root/'failure.json')
            failure = read_json(root/'failure.json')
            self.assertTrue(failure['data']['escalation_checkpoint']['rounds'][-1]['entries']['primary']['carried'])
            with scripted_model(outputs(plan, terminal, final=True)), patch.object(HttpTransport, 'request', wraps=HttpTransport.request) as transport:
                final = pipeline.review(intake, initial, first, followup, resume=failure)
            self.assertEqual(transport.call_count, 5)  # proof, three regions, adjudication
            self.assertEqual(final['data']['primary']['review'], first['data']['primary']['review'])
            self.assertEqual(final['data']['escalation']['review']['region_plan'], first['data']['escalation']['review']['region_plan'])
            pipeline.dossier(intake, initial, first, followup, final)

    def test_failed_audit_carried_through_primary_followup(self):
        with committee('computation', judging=SINGLES) as (root, _, intake):
            initial = execute(intake)
            script = read_json(root/'simulation.json')
            with scripted_model([script['initial_decision'], {'bad': True}]):
                first = pipeline.review(intake, initial)
            with patch.object(DockerRunner, 'run', return_value=output_record('completed',0,b'{"checked_n":25,"all_equal":true}',b'')):
                followup = pipeline.execute(intake, first, initial)
            with scripted_model([script['final_decision']]), patch.object(HttpTransport, 'request', wraps=HttpTransport.request) as transport:
                final = pipeline.review(intake, initial, first, followup)
            self.assertEqual(transport.call_count, 1)
            self.assertTrue(final['data']['escalation']['carried'])
            pipeline.dossier(intake, initial, first, followup, final)

    def test_joint_followup_overflow_does_not_drop_requests(self):
        with committee('computation', judging=SINGLES, limits={'max_followups': 1}) as (root, _, intake):
            initial = execute(intake)
            request = read_json(root/'simulation.json')['initial_decision']
            other = copy.deepcopy(request); other['requests'][0]['inputs']['n'] += 1
            with scripted_model([request, other]), self.assertRaisesRegex(VerifierError, 'run_count_budget'):
                pipeline.review(intake, initial)

    def test_final_review_shares_request_and_token_budget(self):
        for limits in ({'total_provider_requests': 2}, {'total_review_tokens': 2*8192}):
            with self.subTest(limits=limits), committee('computation', judging=SINGLES, limits=limits) as (root, _, intake):
                initial = execute(intake)
                request = read_json(root/'simulation.json')['initial_decision']
                with scripted_model([request, request]):
                    first = pipeline.review(intake, initial)
                with patch.object(DockerRunner, 'run', return_value=output_record('completed',0,b'{"checked_n":25,"all_equal":true}',b'')):
                    followup = pipeline.execute(intake, first, initial)
                with patch.object(HttpTransport, 'request') as transport, self.assertRaisesRegex(InfrastructureError, 'budget_exhausted'):
                    pipeline.review(intake, initial, first, followup)
                transport.assert_not_called()

    @patch('llm_verifier.escalation.verify', new=escalation._serial_verify)
    @patch('llm_verifier.escalation.run', new=escalation._serial_run)
    def test_legacy_both_children_share_one_time_budget(self):
        class Measured(ScriptedProvider):
            def __init__(self, *args, elapsed=0, **kwargs):
                super().__init__(*args, **kwargs); self.elapsed = elapsed; self.offers = []
            def review(self, messages, schema, seconds, requests):
                self.offers.append(seconds)
                value, provenance = super().review(messages, schema, seconds, requests)
                provenance['elapsed_seconds'] = self.elapsed
                return value, provenance
        with committee(judging=SINGLES, limits={'total_review_seconds': 5}) as (root, effective, intake):
            initial = execute(intake)
            profile = effective['judging']['committee']
            primary = Measured(profile['primary']['members'][0]['provider'], verdict(root), elapsed=4)
            audit = Measured(profile['escalation']['members'][0]['provider'], verdict(root), elapsed=.5)
            first = pipeline.review(intake, initial, providers={'primary':{'reviewer':primary}, 'escalation':{'reviewer':audit}})
            self.assertLessEqual(audit.offers[0], 1)
            self.assertGreaterEqual(first['data']['provenance']['elapsed_seconds'], 4.5)
            pipeline.dossier(intake, initial, first)

    def test_resume_rejects_changed_parents_or_primary_before_calls(self):
        with committee(judging={**SINGLES, 'mode':'override'}) as (root, effective, intake):
            initial = execute(intake)
            with scripted_model([verdict(root), {'bad':True}]), self.assertRaises(InfrastructureError):
                pipeline.review(intake, initial, failure_out=root/'failed.json')
            failed = read_json(root/'failed.json')
            for mutation in (
                lambda f: f['parents'].reverse(),
                lambda f: f['data']['escalation_checkpoint']['rounds'][-1]['entries']['primary']['review']['reviews'][0]['provenance'].update(prompt_sha256='0'*64),
                lambda f: f['data']['escalation_checkpoint']['selection'].update(selected=False),
                lambda f: f['data']['escalation_checkpoint']['rounds'][0]['entries']['escalation']['failure']['state']['provenance'].update(attempts=0),
            ):
                bad = copy.deepcopy(failed); mutation(bad)
                with patch.object(HttpTransport, 'request') as transport, self.assertRaises(VerifierError):
                    pipeline.review(intake, initial, resume=reseal(bad))
                transport.assert_not_called()
            other = intake_snapshot(effective, root/'submission')
            with patch.object(HttpTransport, 'request') as transport, self.assertRaises(VerifierError):
                pipeline.review(other, execute(other), resume=failed)
            transport.assert_not_called()

    def test_cli_resume_keeps_diagnostic_inputs_and_clears_stale_score(self):
        with committee(judging={**SINGLES, 'mode':'override'}) as (root, _, intake):
            initial = execute(intake)
            write_json(root/'intake.json', intake); write_json(root/'initial.json', initial)
            args = ['review-initial','--intake',str(root/'intake.json'),'--initial',str(root/'initial.json'),'--out',str(root/'review.json')]
            with scripted_model([verdict(root), {'bad':True}]):
                self.assertEqual(main(args), 2)
            failure = (root/'review.json.failure.json').read_bytes()
            # Outputs cannot alias an input checkpoint.
            with patch.object(HttpTransport, 'request') as transport:
                self.assertEqual(main(args + ['--resume',str(root/'review.json.failure.json')]), 2)
            transport.assert_not_called()
            self.assertEqual((root/'review.json.failure.json').read_bytes(), failure)
            with scripted_model([verdict(root, False)]):
                self.assertEqual(main(args + ['--resume',str(root/'review.json.failure.json'),'--failure-out',str(root/'new-failure.json')]), 0)
            self.assertFalse((root/'new-failure.json').exists())
            self.assertEqual(read_json(root/'review.json')['data']['provenance']['attempts'], 3)

    def test_simulation_nested_maps_and_missing_extra_reports(self):
        from llm_verifier.simulation import simulate
        with committee(judging=SINGLES) as (root, effective, _):
            script = {'initial_results':[], 'initial_reviews':{'primary':{'reviewer':verdict(root)}, 'escalation':{'reviewer':verdict(root)}}, 'followup_results':[], 'final_reviews':None}
            result = simulate(effective, root/'submission', script, root/'simulated.json')
            self.assertTrue(result['simulation'])
            with self.assertRaises(VerifierError):
                pipeline.export_results(result)
            script['initial_reviews']['escalation']['extra'] = verdict(root)
            with self.assertRaisesRegex(VerifierError, 'simulation_unexpected_member_reports'):
                simulate(effective, root/'submission', script, root/'simulated.json')

    @patch('llm_verifier.escalation.verify', new=escalation._serial_verify)
    @patch('llm_verifier.escalation.run', new=escalation._serial_run)
    def test_legacy_test_saved_interruption_cannot_be_relabelled_semantic_failure(self):
        with committee(judging=SINGLES) as (root, _, intake):
            initial = execute(intake)
            with patch.dict('os.environ', {'OPENAI_API_KEY':'offline-only'}), patch.object(HttpTransport, 'request', side_effect=[completion(verdict(root)), HttpResponse(401,b'')]):
                first = pipeline.review(intake, initial, failure_out=root/'failed.json')
            for code in ('committee_invalid_review_failure_interruption_binding', 'committee_invalid_review_committee_provenance_binding'):
                bad = copy.deepcopy(first)
                state = bad['data']['escalation']['failure']['state']
                state['interrupted'] = None
                state['error'] = code
                with self.assertRaisesRegex(VerifierError, 'failure_replay_binding'):
                    pipeline.verify_initial_review(intake, initial, reseal(bad))
                saved = read_json(root/'failed.json')
                saved['data']['escalation_checkpoint']['failures'][0]['state'] = state
                with patch.object(HttpTransport, 'request') as transport, self.assertRaises(VerifierError):
                    pipeline.review(intake, initial, resume=reseal(saved))
                transport.assert_not_called()

    @patch('llm_verifier.escalation.verify', new=escalation._serial_verify)
    @patch('llm_verifier.escalation.run', new=escalation._serial_run)
    def test_legacy_deadline_and_resume_charge_outer_work(self):
        from llm_verifier import escalation
        class Clock:
            now = 0
            def __call__(self):
                return self.now
        class Timed(ScriptedProvider):
            def __init__(self, *args, duration=0, fail=False, **kwargs):
                super().__init__(*args, **kwargs)
                self.duration, self.fail, self.calls = duration, fail, []
            def review(self, messages, schema, seconds, requests):
                self.calls.append(seconds)
                clock.now += self.duration
                if self.fail:
                    raise InfrastructureError('provider_timeout')
                value, provenance = super().review(messages, schema, seconds, requests)
                provenance['elapsed_seconds'] = self.duration
                return value, provenance
        for overhead, audit_duration, mode in ((5,1,'override'), (10,0,'override'), (0,9.01,'record-only')):
            with self.subTest(overhead=overhead, mode=mode), committee(judging={**SINGLES, 'mode':mode}, limits={'total_review_seconds':10}) as (root, effective, intake):
                initial = execute(intake); clock = Clock()
                profile = effective['judging']['committee']
                def child(name, duration=0, fail=False):
                    return Timed(profile[name]['members'][0]['provider'], verdict(root), duration=duration, fail=fail)
                primary, audit = child('primary',1), child('escalation',audit_duration,True)
                original = escalation._check_decision
                def expensive(value, *args):
                    original(value, *args)
                    if 'reviews' in value:
                        clock.now += overhead
                with patch('time.monotonic', clock), patch.object(escalation, '_check_decision', expensive):
                    if mode == 'override':
                        with self.assertRaises(InfrastructureError):
                            pipeline.review(intake, initial, providers={'primary':{'reviewer':primary},'escalation':{'reviewer':audit}}, failure_out=root/'failed.json')
                    else:
                        first = pipeline.review(intake, initial, providers={'primary':{'reviewer':primary},'escalation':{'reviewer':audit}}, failure_out=root/'failed.json')
                        self.assertEqual(first['data']['provenance']['elapsed_seconds'], 10)
                        self.assertTrue(first['data']['decision']['judgment']['valid'])
                        self.assertGreater(first['data']['escalation']['failure']['state']['wall_elapsed_seconds'], 9)
                        pipeline.dossier(intake, initial, first)
                failure = read_json(root/'failed.json')
                checkpoint = failure['data']['escalation_checkpoint']
                self.assertEqual(checkpoint['elapsed_seconds'], min(10, clock.now))
                new_primary, new_audit = child('primary'), child('escalation',7)
                with patch('time.monotonic', clock):
                    if mode == 'override':
                        with self.assertRaises(InfrastructureError):
                            pipeline.review(intake, initial, providers={'primary':{'reviewer':new_primary},'escalation':{'reviewer':new_audit}}, resume=failure)
                    else:
                        resumed = pipeline.review(intake, initial, providers={'primary':{'reviewer':new_primary},'escalation':{'reviewer':new_audit}}, resume=failure)
                        self.assertEqual(resumed['data']['escalation']['status'], 'failed')
                        self.assertEqual(resumed['data']['provenance']['elapsed_seconds'], 10)
                self.assertEqual(new_primary.calls, [])
                if overhead == 5:
                    self.assertEqual(new_audit.calls, [3])
                else:
                    self.assertEqual(new_audit.calls, [])

    def test_final_prompts_remove_both_peer_request_reasons(self):
        with committee('computation', judging=SINGLES) as (root, _, intake):
            initial = execute(intake); script = read_json(root/'simulation.json')
            primary = copy.deepcopy(script['initial_decision'])
            audit = copy.deepcopy(primary)
            primary['requests'][0]['reason'] = 'PRIMARY_PRIVATE_VERDICT'
            audit['requests'][0]['inputs']['n'] += 1
            audit['requests'][0]['reason'] = 'ESCALATION_PRIVATE_VERDICT'
            with scripted_model([primary,audit]):
                first = pipeline.review(intake, initial)
            with patch.object(DockerRunner, 'run', return_value=output_record('completed',0,b'{"checked_n":26,"all_equal":true}',b'')):
                followup = pipeline.execute(intake, first, initial)
            bodies = []
            def transport(body, *args):
                bodies.append(body.decode()); return completion(script['final_decision'])
            with patch.dict('os.environ', {'OPENAI_API_KEY':'offline-only'}), patch.object(HttpTransport, 'request', side_effect=transport):
                final = pipeline.review(intake, initial, first, followup)
            for body in bodies:
                self.assertNotIn('PRIMARY_PRIVATE_VERDICT', body)
                self.assertNotIn('ESCALATION_PRIVATE_VERDICT', body)
            # Full original reasons are still retained in the evidence artifacts.
            self.assertIn('PRIMARY_PRIVATE_VERDICT', canonical(followup).decode())
            pipeline.dossier(intake, initial, first, followup, final)

    @patch('llm_verifier.escalation.verify', new=escalation._serial_verify)
    @patch('llm_verifier.escalation.run', new=escalation._serial_run)
    def test_legacy_test_failure_deadline_cannot_refund_time_or_hide_completed_rejection(self):
        with committee(judging=SINGLES) as (root, _, intake):
            initial = execute(intake)
            with patch.dict('os.environ', {'OPENAI_API_KEY':'offline-only'}), patch.object(HttpTransport, 'request', side_effect=[completion(verdict(root)), HttpResponse(401,b'')]):
                first = pipeline.review(intake, initial, failure_out=root/'failed.json')
            failed = read_json(root/'failed.json')
            state = failed['data']['escalation_checkpoint']['failures'][0]['state']
            state['time_limit_seconds'] = 0
            state['provenance']['elapsed_seconds'] = 0
            failed['data']['escalation_checkpoint']['elapsed_seconds'] = failed['data']['escalation_checkpoint']['primary']['review']['provenance']['elapsed_seconds']
            with patch.object(HttpTransport, 'request') as transport, self.assertRaisesRegex(VerifierError, 'escalation_failure_deadline_binding'):
                pipeline.review(intake, initial, resume=reseal(failed))
            transport.assert_not_called()
            with scripted_model([verdict(root), verdict(root, False)]):
                complete = pipeline.review(intake, initial)
            bogus = copy.deepcopy(first)
            failure = bogus['data']['escalation']['failure']
            failure['state']['reviews'] = complete['data']['escalation']['review']['reviews']
            failure['state'].update(interrupted=None, error='committee_review_budget_exhausted')
            with self.assertRaisesRegex(VerifierError, 'failure_contains_completed_review'):
                pipeline.verify_initial_review(intake, initial, reseal(bogus))

    def test_multiple_resumes_preserve_failure_history_and_primary(self):
        with committee(judging={**SINGLES, 'mode':'override'}) as (root, _, intake):
            initial = execute(intake)
            with scripted_model([verdict(root), {'bad':True}]), self.assertRaises(InfrastructureError):
                pipeline.review(intake, initial, failure_out=root/'first-failure.json')
            failed = read_json(root/'first-failure.json')
            with scripted_model([{'still_bad':True}]), self.assertRaises(InfrastructureError):
                pipeline.review(intake, initial, resume=failed, failure_out=root/'second-failure.json')
            second = read_json(root/'second-failure.json')
            saved = second['data']['escalation_checkpoint']
            self.assertEqual(len(saved['rounds']), 2)
            self.assertEqual(saved['rounds'][-1]['entries']['primary'], failed['data']['escalation_checkpoint']['rounds'][-1]['entries']['primary'])
            with scripted_model([verdict(root)]), patch.object(HttpTransport, 'request', wraps=HttpTransport.request) as transport:
                first = pipeline.review(intake, initial, resume=second)
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(first['data']['provenance']['attempts'], 4)
            self.assertEqual(first['data']['recovery'], saved['rounds'])
            pipeline.dossier(intake, initial, first)

    @patch('llm_verifier.escalation.verify', new=escalation._serial_verify)
    @patch('llm_verifier.escalation.run', new=escalation._serial_run)
    def test_legacy_exhausted_phase_has_no_fractional_time_from_cancellation(self):
        clock = [0.0]
        class Timed(ScriptedProvider):
            def __init__(self, *args, duration=0, fail=False):
                super().__init__(*args); self.duration=duration; self.fail=fail; self.calls=0
            def review(self, messages, schema, seconds, requests):
                self.calls += 1; clock[0] += self.duration
                if self.fail:
                    raise InfrastructureError('provider_timeout')
                value, provenance = super().review(messages, schema, seconds, requests)
                provenance['elapsed_seconds'] = self.duration
                return value, provenance
        with committee(judging=SINGLES, limits={'total_review_seconds':1800}) as (root, effective, intake):
            initial = execute(intake); profile=effective['judging']['committee']
            primary=Timed(profile['primary']['members'][0]['provider'], verdict(root), duration=1000)
            audit=Timed(profile['escalation']['members'][0]['provider'], verdict(root), duration=200.01, fail=True)
            with patch('time.monotonic', side_effect=lambda: clock[0]):
                first=pipeline.review(intake, initial, providers={'primary':{'reviewer':primary},'escalation':{'reviewer':audit}}, failure_out=root/'failure.json')
            self.assertEqual(first['data']['provenance']['elapsed_seconds'], 1200)
            audit.calls=0
            with patch('time.monotonic', side_effect=lambda: clock[0]):
                resumed=pipeline.review(intake, initial, providers={'primary':{'reviewer':primary},'escalation':{'reviewer':audit}}, resume=read_json(root/'failure.json'))
            self.assertEqual(audit.calls, 0)
            self.assertEqual(primary.calls, 1)
            self.assertEqual(resumed['data']['provenance']['elapsed_seconds'], 1200)
