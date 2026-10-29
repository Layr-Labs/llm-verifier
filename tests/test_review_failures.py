"""Retained failures remain diagnostic evidence, never replayable judgments."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from llm_verifier import background, pipeline
from llm_verifier.artifacts import intake_snapshot, seal, verify_envelope
from llm_verifier.cli import main
from llm_verifier.diagnostics import MAX_CAPTURE, MAX_TEXT, ReviewDiagnostics, response_capture
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical, loads, sha
from llm_verifier.provider import HttpResponse, HttpTransport, OpenAI
from llm_verifier.simulation import ScriptedProvider
from tests.support import request_parts, completion, example, scripted_model
from tests.test_background import native, response
from tests.test_committees import committee, report
from tests.test_regions import PROFILE, outputs, plan_for


def cli_review(root, intake, initial, *, final=None, extra=(), failure=True):
    for name, value in (("intake", intake), ("initial", initial)):
        write_json(root / (name + ".json"), value)
    args = ["review-final" if final else "review-initial", "--intake", str(root / "intake.json"),
            "--initial", str(root / "initial.json"), "--out", str(root / "review.json")]
    if failure:
        args += ["--failure-out", str(root / "failure.json")]
    if final:
        for name, value in zip(("first", "followup"), final):
            write_json(root / (name + ".json"), value)
        args += ["--initial-review", str(root / "first.json"), "--followup", str(root / "followup.json")]
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(args + list(extra))
    return code, stdout.getvalue(), stderr.getvalue()


class ReviewFailures(unittest.TestCase):
    def test_planner_semantic_failure_keeps_actual_output_usage_and_intake_binding(self):
        with committee(judging=PROFILE) as (root, _, intake):
            initial = pipeline.execute(intake)
            raw = '{"regions":[]}'
            envelope = loads(completion({}).body)
            envelope['choices'][0]['message']['content'] = raw
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}), patch.object(
                    HttpTransport, 'request', return_value=HttpResponse(200, canonical(envelope))) as http:
                code, stdout, stderr = cli_review(root, intake, initial)
            self.assertEqual(code, 2)
            self.assertIn('committee_invalid_review_region_plan_count', stderr)
            self.assertNotIn(raw, stdout + stderr)
            http.assert_called_once()
            self.assertFalse((root / 'review.json').exists())
            failure = read_json(root / 'failure.json')
            data = verify_envelope(failure, 'review_failure', intake, [intake, initial])
            self.assertFalse(data['publishable'])
            self.assertFalse(failure['simulation'])
            self.assertEqual(data['phase'], 'initial')
            self.assertEqual(data['calls'][0]['stage'], 'planning')
            event = data['calls'][0]['events'][0]
            self.assertEqual(event['response']['outputs'], [raw])
            self.assertEqual(event['response']['usage']['total_tokens'], 150)
            self.assertEqual(event['response']['id'], 'chatcmpl-test')
            self.assertGreaterEqual(event['elapsed_seconds'], 0)
            self.assertEqual(event['attempt'], 1)
            self.assertNotIn('decision', data)
            for use in (lambda: pipeline.verify_initial_review(intake, initial, failure),
                        lambda: pipeline.dossier(intake, initial, failure),
                        lambda: pipeline.export_results(failure),
                        lambda: pipeline.execute(intake, failure, initial)):
                with self.assertRaises(VerifierError): use()
            for path in ('score.json', 'dossier.json', 'accepted.json'):
                write_json(root / path, {'stale': True})
            with self.assertRaises(VerifierError):
                pipeline.finalize(intake, initial, failure, root / 'score.json', root / 'dossier.json',
                                  results_path=root / 'accepted.json')
            self.assertFalse((root / 'score.json').exists())
            self.assertFalse((root / 'accepted.json').exists())
            relabeled = seal('initial_review', failure['bindings'], failure['data'], failure['parents'])
            with self.assertRaises(VerifierError): pipeline.dossier(intake, initial, relabeled)

    def test_parse_failures_preserve_raw_assistant_text_and_independent_numeric_usage(self):
        for mode in ('invalid_json', 'length', 'metadata', 'usage'):
            with self.subTest(mode=mode), example() as (root, _, intake):
                initial = pipeline.execute(intake)
                value = loads(completion({}).body)
                value['choices'][0]['message']['content'] = '{"raw":"secret-key",'
                value['private'] = 'PRIVATE-METADATA'
                value['usage']['private'] = 'PRIVATE-METADATA'
                if mode == 'length': value['choices'][0]['finish_reason'] = 'length'
                if mode == 'metadata':
                    value['choices'][0]['message']['content'] = '{"raw":"secret-key"}'
                    value['model'] = 'bad/model'
                if mode == 'usage':
                    value['choices'][0]['message']['content'] = '{}'
                    value['usage']['completion_tokens'] = 'private'
                with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret-key'}), patch.object(
                        HttpTransport, 'request', return_value=HttpResponse(200, canonical(value))) as http:
                    code, stdout, stderr = cli_review(root, intake, initial)
                self.assertEqual(code, 2)
                http.assert_called_once()
                data = read_json(root / 'failure.json')['data']
                event = data['calls'][0]['events'][0]
                self.assertEqual(event['response']['usage']['prompt_tokens'], 100)
                self.assertEqual(event['response']['usage']['total_tokens'], 150)
                if mode != 'usage': self.assertIn('[REDACTED]', event['response']['outputs'][0])
                text = (root / 'failure.json').read_text() + stdout + stderr
                self.assertNotIn('secret-key', text)
                self.assertNotIn('PRIVATE-METADATA', text)

    def test_transport_failure_keeps_completed_calls_and_sanitized_failed_attempts(self):
        with committee(judging=PROFILE) as (root, _, intake):
            initial = pipeline.execute(intake)
            plan = plan_for(intake)
            attempts = []
            def transport(body, *args):
                _, member, context = request_parts(body)
                if member['stage'] == 'planning': return completion(plan)
                if member['stage'] == 'review': return completion(report())
                if context['region']['id'] == plan['regions'][0]['id']:
                    attempts.append(1)
                    if len(attempts) == 1: raise RuntimeError('Authorization: secret-key')
                    return HttpResponse(500, b'provider secret-key')
                from tests.test_regions import region_report
                return completion(region_report(context['region']))
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret-key'}), patch.object(
                    HttpTransport, 'request', side_effect=transport) as http, patch('llm_verifier.provider.time.sleep'):
                code, stdout, stderr = cli_review(root, intake, initial)
            self.assertEqual(code, 2)
            self.assertEqual(http.call_count, 5)
            failure = read_json(root / 'failure.json')
            calls = failure['data']['calls']
            self.assertEqual([r['stage'] for r in calls], ['planning', 'review', 'region', 'region'])
            self.assertEqual(json.loads(calls[0]['events'][0]['response']['outputs'][0]), plan)
            self.assertEqual(calls[0]['events'][0]['response']['usage']['total_tokens'], 150)
            self.assertEqual(calls[1]['events'][0]['response']['usage']['total_tokens'], 150)
            self.assertEqual([v['attempt'] for v in calls[2]['events'] if 'attempt' in v], [1, 2])
            self.assertEqual(calls[2]['events'][0]['error'], 'provider_transport_failed')
            self.assertNotIn('secret-key', (root / 'failure.json').read_text() + stdout + stderr)
            self.assertFalse((root / 'review.json').exists())

    def test_background_failed_response_keeps_output_usage_without_reasoning(self):
        value = native('incomplete')
        value['incomplete_details'] = {'reason': 'max_output_tokens'}
        value['output'] = [{'type': 'reasoning', 'summary': 'PRIVATE-REASONING'}, {
            'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"partial":'}]}]
        with example(mutate=lambda c: c['provider'].update(background=True)) as (root, _, intake):
            initial = pipeline.execute(intake)
            def transport(body, key, timeout, limit):
                request = Mock(side_effect=[response(value), HttpResponse(200, b'{}')])
                return HttpResponse(*background.request_background(body, key, timeout, limit, request))
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}), patch.object(HttpTransport, 'request', side_effect=transport):
                code, _, _ = cli_review(root, intake, initial)
            self.assertEqual(code, 2)
            text = (root / 'failure.json').read_text()
            self.assertNotIn('PRIVATE-REASONING', text)
            event = read_json(root / 'failure.json')['data']['calls'][0]['events'][0]
            self.assertEqual(event['response']['outputs'], ['{"partial":'])
            self.assertEqual(event['response']['usage']['total_tokens'], 150)

    def test_final_phase_failure_binds_all_original_parent_checkpoints(self):
        from llm_verifier.runner import DockerRunner, output_record
        with example('computation') as (root, _, intake):
            script = read_json(root / 'simulation.json')
            with patch.object(DockerRunner, 'run', return_value=output_record(
                    'completed', 0, b'{"checked_n":25}', b'')):
                initial = pipeline.execute(intake)
                with scripted_model([script['initial_decision']]):
                    first = pipeline.review(intake, initial)
                followup = pipeline.execute(intake, first, initial)
            with scripted_model([{'invalid': True}]):
                code, _, _ = cli_review(root, intake, initial, final=(first, followup))
            self.assertEqual(code, 2)
            failure = read_json(root / 'failure.json')
            data = verify_envelope(failure, 'review_failure', intake, [intake, initial, first, followup])
            self.assertEqual(data['phase'], 'final')
            self.assertEqual(data['calls'][0]['events'][0]['response']['usage']['total_tokens'], 150)
            with self.assertRaises(VerifierError):
                pipeline.dossier(intake, initial, first, followup, failure)

    def test_private_worker_preserves_native_failure_and_redacts_credential(self):
        import llm_verifier.provider as module
        # Execute the real private-pipe protocol, substituting only its HTTP
        # boundary. No socket, model, or Docker call is made.
        bootstrap = """import sys
sys.path.insert(0, sys.argv[1])
from llm_verifier import provider as p
from llm_verifier.jsonutil import canonical
value = {"object":"response","id":"resp_test","model":"test-model","status":"incomplete",
         "incomplete_details":{"reason":"max_output_tokens"}, "error":None,
         "output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"raw secret-" + chr(92) + "u006bey"}]}],
         "usage":{"input_tokens":12,"output_tokens":34,"total_tokens":46}}
p.HttpTransport._request_in_child = lambda *a, **k: p.HttpResponse(200, canonical(value))
p._http_worker()
"""
        with example(mutate=lambda c: c['provider'].update(background=True)) as (root, _, intake):
            initial = pipeline.execute(intake)
            with patch.object(module, '_CHILD_BOOTSTRAP', bootstrap), patch.dict(
                    os.environ, {'OPENAI_API_KEY': 'secret-key'}):
                code, _, stderr = cli_review(root, intake, initial)
            self.assertEqual(code, 2)
            self.assertIn('provider_response_invalid_output_token_limit', stderr)
            event = read_json(root / 'failure.json')['data']['calls'][0]['events'][0]
            self.assertEqual(event['response']['outputs'], ['raw [REDACTED]'])
            self.assertEqual(event['response']['usage']['total_tokens'], 46)
            self.assertNotIn('secret-key', (root / 'failure.json').read_text())

    def test_background_malformed_envelope_capture_precedes_native_parser(self):
        transport = Mock(return_value=HttpResponse(200, b'{secret-key'))
        with self.assertRaises(InfrastructureError) as failure:
            background.request_background(b'{}', 'secret-key', 60, 4096, transport)
        # Redaction also happens at the final parent-side boundary.
        captured = failure.exception.response_capture
        self.assertTrue(captured['unparsed_body_omitted'])
        self.assertEqual(captured['redacted_body_sha256'], sha(b'{[REDACTED]'))
        events = []
        with example(mutate=lambda c: c['provider'].update(background=True)) as (_, effective, _):
            fake = Mock(); fake.request.side_effect = failure.exception
            provider = OpenAI(effective['config']['provider'], transport=fake, api_key='secret-key')
            with self.assertRaises(InfrastructureError): provider.review([], {}, 60, 1, observer=events.append)
        self.assertTrue(events[0]['response']['unparsed_body_omitted'])

    def test_malformed_background_poll_preserves_failed_body_not_previous_status(self):
        request = Mock(side_effect=[response(native('queued')), HttpResponse(200, b'{failed-poll secret-key'),
                                    HttpResponse(200, b'{}'), HttpResponse(200, b'{}')])
        with self.assertRaises(InfrastructureError) as failure:
            background.request_background(b'{}', 'secret-key', 60, 4096, request, sleeper=lambda _: None)
        captured = failure.exception.response_capture
        self.assertTrue(captured['unparsed_body_omitted'])
        self.assertEqual(captured['redacted_body_sha256'], sha(b'{failed-poll [REDACTED]'))
        self.assertNotIn('id', captured)
        self.assertEqual([c.kwargs['method'] for c in request.call_args_list], ['POST', 'GET', 'POST', 'DELETE'])

    def test_valid_output_bytes_unchanged_and_failure_file_cleared(self):
        for profile in (None, {'profile': 'builtin:single-v1'}, PROFILE):
            with self.subTest(profile=profile), example(mutate=lambda c: c.update(
                    **({'judging': profile} if profile else {}))) as (root, _, intake):
                initial = pipeline.execute(intake)
                decision = read_json(root / 'simulation.json')['initial_decision']
                values = outputs(plan_for(intake), decision) if profile == PROFILE else [decision]
                with scripted_model(values), patch('llm_verifier.provider.time.monotonic', return_value=0), patch(
                        'llm_verifier.committee.time.monotonic', return_value=0):
                    expected = pipeline.review(intake, initial)
                write_json(root / 'failure.json', {'stale': True})
                with scripted_model(values), patch('llm_verifier.provider.time.monotonic', return_value=0), patch(
                        'llm_verifier.committee.time.monotonic', return_value=0):
                    code, _, _ = cli_review(root, intake, initial)
                self.assertEqual(code, 0)
                self.assertEqual(canonical(read_json(root / 'review.json')), canonical(expected))
                self.assertFalse((root / 'failure.json').exists())

    def test_injected_failed_provider_is_always_simulation(self):
        with example() as (root, effective, intake):
            initial = pipeline.execute(intake)
            provider = ScriptedProvider(effective['config']['provider'], {'invalid': True})
            with self.assertRaises(VerifierError):
                pipeline.review(intake, initial, provider=provider, failure_out=root / 'failure.json')
            self.assertTrue(read_json(root / 'failure.json')['simulation'])
        with committee(judging=PROFILE) as (root, effective, intake):
            initial = pipeline.execute(intake)
            providers = {m['id']: ScriptedProvider(m['provider'], {'regions': []})
                         for m in effective['judging']['committee']['members']}
            with self.assertRaises(VerifierError):
                pipeline.review(intake, initial, providers=providers, failure_out=root / 'failure.json')
            self.assertTrue(read_json(root / 'failure.json')['simulation'])

    def test_aliases_symlink_parents_and_wrong_intake_stop_before_calls(self):
        with example() as (root, _, intake):
            initial = pipeline.execute(intake)
            for kind in ('same_input', 'hardlink', 'same_output', 'symlink_parent', 'symlink_missing_parent', 'wrong_intake'):
                for name, value in (('intake', intake), ('initial', initial)):
                    write_json(root / (name + '.json'), value)
                before = (root / 'intake.json').read_bytes()
                if kind == 'same_input': extra = ['--failure-out', str(root / 'intake.json')]
                elif kind == 'hardlink':
                    alias = root / 'alias.json'; os.link(root / 'intake.json', alias)
                    extra = ['--failure-out', str(alias)]
                elif kind == 'same_output': extra = ['--failure-out', str(root / 'review.json')]
                elif kind == 'symlink_parent':
                    (root / 'link').symlink_to(root, target_is_directory=True)
                    extra = ['--failure-out', str(root / 'link/failure.json')]
                elif kind == 'symlink_missing_parent':
                    (root / 'link-missing').symlink_to(root, target_is_directory=True)
                    extra = ['--failure-out', str(root / 'link-missing/missing/failure.json')]
                else: extra = ['--expected-intake', '0' * 64]
                # Do not replace input files between alias construction and the CLI.
                args = ['review-initial', '--intake', str(root / 'intake.json'), '--initial', str(root / 'initial.json'),
                        '--out', str(root / 'review.json')] + extra
                with self.subTest(kind=kind), patch.object(HttpTransport, 'request') as http, redirect_stderr(io.StringIO()):
                    self.assertEqual(main(args), 2)
                    http.assert_not_called()
                self.assertEqual((root / 'intake.json').read_bytes(), before)
                self.assertFalse((root / 'review.json').exists())
                if kind == 'hardlink': alias.unlink()

    def test_default_cli_failure_path_and_malformed_envelope_redaction(self):
        with example() as (root, _, intake):
            initial = pipeline.execute(intake)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret-key'}), patch.object(
                    HttpTransport, 'request', return_value=HttpResponse(200, b'{secret-key')):
                code, _, _ = cli_review(root, intake, initial, failure=False)
            self.assertEqual(code, 2)
            event = read_json(root / 'review.json.failure.json')['data']['calls'][0]['events'][0]
            self.assertTrue(event['response']['unparsed_body_omitted'])
            self.assertEqual(event['response']['redacted_body_sha256'], sha(b'{[REDACTED]'))

    def test_escaped_credentials_and_malformed_envelope_never_retain_private_metadata(self):
        from llm_verifier.diagnostics import redact
        for key in ('secret-key', 'secret"key', r'secret\key'):
            encoded = ''.join('\\u%04x' % ord(c) for c in key)
            # Both literal and JSON-escaped key characters are redacted.
            self.assertEqual(redact(key, key), '[REDACTED]')
            self.assertEqual(redact(encoded.replace('\\\\u', '\\u'), key), '[REDACTED]')
        body = b'{"authorization":"secret-\\u006bey","reasoning":"PRIVATE-REASONING","private":"SECRET-METADATA",'
        capture = response_capture(body, 'secret-key')
        self.assertTrue(capture['unparsed_body_omitted'])
        self.assertNotIn('PRIVATE-REASONING', canonical(capture).decode())
        self.assertNotIn('SECRET-METADATA', canonical(capture).decode())
        self.assertNotIn('006b', canonical(capture).decode())
        value = loads(completion({}).body)
        value['choices'][0]['message']['content'] = '{"result":"secret-\\u006bey"}'
        self.assertEqual(response_capture(canonical(value), 'secret-key')['outputs'], ['{"result":"[REDACTED]"}'])

    def test_actual_http_reader_overflow_retains_completed_output_and_usage(self):
        body = completion({'raw': 'secret-key'}).body
        reply = Mock(status=200); reply.read1.return_value = body
        conn = Mock(); conn.getresponse.return_value = reply
        with patch('http.client.HTTPSConnection', return_value=conn), self.assertRaises(InfrastructureError) as error:
            HttpTransport()._request_in_child(b'{}', 'secret-key', 60, len(body) - 1)
        self.assertEqual(str(error.exception), 'provider_response_limit')
        capture = error.exception.response_capture
        self.assertEqual(capture['usage']['total_tokens'], 150)
        self.assertEqual(json.loads(capture['outputs'][0]), {'raw': '[REDACTED]'})
        conn.close.assert_called_once()

    def test_private_real_http_reader_limit_and_native_malformed_envelope(self):
        import http.server
        import threading
        import llm_verifier.provider as module
        for native_mode in (False, True):
            body = (b'{"private":"secret-key","reasoning":"PRIVATE-REASONING",' if native_mode
                    else completion({'raw': 'secret-key', 'padding': 'x' * 2000}).body)
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_POST(self):
                    self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            bootstrap = ("import sys,http.client;sys.path.insert(0,sys.argv[1]);"
                         "from llm_verifier import provider as p;"
                         f"p.http.client.HTTPSConnection=lambda host,timeout:http.client.HTTPConnection('127.0.0.1',{server.server_port},timeout=timeout);"
                         "p._http_worker()")
            try:
                def configure(config):
                    config['provider'].update(background=native_mode,
                        max_response_bytes=4096 if native_mode else len(body) - 1)
                with self.subTest(native=native_mode), example(mutate=configure) as (root, _, intake):
                    initial = pipeline.execute(intake)
                    with patch.object(module, '_CHILD_BOOTSTRAP', bootstrap), patch.dict(
                            os.environ, {'OPENAI_API_KEY': 'secret-key'}):
                        code, stdout, stderr = cli_review(root, intake, initial)
                    self.assertEqual(code, 2)
                    saved = (root / 'failure.json').read_text()
                    self.assertNotIn('secret-key', saved + stdout + stderr)
                    self.assertNotIn('PRIVATE-REASONING', saved)
                    event = read_json(root / 'failure.json')['data']['calls'][0]['events'][0]
                    if native_mode:
                        self.assertEqual(event['error'], 'provider_background_invalid_envelope')
                        self.assertTrue(event['response']['unparsed_body_omitted'])
                        self.assertEqual(event['response']['body_bytes'], len(body))
                    else:
                        self.assertEqual(event['error'], 'provider_response_limit')
                        self.assertEqual(event['response']['usage']['total_tokens'], 150)
                        self.assertEqual(json.loads(event['response']['outputs'][0])['raw'], '[REDACTED]')
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_incomplete_region_keeps_static_category_and_call_identity(self):
        from tests.test_regions import region_report
        with committee(judging=PROFILE) as (root, _, intake):
            initial = pipeline.execute(intake)
            plan = plan_for(intake)
            region = region_report(plan['regions'][0]); region['coverage']['complete'] = False
            with scripted_model([plan, report(), region]):
                code, _, stderr = cli_review(root, intake, initial)
            self.assertEqual(code, 2)
            self.assertIn('committee_invalid_review_region_review_incomplete', stderr)
            failure = read_json(root / 'failure.json')['data']
            self.assertEqual(failure['error'], 'committee_invalid_review_region_review_incomplete')
            self.assertEqual(failure['calls'][2]['region_id'], plan['regions'][0]['id'])

    def test_workflow_retains_failure_artifacts_separately_including_failed_audits(self):
        from tests.support import REPO
        workflow = (REPO / '.github/workflows/verify.yml').read_text()
        for phase, next_job, folder in [('initial', 'followup', 'review-failure'),
                                      ('final', 'finalize', 'final-review-failure')]:
            job = workflow.split('  ' + phase + '_review:', 1)[1].split('  ' + next_job + ':', 1)[0]
            self.assertNotIn('actions/checkout', job)
            self.assertIn('--failure-out "$RUNNER_TEMP/' + folder + '/failure.json"', job)
            self.assertIn("if: ${{ always() }}", job)
            self.assertNotIn("steps.review.outcome == 'failure'", job)
            self.assertIn('name: llmv-' + phase + '-review-failure', job)
            self.assertIn('if-no-files-found: ignore', job)
            self.assertEqual(job.count('id: review'), 1)

    def test_all_cardinality_capture_limits_mark_truncation(self):
        cases = []
        value = native()
        value['output'][1]['content'] = [{'type': 'output_text', 'text': str(i)} for i in range(17)]
        cases.append(value)
        value = native(); value['output'] = [value['output'][1]] * 33
        cases.append(value)
        value = loads(completion({}).body); value['choices'] *= 17
        cases.append(value)
        for value in cases:
            with self.subTest(value=value):
                captured = response_capture(canonical(value))
                self.assertTrue(captured['truncated'])
                self.assertLessEqual(len(captured['outputs']), 16)

    def test_capture_bounds_include_escaping_and_omit_arbitrary_metadata(self):
        body = completion({'text': '\U0001f600' * MAX_TEXT}).body
        captured = response_capture(body)
        self.assertTrue(captured['truncated'])
        self.assertLess(len(canonical(captured)), MAX_TEXT + 1024)
        with example() as (_, _, intake):
            capture = ReviewDiagnostics(intake, [intake], 'initial', False)
            observe = capture.start('single', 'review', {}, [], {})
            for _ in range(100): observe({'response': captured})
            self.assertTrue(capture.truncated)
            self.assertLess(len(canonical(capture.calls)), MAX_CAPTURE + 65536)
            self.assertEqual(capture.calls[0]['events'][-1]['response']['usage']['total_tokens'], 150)


if __name__ == '__main__':
    unittest.main()
