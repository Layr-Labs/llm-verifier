"""Synchronization-based coverage of the shared scheduler; never calls a model."""

from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import unittest
from unittest.mock import patch

from llm_verifier import committee as runtime, pipeline, scheduler
from llm_verifier.artifacts import seal
from llm_verifier.diagnostics import MAX_CAPTURE, MAX_EVENTS, ReviewDiagnostics
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.files import read_json
from llm_verifier.jsonutil import canonical
from llm_verifier.provider import HttpResponse, HttpTransport
from llm_verifier.simulation import ScriptedProvider
from tests.support import completion, reseal
from tests.test_committees import committee, report
from tests.test_regions import plan_for, region_report
from tests.test_escalation import PROFILE, SINGLES, execute, verdict


class Callback(ScriptedProvider):
    def __init__(self, config, value, callback):
        super().__init__(config, value)
        self.callback = callback

    def review(self, *args):
        self.callback()
        return super().review(*args)


def providers(child, intake, decision, callbacks=None):
    result = {}
    plan = plan_for(intake)
    for member in child['members']:
        stage = member['stage']
        value = (decision if child['protocol'] == 'single-v1' else
                 plan if stage == 'planning' else
                 {'decision': decision, 'resolutions': []} if stage == 'adjudication' else
                 {'responses': []} if stage == 'defense' else report())
        if stage == 'region':
            result[member['id']] = ScriptedProvider(member['provider'], [region_report(r) for r in plan['regions']], sequence=True)
        elif callbacks and member['id'] in callbacks:
            result[member['id']] = Callback(member['provider'], value, callbacks[member['id']])
        else:
            result[member['id']] = ScriptedProvider(member['provider'], value)
    return result


def wrapper_providers(profile, intake, decision):
    return {n: providers(profile[n], intake, decision) for n in ('primary', 'escalation')}


class SchedulerTests(unittest.TestCase):
    def test_committee_overlap_cap_and_order(self):
        spec = {'profile': 'builtin:math-committee-v1', 'add_members': [
            {'id': n, 'stage': 'review', 'persona': 'builtin:proof-reviewer-v1'} for n in ('extra1', 'extra2')]}
        with committee(judging=spec, limits={'max_concurrent_calls': 4}) as (root, effective, intake):
            child = effective['judging']['committee']
            names = [m['id'] for m in child['members'] if m['stage'] == 'review']
            barrier = threading.Barrier(4, timeout=5)
            second = threading.Barrier(2, timeout=5)
            order, active, peak = [], 0, 0
            lock = threading.Lock()
            gates = [threading.Event() for _ in range(4)]
            gates[3].set()
            def visit(index):
                def callback():
                    nonlocal active, peak
                    with lock:
                        active += 1; peak = max(peak, active)
                    (barrier if index < 4 else second).wait()
                    if index < 4:
                        self.assertTrue(gates[index].wait(5))
                    with lock:
                        order.append(names[index]); active -= 1
                    if 0 < index < 4:
                        gates[index - 1].set()
                return callback
            supplied = providers(child, intake, verdict(root), {n: visit(i) for i, n in enumerate(names)})
            initial = execute(intake)
            first = pipeline.review(intake, initial, providers=supplied)
            self.assertEqual(peak, 4)
            self.assertEqual([n for n in order if n in names[:4]], list(reversed(names[:4])))
            self.assertEqual([r['member_id'] for r in first['data']['reviews'][:-1]], names)
            pipeline.dossier(intake, initial, first)

    def test_nested_children_share_four_model_slots(self):
        spec = {**PROFILE, 'primary': {'profile': 'builtin:math-committee-v1'},
                'escalation': {'profile': 'builtin:math-committee-v1'}}
        with committee(judging=spec, limits={'max_concurrent_calls': 4}) as (root, effective, intake):
            profile = effective['judging']['committee']
            lock = threading.Lock(); barrier = threading.Barrier(4, timeout=5)
            active = peak = calls = 0
            def visit():
                nonlocal active, peak, calls
                with lock:
                    active += 1; peak = max(peak, active); calls += 1
                barrier.wait()
                with lock:
                    active -= 1
            supplied = {n: providers(profile[n], intake, verdict(root), {m['id']: visit for m in profile[n]['members'] if m['stage'] == 'review'}) for n in ('primary', 'escalation')}
            initial = execute(intake)
            first = pipeline.review(intake, initial, providers=supplied)
            self.assertEqual((peak, calls), (4, 8))
            self.assertEqual(first['data']['provenance']['attempts'], 10)
            pipeline.dossier(intake, initial, first)

    def test_reused_region_provider_is_ordered_and_never_reentered(self):
        with committee(judging={'profile': 'builtin:math-decomposed-v1'}) as (root, effective, intake):
            child = effective['judging']['committee']
            supplied = providers(child, intake, verdict(root))
            region = supplied['region']
            original = region.review
            lock = threading.Lock(); seen = []
            def guarded(*args):
                self.assertTrue(lock.acquire(blocking=False))
                try:
                    result = original(*args)
                    seen.append(result[0]['coverage']['region_id'])
                    return result
                finally:
                    lock.release()
            region.review = guarded
            initial = execute(intake)
            first = pipeline.review(intake, initial, providers=supplied)
            self.assertEqual(seen, [r['id'] for r in plan_for(intake)['regions']])
            pipeline.dossier(intake, initial, first)

    def test_native_retries_require_shared_admission_and_one_attempt_suffices(self):
        for limits in ({'total_provider_requests': 2}, {'total_review_tokens': 2 * 8192}):
            with self.subTest(limits=limits), committee(judging={**SINGLES, 'mode': 'override'}, limits=limits) as (root, _, intake):
                barrier = threading.Barrier(2, timeout=5)
                def transport(*args):
                    barrier.wait()
                    return HttpResponse(503, b'')
                initial = execute(intake)
                with patch.dict('os.environ', {'OPENAI_API_KEY': 'offline-test'}), patch.object(HttpTransport, 'request', side_effect=transport) as calls, patch('llm_verifier.provider.time.sleep'):
                    with self.assertRaises(InfrastructureError):
                        pipeline.review(intake, initial, failure_out=root/'failure.json')
                self.assertEqual(calls.call_count, 2)
                failure = read_json(root/'failure.json')
                history = failure['data']['escalation_checkpoint']['rounds']
                self.assertEqual(sum(e['failure']['state']['provenance']['attempts'] for e in history[0]['entries'].values()), 2)
                with patch.object(HttpTransport, 'request') as calls, self.assertRaises(InfrastructureError):
                    pipeline.review(intake, initial, resume=failure)
                calls.assert_not_called()
        with committee(judging={'profile': 'builtin:math-committee-v1'}, limits={'total_provider_requests': 6}) as (root, _, intake):
            initial = execute(intake)
            def transport(body, *args):
                import json
                shape = json.loads(body)['response_format']['json_schema']['schema']['properties']
                return completion({'decision': verdict(root), 'resolutions': []} if 'resolutions' in shape else report())
            with patch.dict('os.environ', {'OPENAI_API_KEY': 'offline-test'}), patch.object(HttpTransport, 'request', side_effect=transport):
                first = pipeline.review(intake, initial)
            self.assertEqual(first['data']['provenance']['attempts'], 5)
            pipeline.dossier(intake, initial, first)

    def test_failed_wave_retains_all_successful_peers(self):
        with committee() as (root, effective, intake):
            child = effective['judging']['committee']; barrier = threading.Barrier(4, timeout=5)
            def fail():
                barrier.wait(); raise RuntimeError('private credential-like text')
            callbacks = {m['id']: (fail if m['id'] == 'proof' else barrier.wait) for m in child['members'] if m['stage'] == 'review'}
            initial = execute(intake)
            with self.assertRaises(InfrastructureError) as caught:
                pipeline.review(intake, initial, providers=providers(child, intake, verdict(root), callbacks), failure_out=root/'failure.json')
            state = caught.exception.review_state
            self.assertEqual([r['member_id'] for r in state['reviews']], ['skeptic', 'cost', 'evidence'])
            self.assertEqual(state['provenance']['attempts'], 4)
            self.assertNotIn('private credential', canonical(state).decode())
            runtime.verify_failure(state, child, effective['config'], intake['data']['manifest'],
                pipeline.evidence_refs(intake, initial), len(initial['data']['runs']), False,
                pipeline._committee_material(intake, initial, None, None), {}, True, files=intake['data']['files'])

    def test_primary_failure_preserves_audit_and_resume_calls_only_primary(self):
        with committee(judging=SINGLES) as (root, effective, intake):
            profile = effective['judging']['committee']; initial = execute(intake)
            supplied = wrapper_providers(profile, intake, verdict(root))
            supplied['primary']['reviewer'].decision = {'bad': True}
            with self.assertRaises(InfrastructureError):
                pipeline.review(intake, initial, providers=supplied, failure_out=root/'failure.json')
            failure = read_json(root/'failure.json')
            entries = failure['data']['escalation_checkpoint']['rounds'][0]['entries']
            self.assertEqual(entries['escalation']['status'], 'completed')
            supplied = wrapper_providers(profile, intake, verdict(root))
            supplied['escalation']['reviewer'].review = lambda *args: self.fail('repeated completed audit')
            first = pipeline.review(intake, initial, providers=supplied, resume=failure)
            self.assertEqual(first['data']['escalation'], entries['escalation'])
            self.assertEqual(first['data']['provenance']['attempts'], 3)
            pipeline.dossier(intake, initial, first)

    def test_immediate_primary_snapshot_is_nonresumable_until_drained(self):
        with committee(judging={**SINGLES, 'mode': 'override'}) as (root, effective, intake):
            profile = effective['judging']['committee']; initial = execute(intake)
            ready, release = threading.Event(), threading.Event()
            supplied = wrapper_providers(profile, intake, verdict(root))
            def audit(*args):
                self.assertTrue(release.wait(5)); raise RuntimeError('failed audit')
            supplied['escalation']['reviewer'].review = audit
            original = ReviewDiagnostics.write
            def persist(self, path, exc):
                original(self, path, exc)
                if getattr(self, 'checkpoint', {}).get('status') == 'in-progress' and 'primary' in self.checkpoint['completed']:
                    ready.set()
            with patch.object(ReviewDiagnostics, 'write', persist), ThreadPoolExecutor(max_workers=1) as worker:
                pending = worker.submit(pipeline.review, intake, initial, providers=supplied, failure_out=root/'failure.json')
                try:
                    self.assertTrue(ready.wait(5))
                    snapshot = read_json(root/'failure.json')
                    self.assertEqual(snapshot['data']['escalation_checkpoint']['completed']['primary']['status'], 'completed')
                    with self.assertRaisesRegex(VerifierError, 'in_progress'):
                        pipeline.review(intake, initial, providers=supplied, resume=snapshot)
                finally:
                    release.set()
                with self.assertRaises(InfrastructureError):
                    pending.result(timeout=5)
            failure = read_json(root/'failure.json')
            self.assertEqual(failure['data']['escalation_checkpoint']['status'], 'settled')
            supplied = wrapper_providers(profile, intake, verdict(root))
            supplied['primary']['reviewer'].review = lambda *args: self.fail('repeated primary')
            first = pipeline.review(intake, initial, providers=supplied, resume=failure)
            pipeline.dossier(intake, initial, first)

    def test_wall_time_is_not_sum_of_overlapping_call_durations(self):
        with committee(limits={'total_review_seconds': 5}) as (root, effective, intake):
            child = effective['judging']['committee']; initial = execute(intake)
            now = [0]
            barrier = threading.Barrier(4, action=lambda: now.__setitem__(0, 4), timeout=5)
            supplied = providers(child, intake, verdict(root), {m['id']: barrier.wait for m in child['members'] if m['stage'] == 'review'})
            for member in child['members']:
                if member['stage'] == 'review':
                    original = supplied[member['id']].review
                    def measured(*args, original=original):
                        result, provenance = original(*args)
                        provenance['elapsed_seconds'] = 4
                        return result, provenance
                    supplied[member['id']].review = measured
            with patch('time.monotonic', side_effect=lambda: now[0]):
                first = pipeline.review(intake, initial, providers=supplied)
            self.assertEqual(first['data']['provenance']['elapsed_seconds'], 4)
            pipeline.dossier(intake, initial, first)

    def test_replay_mutations_fail_before_any_resume_call(self):
        with committee(judging={**SINGLES, 'mode': 'override'}) as (root, effective, intake):
            profile = effective['judging']['committee']; initial = execute(intake)
            supplied = wrapper_providers(profile, intake, verdict(root))
            supplied['escalation']['reviewer'].decision = {'bad': True}
            with self.assertRaises(InfrastructureError):
                pipeline.review(intake, initial, providers=supplied, failure_out=root/'failure.json')
            original = read_json(root/'failure.json')
            mutations = [lambda r: r.update(elapsed_seconds=0),
                         lambda r: r['entries']['primary']['review']['provenance'].update(attempts=0),
                         lambda r: r['entries']['escalation']['failure']['state'].update(time_limit_seconds=0),
                         lambda r: r['entries']['escalation']['failure']['state']['execution'].update(offset_seconds=100),
                         lambda r: r['entries']['escalation']['failure']['state']['execution']['groups'][0]['calls'][0].update(prompt_sha256='0'*64),
                         lambda r: r.update(reused=['primary'])]
            for mutate in mutations:
                bad = copy.deepcopy(original); mutate(bad['data']['escalation_checkpoint']['rounds'][0]); bad = reseal(bad)
                with self.subTest(mutate=mutate), patch.object(ScriptedProvider, 'review') as call, self.assertRaises(VerifierError):
                    pipeline.review(intake, initial, providers=supplied, resume=bad)
                call.assert_not_called()

    def test_concurrent_diagnostics_are_bounded_across_children(self):
        with committee() as (_, _, intake):
            capture = ReviewDiagnostics(intake, [intake], 'initial', True)
            children = [ReviewDiagnostics(intake, [intake], 'initial', True, budget=capture) for _ in range(4)]
            observers = [child.start(str(i), 'review', {}, [], {}) for i, child in enumerate(children)]
            barrier = threading.Barrier(4, timeout=5)
            def fill(observe):
                barrier.wait()
                for _ in range(100):
                    observe({'response': {'outputs': ['x'*100000], 'usage': {}}})
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(fill, observers))
            self.assertLessEqual(capture.bytes, MAX_CAPTURE)
            self.assertLessEqual(capture.events, MAX_EVENTS)
            self.assertEqual(sum(len(c['events']) for child in children for c in child.calls), capture.events)
            self.assertTrue(capture.truncated)

    def test_legacy_serial_committee_artifact_still_replays(self):
        with committee() as (root, effective, intake):
            child = effective['judging']['committee']; initial = execute(intake)
            value, _ = runtime.run(child, effective['config'], intake['data']['manifest'],
                pipeline.evidence_refs(intake, initial), len(initial['data']['runs']), False,
                pipeline._committee_material(intake, initial, None, None), {}, providers=providers(child, intake, verdict(root)), serial=True)
            self.assertNotIn('execution', value)
            runtime.verify(value, child, effective['config'], intake['data']['manifest'],
                pipeline.evidence_refs(intake, initial), len(initial['data']['runs']), False,
                pipeline._committee_material(intake, initial, None, None), {}, True,
                execution_version=runtime.SERIAL_EXECUTION)
            first = seal('initial_review', intake['bindings'], value, [intake['sha256'], initial['sha256']], True)
            with self.assertRaisesRegex(VerifierError, 'committee_execution_version'):
                pipeline.dossier(intake, initial, first)


    def test_native_retry_receipts_reject_refund_and_hidden_peer(self):
        from llm_verifier.provider import OpenAI
        class Transport:
            def __init__(self): self.count = 0
            def request(self, *args):
                self.count += 1
                return HttpResponse(503 if self.count == 1 else 401, b'')
        with committee(judging=SINGLES) as (root, effective, intake):
            profile = effective['judging']['committee']; initial = execute(intake)
            supplied = wrapper_providers(profile, intake, verdict(root))
            transport = Transport()
            supplied['escalation']['reviewer'] = OpenAI(profile['escalation']['members'][0]['provider'],
                transport=transport, api_key='offline-only', sleeper=lambda _: None)
            first = pipeline.review(intake, initial, providers=supplied)
            self.assertEqual(transport.count, 2)
            for edit_receipt in (False, True):
                bad = copy.deepcopy(first); state = bad['data']['escalation']['failure']['state']
                state['execution']['groups'][0]['calls'][0]['attempts'] = 1
                if edit_receipt:
                    for calls in (state['calls'], bad['data']['escalation']['failure']['calls']):
                        calls[0]['outcome']['attempts'] = 1
                for value in (state['provenance'], bad['data']['provenance']):
                    value['attempts'] -= 1; value['allotted_completion_tokens'] -= 8192
                with self.subTest(edit_receipt=edit_receipt), self.assertRaises(VerifierError):
                    pipeline.verify_initial_review(intake, initial, reseal(bad))
        spec = {**SINGLES, 'escalation': {'profile': 'builtin:math-committee-v1'}}
        with committee(judging=spec) as (root, effective, intake):
            profile = effective['judging']['committee']; initial = execute(intake)
            supplied = wrapper_providers(profile, intake, verdict(root))
            def fail(*args): raise InfrastructureError('provider_timeout')
            supplied['escalation']['proof'].review = fail
            first = pipeline.review(intake, initial, providers=supplied)
            for delete in (True, False):
                bad = copy.deepcopy(first); state = bad['data']['escalation']['failure']['state']
                if delete:
                    state['reviews'] = [r for r in state['reviews'] if r['member_id'] != 'skeptic']
                state['execution']['groups'][0]['calls'][1]['error'] = 'provider_timeout'
                with self.subTest(delete=delete), self.assertRaises(VerifierError):
                    pipeline.verify_initial_review(intake, initial, reseal(bad))

    def test_selective_repeated_resume_in_both_modes_and_directions(self):
        for mode in ('record-only', 'override'):
            for failed_name in ('primary', 'escalation'):
                with self.subTest(mode=mode, failed=failed_name), committee(judging={**SINGLES, 'mode': mode}) as (root, effective, intake):
                    profile = effective['judging']['committee']; initial = execute(intake)
                    supplied = wrapper_providers(profile, intake, verdict(root))
                    supplied[failed_name]['reviewer'].decision = {'invalid': True}
                    def attempt(resume=None):
                        try:
                            pipeline.review(intake, initial, providers=supplied, resume=resume, failure_out=root/'failure.json')
                        except InfrastructureError:
                            pass
                        return read_json(root/'failure.json')
                    first = attempt()
                    retained = 'escalation' if failed_name == 'primary' else 'primary'
                    completed = first['data']['escalation_checkpoint']['rounds'][0]['entries'][retained]
                    supplied[retained]['reviewer'].review = lambda *args: self.fail('repeated completed child')
                    second = attempt(first)
                    supplied[failed_name]['reviewer'].decision = verdict(root)
                    result = pipeline.review(intake, initial, providers=supplied, resume=second)
                    self.assertEqual(result['data'][retained], completed)
                    self.assertEqual(result['data']['provenance']['attempts'], 4)
                    self.assertEqual(len(result['data']['recovery']), 2)
                    pipeline.dossier(intake, initial, result)

    def test_pre_dispatch_material_failures_replay_without_calls(self):
        with committee() as (root, effective, intake):
            child = effective['judging']['committee']; initial = execute(intake)
            def material(*args): raise InfrastructureError('full_evidence_context_budget')
            args = (child, effective['config'], intake['data']['manifest'], pipeline.evidence_refs(intake, initial),
                    len(initial['data']['runs']), False, material, {})
            with self.assertRaises(InfrastructureError) as caught:
                scheduler.run(*args, providers=providers(child, intake, verdict(root)))
            state = caught.exception.review_state
            self.assertEqual(state['provenance']['attempts'], 0)
            scheduler.verify(state, *args, True, failed=True)

    def test_composition_seal_and_verification_failure_keep_both_outputs(self):
        from llm_verifier import parallel_escalation
        from llm_verifier.runner import DockerRunner, output_record
        for final in (False, True):
            for boundary in ('composition', 'seal', 'verify'):
                with self.subTest(final=final, boundary=boundary), committee('computation', judging=SINGLES) as (root, effective, intake):
                    profile = effective['judging']['committee']; initial = execute(intake)
                    script = read_json(root/'simulation.json'); first = followup = None
                    if final:
                        first = pipeline.review(intake, initial, providers=wrapper_providers(profile, intake, script['initial_decision']))
                        with patch.object(DockerRunner, 'run', return_value=output_record('completed', 0, b'{"checked_n":25}', b'')):
                            followup = pipeline.execute(intake, first, initial)
                    decision = script['final_decision'] if final else script['initial_decision']
                    supplied = wrapper_providers(profile, intake, decision)
                    target = ('llm_verifier.parallel_escalation.legacy._result' if boundary == 'composition' else
                              'llm_verifier.pipeline.seal' if boundary == 'seal' else 'llm_verifier.pipeline._verify_review')
                    original = (parallel_escalation.legacy._result if boundary == 'composition' else
                                pipeline.seal if boundary == 'seal' else pipeline._verify_review)
                    def fail_after_children(*args, **kwargs):
                        # Initial-review replay on entry to a final phase remains valid.
                        if final and ((boundary == 'verify' and args[2]['stage'] == 'initial_review') or
                                      (boundary == 'composition' and args[-1] == len(initial['data']['runs']))):
                            return original(*args, **kwargs)
                        raise VerifierError('post_join_failure')
                    with patch(target, side_effect=fail_after_children), self.assertRaises(VerifierError):
                        pipeline.review(intake, initial, first, followup, providers=supplied, failure_out=root/'failure.json')
                    saved = read_json(root/'failure.json')['data']['escalation_checkpoint']
                    self.assertEqual(saved['status'], 'in-progress')
                    self.assertEqual(set(saved['completed']), {'primary', 'escalation'})
                    self.assertEqual(saved['reserved_attempts'], 2)
                    self.assertTrue(all(e['status'] == 'completed' for e in saved['completed'].values()))

    def test_cli_output_failure_keeps_initial_and_final_checkpoints(self):
        from llm_verifier import cli
        from llm_verifier.files import write_json
        from llm_verifier.runner import DockerRunner, output_record
        for final in (False, True):
            with self.subTest(final=final), committee('computation', judging=SINGLES) as (root, effective, intake):
                profile = effective['judging']['committee']; initial = execute(intake)
                script = read_json(root/'simulation.json')
                write_json(root/'intake.json', intake); write_json(root/'initial.json', initial)
                args = ['review-final' if final else 'review-initial', '--intake', str(root/'intake.json'),
                        '--initial', str(root/'initial.json'), '--out', str(root/'review.json'), '--failure-out', str(root/'failure.json')]
                if final:
                    # Use native fake transport so the CLI does not downgrade simulation.
                    with patch.dict('os.environ', {'OPENAI_API_KEY': 'offline-only'}), patch.object(HttpTransport, 'request', return_value=completion(script['initial_decision'])):
                        first = pipeline.review(intake, initial)
                    with patch.object(DockerRunner, 'run', return_value=output_record('completed', 0, b'{"checked_n":25}', b'')):
                        followup = pipeline.execute(intake, first, initial)
                    write_json(root/'first.json', first); write_json(root/'followup.json', followup)
                    args += ['--initial-review', str(root/'first.json'), '--followup', str(root/'followup.json')]
                decision = script['final_decision'] if final else script['initial_decision']
                with patch.dict('os.environ', {'OPENAI_API_KEY': 'offline-only'}), patch.object(HttpTransport, 'request', return_value=completion(decision)), patch.object(cli, 'write_json', side_effect=OSError('disk full')):
                    self.assertEqual(cli.main(args), 2)
                saved = read_json(root/'failure.json')['data']['escalation_checkpoint']
                self.assertEqual(set(saved['completed']), {'primary', 'escalation'})
                self.assertEqual(saved['reserved_attempts'], 2)
                self.assertFalse((root/'review.json').exists())


    def test_settled_checkpoint_charges_composition_time_on_resume(self):
        from llm_verifier import escalation
        with committee(judging=SINGLES) as (root, effective, intake):
            profile = effective['judging']['committee']; initial = execute(intake)
            supplied = wrapper_providers(profile, intake, verdict(root))
            def fail(*args): raise InfrastructureError('provider_timeout')
            supplied['escalation']['reviewer'].review = fail
            clock = [0.0]; advanced = [False]; original = escalation._result
            def compose(*args, **kwargs):
                result = original(*args, **kwargs)
                if not advanced[0]:
                    advanced[0] = True; clock[0] += 50
                return result
            with patch('time.monotonic', side_effect=lambda: clock[0]), patch.object(escalation, '_result', side_effect=compose):
                first = pipeline.review(intake, initial, providers=supplied, failure_out=root/'failure.json')
            failure = read_json(root/'failure.json')
            self.assertEqual(first['data']['provenance']['elapsed_seconds'], 50)
            self.assertEqual(failure['data']['escalation_checkpoint']['rounds'][0]['elapsed_seconds'], 50)
            supplied = wrapper_providers(profile, intake, verdict(root))
            offers = []; original = supplied['escalation']['reviewer'].review
            def retry(messages, schema, seconds, requests):
                offers.append(seconds)
                return original(messages, schema, seconds, requests)
            supplied['escalation']['reviewer'].review = retry
            with patch('time.monotonic', side_effect=lambda: clock[0]):
                result = pipeline.review(intake, initial, providers=supplied, resume=failure)
            self.assertEqual(offers, [1150])
            self.assertEqual(result['data']['provenance']['elapsed_seconds'], 50)

if __name__ == '__main__':
    unittest.main()
