import copy
import json
import time
import unittest
from unittest.mock import Mock, patch

from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.jsonutil import canonical, digest, loads
from llm_verifier.provider import ENDPOINT, HttpResponse, HttpTransport, OpenAI, parse_response
from tests.support import completion, example


class Provider(unittest.TestCase):
    def test_truncation_refusal_tools_ambiguous_and_malformed(self):
        baseline=loads(completion({'kind':'judgment'}).body)
        variants=[]
        for reason in ('length','tool_calls','function_call','content_filter','unknown',None):
            data=copy.deepcopy(baseline);data['choices'][0]['finish_reason']=reason;variants.append(data)
        for field,value in [('refusal','No'),('refusal',''),('refusal',False),('content',None),('content',[]),('tool_calls',[{'id':'x'}]),('function_call',{'name':'f'}),('role','user'),('content','```json\n{}\n```'),('content','{"x":1,"x":2}'),('content','NaN'),('content','{"n":1e999}')]:
            data=copy.deepcopy(baseline);data['choices'][0]['message'][field]=value;variants.append(data)
        data=copy.deepcopy(baseline);data['choices']*=2;variants.append(data)
        data=copy.deepcopy(baseline);data['object']='chat.completion.chunk';variants.append(data)
        data=copy.deepcopy(baseline);data['error']={'message':'SECRET'};variants.append(data)
        data=copy.deepcopy(baseline);data['choices'][0]['error']={'message':'SECRET'};variants.append(data)
        for payload in variants:
            with self.subTest(payload=payload),self.assertRaisesRegex(InfrastructureError,'provider_response_invalid'):
                parse_response(HttpResponse(200,canonical(payload)),'test-model')

    def test_transport_error_retry_limits_and_redaction(self):
        with example() as (_,effective,_):
            config=effective['config']['provider']
            for side in ([RuntimeError('Authorization: SUPER-SECRET')]*2,[HttpResponse(500,b'SUPER-SECRET')]*2,[HttpResponse(401,b'SUPER-SECRET')]):
                transport=Mock();transport.request.side_effect=side
                provider=OpenAI(config,transport=transport,api_key='SUPER-SECRET',sleeper=lambda _:None)
                with self.assertRaises(InfrastructureError) as error:provider.review([],{},100,4)
                self.assertNotIn('SUPER-SECRET',str(error.exception))
                self.assertEqual(transport.request.call_count,len(side))

    def test_valid_response_rejection_is_not_retried(self):
        with example() as (_,effective,_):
            decision={'kind':'judgment','judgment':{'valid':False},'requests':[]}
            transport=Mock();transport.request.return_value=completion(decision)
            provider=OpenAI(effective['config']['provider'],transport=transport,api_key='dummy')
            got,provenance=provider.review([{'role':'system','content':'rules'}],{},100,4)
            self.assertEqual(got,decision)
            self.assertEqual(transport.request.call_count,1)
            self.assertEqual(provenance['usage']['total_tokens'],150)
            request=loads(transport.request.call_args.args[0])
            self.assertEqual(request['model'],'test-model')
            self.assertEqual(request, {'model':'test-model', 'messages':[{'role':'system','content':'rules'}],
                'max_completion_tokens':8192, 'stream':False, 'n':1, 'store':False,
                'response_format':{'type':'json_schema','json_schema':{
                    'name':'llm_verifier_decision_v1','strict':True,'schema':{}}}})
            self.assertEqual(provenance['provider'],'openai')
            self.assertEqual(provenance['endpoint'],ENDPOINT)
            self.assertNotIn('routing',provenance)
            self.assertNotIn('dummy',str(provenance))
            self.assertTrue(provider.simulation)

    def test_malformed_success_no_semantic_repair_retry(self):
        with example() as (_,effective,_):
            transport=Mock();transport.request.return_value=HttpResponse(200,b'not JSON')
            provider=OpenAI(effective['config']['provider'],transport=transport,api_key='dummy')
            with self.assertRaises(InfrastructureError):provider.review([],{},100,4)
            self.assertEqual(transport.request.call_count,1)

    def test_bounded_response_requests_and_wall_deadline(self):
        with example() as (_,effective,_):
            transport=Mock();transport.request.return_value=HttpResponse(200,b'x'*300000)
            provider=OpenAI(effective['config']['provider'],transport=transport,api_key='dummy')
            with self.assertRaisesRegex(InfrastructureError,'response_limit'):provider.review([],{},100,4)
            transport.reset_mock()
            with self.assertRaisesRegex(InfrastructureError,'attempt_budget'):provider.review([],{},100,0)
            transport.request.assert_not_called()
            with self.assertRaisesRegex(InfrastructureError,'time_budget'):provider.review([],{},0,4)
            transport.request.assert_not_called()

    def test_real_http_reader_drops_errors_no_redirect_and_bounds_reads(self):
        conn=Mock();response=Mock();conn.getresponse.return_value=response
        response.status=302
        with patch('http.client.HTTPSConnection',return_value=conn) as connect:
            got=HttpTransport()._request_in_child(b'{}','KEY',1,32)
        self.assertEqual(got,HttpResponse(302,b''))
        connect.assert_called_once_with('api.openai.com',timeout=1)
        conn.request.assert_called_once_with('POST','/v1/chat/completions',body=b'{}',headers={
            'Authorization':'Bearer KEY','Content-Type':'application/json',
            'Accept':'application/json','User-Agent':'llm-verifier/0.1'})
        response.read1.assert_not_called()
        response.status=200;response.read1.side_effect=[b'a'*33]
        with patch('http.client.HTTPSConnection',return_value=conn),self.assertRaisesRegex(InfrastructureError,'^provider_response_limit$'):
            HttpTransport()._request_in_child(b'{}','KEY',1,32)
        conn.close.assert_called()

    def test_missing_credential_and_arbitrary_metadata(self):
        with example() as (_,effective,_),patch.dict('os.environ',{'OPENAI_API_KEY':''}):
            with self.assertRaisesRegex(InfrastructureError,'credential_missing'):OpenAI(effective['config']['provider'])
        body=loads(completion({}).body);body['usage']['private']='SECRET';body['private']='SECRET'
        _,provenance=parse_response(HttpResponse(200,canonical(body)),'test-model')
        self.assertNotIn('SECRET',str(provenance))

    def test_hard_deadline_trickled_headers_and_body_with_local_server(self):
        import socket
        import threading
        import llm_verifier.provider as module
        for phase in ('headers','body'):
            with self.subTest(phase=phase):
                listener=socket.socket();listener.settimeout(2);listener.bind(('127.0.0.1',0));listener.listen(1)
                port=listener.getsockname()[1]
                def server():
                    try:
                        with listener:
                            client,_=listener.accept()
                            with client:
                                client.recv(8192)
                                if phase=='headers':
                                    outgoing=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}'
                                else:
                                    client.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\n')
                                    outgoing=b'x'*200
                                for byte in outgoing:
                                    client.sendall(bytes([byte]));time.sleep(.025)
                    except OSError:
                        pass
                thread=threading.Thread(target=server,daemon=True);thread.start()
                # Replace only the trusted child bootstrap in the test. The
                # production config has no endpoint/worker override whatsoever.
                bootstrap=("import sys,http.client; sys.path.insert(0,sys.argv[1]); "
                           "from llm_verifier import provider as p; "
                           f"p.http.client.HTTPSConnection=lambda host,timeout:http.client.HTTPConnection('127.0.0.1',{port},timeout=timeout); "
                           "p._http_worker()")
                started=time.monotonic()
                with patch.object(module,'_CHILD_BOOTSTRAP',bootstrap),self.assertRaisesRegex(InfrastructureError,'^provider_timeout$'):
                    HttpTransport().request(b'{}','not-a-real-key',.15,1024)
                self.assertLess(time.monotonic()-started,.7)
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_worker_never_receives_secret_in_env_argv_or_disk(self):
        import subprocess
        captured=[]
        real_popen=subprocess.Popen
        def launch(cmd,**kwargs):
            captured.append((cmd,kwargs))
            return real_popen(cmd,**kwargs)
        # A local no-network worker returning an HTTP failure is enough to verify
        # secret placement, framing, process cleanup and error-body exclusion.
        bootstrap="import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'401\\n')"
        with patch('llm_verifier.provider._CHILD_BOOTSTRAP',bootstrap),patch('llm_verifier.provider.subprocess.Popen',side_effect=launch):
            result=HttpTransport().request(b'{}','test-secret-sentinel',1,1024)
        self.assertEqual(result,HttpResponse(401,b''))
        self.assertNotIn('test-secret-sentinel',str(captured))


    def test_only_openai_credentials_are_read_and_invalid_keys_fail_locally(self):
        with example() as (_, effective, _):
            config = effective['config']['provider']
            with patch.dict('os.environ', {'OPENAI_API_KEY':'openai-test-key',
                    'OPENROUTER_API_KEY':'legacy-test-key', 'OPENAI_BASE_URL':'https://invalid.example'}, clear=True):
                transport = Mock(); transport.request.return_value = completion({})
                client = OpenAI(config, transport=transport)
                _, provenance = client.review([], {}, 10, 1)
                self.assertEqual(transport.request.call_args.args[1], 'openai-test-key')
                self.assertEqual(provenance['endpoint'], ENDPOINT)
            for key in ('', ' ', 'bad key', 'bad\nkey', 'bad\rkey', 'badékey'):
                with self.subTest(key=key), patch.dict('os.environ', {
                        'OPENAI_API_KEY':key, 'OPENROUTER_API_KEY':'legacy-test-key'}):
                    with self.assertRaisesRegex(InfrastructureError, 'credential_missing'):
                        OpenAI(config)
            with self.assertRaisesRegex(InfrastructureError, 'credential_missing'):
                OpenAI(config, api_key='bad\x00key')

    def test_placeholder_cannot_make_a_production_request(self):
        with example() as (_, effective, _), patch.object(HttpTransport, 'request') as network:
            config = {**effective['config']['provider'], 'model':'SELECT_OPENAI_MODEL'}
            with self.assertRaisesRegex(InfrastructureError, 'explicit_provider_model_required'):
                OpenAI(config, api_key='test-key')
            network.assert_not_called()

    def test_json_object_is_explicit_and_uses_the_same_token_bound(self):
        with example() as (_, effective, _):
            config = {**effective['config']['provider'], 'response_format':'json_object', 'max_completion_tokens':512}
            transport = Mock(); transport.request.return_value = completion({})
            OpenAI(config, transport=transport, api_key='test-key').review([], {}, 5, 1)
            request = loads(transport.request.call_args.args[0])
            self.assertEqual(request['response_format'], {'type':'json_object'})
            self.assertEqual(request['max_completion_tokens'], 512)
            self.assertNotIn('max_tokens', request)
            self.assertNotIn('provider', request)

    def test_transient_retries_preserve_request_and_share_remaining_budget(self):
        with example(mutate=lambda c:c['provider'].update(reasoning_effort='high')) as (_, effective, _):
            for failure in (HttpResponse(429, b'PRIVATE'), HttpResponse(503, b'PRIVATE'), RuntimeError('PRIVATE')):
                with self.subTest(failure=type(failure).__name__):
                    transport = Mock(); transport.request.side_effect = [failure, completion({})]
                    clock = Mock(side_effect=[0, 0, 2, 3, 4])
                    sleeper = Mock()
                    with patch('llm_verifier.provider.time.monotonic', clock):
                        _, provenance = OpenAI(effective['config']['provider'], transport=transport,
                            api_key='test-key', sleeper=sleeper).review([], {}, 10, 2)
                    self.assertEqual(provenance['attempts'], 2)
                    self.assertEqual(provenance['elapsed_seconds'], 4)
                    calls = transport.request.call_args_list
                    self.assertEqual(calls[0].args[:2], calls[1].args[:2])
                    self.assertEqual(loads(calls[0].args[0])['reasoning_effort'], 'high')
                    self.assertEqual([call.args[2] for call in calls], [10, 7])
                    sleeper.assert_called_once_with(.5)

    def test_transport_return_after_deadline_is_not_a_judgment(self):
        with example() as (_, effective, _):
            transport = Mock(); transport.request.return_value = completion({})
            with patch('llm_verifier.provider.time.monotonic', side_effect=[0, 0, 11]):
                with self.assertRaisesRegex(InfrastructureError, 'time_budget'):
                    OpenAI(effective['config']['provider'], transport=transport, api_key='test-key').review([], {}, 10, 1)
            transport.request.assert_called_once()

    def test_nonstop_and_refused_successes_are_not_retried(self):
        with example() as (_, effective, _):
            for change in ('refusal','length','content_filter'):
                body = loads(completion({}).body)
                if change == 'refusal': body['choices'][0]['message']['refusal'] = 'PRIVATE'
                else: body['choices'][0]['finish_reason'] = change
                transport = Mock(); transport.request.return_value = HttpResponse(200, canonical(body))
                with self.assertRaisesRegex(InfrastructureError, 'provider_response_invalid'):
                    OpenAI(effective['config']['provider'], transport=transport, api_key='test-key').review([], {}, 10, 3)
                transport.request.assert_called_once()

    def test_native_snapshot_and_numeric_usage_are_retained_without_extra_metadata(self):
        body = loads(completion({}, model='gpt-4.1-2025-04-14').body)
        body['choices'][0]['message']['refusal'] = None
        body['usage']['completion_tokens_details'] = {'reasoning_tokens':10, 'private':'PRIVATE'}
        body['system_fingerprint'] = 'PRIVATE'
        _, provenance = parse_response(HttpResponse(200, canonical(body)), 'gpt-4.1')
        self.assertEqual(provenance['configured_model'], 'gpt-4.1')
        self.assertEqual(provenance['returned_model'], 'gpt-4.1-2025-04-14')
        self.assertEqual(provenance['request_id'], 'chatcmpl-test')
        self.assertEqual(provenance['usage'], {'prompt_tokens':100,'completion_tokens':50,'total_tokens':150})
        self.assertNotIn('PRIVATE', str(provenance))


    def test_optional_effort_request_bytes_and_provider_config_hash(self):
        messages = [{'role':'system', 'content':'rules'}]
        schema = {'type':'object', 'properties':{}, 'required':[], 'additionalProperties':False}
        with example() as (_, effective, _):
            for response_format in ('json_schema', 'json_object'):
                config = {**effective['config']['provider'], 'response_format':response_format}
                expected = {'model':'test-model', 'messages':messages, 'max_completion_tokens':8192,
                    'stream':False, 'n':1, 'store':False, 'response_format':{'type':response_format}}
                if response_format == 'json_schema':
                    expected['response_format']['json_schema'] = {
                        'name':'llm_verifier_decision_v1', 'strict':True, 'schema':schema}
                hashes = set()
                for effort in (None, 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
                    with self.subTest(response_format=response_format, effort=effort):
                        settings = dict(config)
                        request = copy.deepcopy(expected)
                        if effort is not None:
                            settings['reasoning_effort'] = effort
                            request['reasoning_effort'] = effort
                        transport = Mock(); transport.request.return_value = completion({})
                        _, provenance = OpenAI(settings, transport=transport, api_key='test-key').review(
                            messages, schema, 10, 1)
                        transport.request.assert_called_once()
                        body = transport.request.call_args.args[0]
                        self.assertEqual(body, canonical(request))
                        self.assertEqual(provenance['provider_config_sha256'], digest(settings))
                        self.assertEqual(provenance['prompt_sha256'], digest(messages))
                        self.assertEqual(provenance['schema_sha256'], digest(schema))
                        self.assertEqual(provenance['endpoint'], ENDPOINT)
                        hashes.add(provenance['provider_config_sha256'])
                self.assertEqual(len(hashes), 8)

    def test_effort_model_support_failure_does_not_downgrade(self):
        with example(mutate=lambda c:c['provider'].update(model='gpt-6-astra', reasoning_effort='high')) as (_, effective, _):
            transport = Mock(); transport.request.return_value = HttpResponse(400, b'PRIVATE')
            provider = OpenAI(effective['config']['provider'], transport=transport, api_key='test-key')
            with self.assertRaisesRegex(InfrastructureError, '^provider_http_failed$'):
                provider.review([], {}, 10, 3)
            transport.request.assert_called_once()
            request = loads(transport.request.call_args.args[0])
            self.assertEqual(request['model'], 'gpt-6-astra')
            self.assertEqual(request['reasoning_effort'], 'high')

    def test_exhaustion_preserves_safe_failure_categories(self):
        with example() as (_, effective, _):
            cases = (
                ([InfrastructureError('provider_timeout')]*2, 'provider_timeout'),
                ([RuntimeError('PRIVATE')]*2, 'provider_transport_failed'),
                ([InfrastructureError('PRIVATE')]*2, 'provider_transport_failed'),
                ([InfrastructureError({'PRIVATE':'SECRET'})]*2, 'provider_transport_failed'),
                ([HttpResponse(429,b'PRIVATE')]*2, 'provider_rate_limited'),
                ([HttpResponse(503,b'PRIVATE')]*2, 'provider_server_error'),
                ([HttpResponse(503,b'PRIVATE'),InfrastructureError('provider_timeout')], 'provider_timeout'),
            )
            for failures, category in cases:
                with self.subTest(category=category, failures=failures):
                    transport=Mock();transport.request.side_effect=failures
                    provider=OpenAI(effective['config']['provider'],transport=transport,
                        api_key='PRIVATE',sleeper=lambda _:None)
                    with self.assertRaisesRegex(InfrastructureError,'^'+category+'$') as error:
                        provider.review([],{},100,4)
                    self.assertNotIn('PRIVATE',str(error.exception))
                    self.assertEqual(transport.request.call_count,2)

    def test_size_failure_is_not_retried(self):
        with example() as (_, effective, _):
            transport=Mock();transport.request.side_effect=InfrastructureError('provider_response_limit')
            provider=OpenAI(effective['config']['provider'],transport=transport,api_key='test-key')
            with self.assertRaisesRegex(InfrastructureError,'^provider_response_limit$'):
                provider.review([],{},100,4)
            transport.request.assert_called_once()

    def test_worker_only_emits_allowlisted_failure_codes(self):
        import io
        from types import SimpleNamespace
        from llm_verifier.provider import _http_worker, TRANSPORT_ERRORS
        for failure in [InfrastructureError(code) for code in TRANSPORT_ERRORS]+[
                InfrastructureError('PRIVATE'),InfrastructureError({'PRIVATE':'SECRET'}),
                RuntimeError('PRIVATE')]:
            with self.subTest(failure=failure):
                output=io.BytesIO()
                metadata=canonical({'key':'PRIVATE','timeout':1,'limit':1024})+b'\n{}'
                with patch('sys.stdin',SimpleNamespace(buffer=io.BytesIO(metadata))), \
                     patch('sys.stdout',SimpleNamespace(buffer=output)), \
                     patch.object(HttpTransport,'_request_in_child',side_effect=failure):
                    if failure.args and type(failure.args[0]) is str and failure.args[0] in TRANSPORT_ERRORS:
                        _http_worker()
                        self.assertEqual(output.getvalue(),b'error\n'+failure.args[0].encode())
                    else:
                        with self.assertRaises(SystemExit):
                            _http_worker()
                        self.assertEqual(output.getvalue(),b'')
                self.assertNotIn(b'PRIVATE',output.getvalue())

    def test_parent_accepts_only_allowlisted_worker_error_frames(self):
        import llm_verifier.provider as module
        for code, expected in [('provider_timeout','provider_timeout'),
                ('provider_response_limit','provider_response_limit'),
                ('PRIVATE','provider_transport_failed')]:
            bootstrap="import sys;sys.stdin.buffer.read();sys.stdout.buffer.write("+repr(('error\n'+code).encode())+")"
            with self.subTest(code=code),patch.object(module,'_CHILD_BOOTSTRAP',bootstrap):
                with self.assertRaisesRegex(InfrastructureError,'^'+expected+'$'):
                    HttpTransport().request(b'{}','PRIVATE',1,1024)

    def test_long_review_config_and_phase_bound(self):
        from llm_verifier.config import normalize
        with example() as (_, effective, _):
            raw=copy.deepcopy(effective['config'])
            raw['provider'].update(timeout_seconds=600,max_attempts=1)
            raw['limits']['total_review_seconds']=1200
            normalized=normalize(raw)
            self.assertEqual(normalized['provider']['timeout_seconds'],600)
            self.assertEqual(normalized['limits']['total_review_seconds'],1200)
            for field, value in [('timeout_seconds',601),('total_review_seconds',1201)]:
                bad=copy.deepcopy(raw)
                bad['provider' if field=='timeout_seconds' else 'limits'][field]=value
                with self.assertRaises(VerifierError):
                    normalize(bad)
            transport=Mock();transport.request.return_value=completion({})
            provider=OpenAI(normalized['provider'],transport=transport,api_key='test-key')
            with patch('llm_verifier.provider.time.monotonic',side_effect=[0,0,601]):
                with self.assertRaisesRegex(InfrastructureError,'^provider_time_budget$'):
                    provider.review([],{},1200,1)
            self.assertEqual(transport.request.call_args.args[2],600)

    def test_retry_timeouts_share_the_six_hundred_second_phase_budget(self):
        with example() as (_, effective, _):
            config={**effective['config']['provider'],'timeout_seconds':400,'max_attempts':2}
            transport=Mock()
            transport.request.side_effect=[InfrastructureError('provider_timeout'),completion({})]
            with patch('llm_verifier.provider.time.monotonic',side_effect=[0,0,399,400,500]):
                _, provenance=OpenAI(config,transport=transport,api_key='test-key',
                    sleeper=lambda _:None).review([],{},1200,2)
            self.assertEqual([call.args[2] for call in transport.request.call_args_list],[400,200])
            self.assertEqual(provenance['elapsed_seconds'],500)
