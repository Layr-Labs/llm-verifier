import copy
import json
import time
import unittest
from unittest.mock import Mock, patch

from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.jsonutil import canonical, loads
from llm_verifier.provider import HttpResponse, HttpTransport, OpenRouter, parse_response
from tests.support import completion, example


class Provider(unittest.TestCase):
    def test_truncation_refusal_tools_ambiguous_and_malformed(self):
        baseline=loads(completion({'kind':'judgment'}).body)
        variants=[]
        for reason in ('length','tool_calls','content_filter',None):
            data=copy.deepcopy(baseline);data['choices'][0]['finish_reason']=reason;variants.append(data)
        for field,value in [('refusal','No'),('tool_calls',[{'id':'x'}]),('function_call',{'name':'f'}),('role','user'),('content','```json\n{}\n```'),('content','{"x":1,"x":2}'),('content','NaN'),('content','{"n":1e999}')]:
            data=copy.deepcopy(baseline);data['choices'][0]['message'][field]=value;variants.append(data)
        data=copy.deepcopy(baseline);data['choices']*=2;variants.append(data)
        data=copy.deepcopy(baseline);data['error']={'message':'SECRET'};variants.append(data)
        data=copy.deepcopy(baseline);data['choices'][0]['error']={'message':'SECRET'};variants.append(data)
        for payload in variants:
            with self.subTest(payload=payload),self.assertRaisesRegex(InfrastructureError,'provider_response_invalid'):
                parse_response(HttpResponse(200,canonical(payload)),'organizer/model')

    def test_transport_error_retry_limits_and_redaction(self):
        with example() as (_,effective,_):
            config=effective['config']['provider']
            for side in ([RuntimeError('Authorization: SUPER-SECRET')]*2,[HttpResponse(500,b'SUPER-SECRET')]*2,[HttpResponse(401,b'SUPER-SECRET')]):
                transport=Mock();transport.request.side_effect=side
                provider=OpenRouter(config,transport=transport,api_key='SUPER-SECRET',sleeper=lambda _:None)
                with self.assertRaises(InfrastructureError) as error:provider.review([],{},100,4)
                self.assertNotIn('SUPER-SECRET',str(error.exception))
                self.assertEqual(transport.request.call_count,len(side))

    def test_valid_response_rejection_is_not_retried(self):
        with example() as (_,effective,_):
            decision={'kind':'judgment','judgment':{'valid':False},'requests':[]}
            transport=Mock();transport.request.return_value=completion(decision)
            provider=OpenRouter(effective['config']['provider'],transport=transport,api_key='dummy')
            got,provenance=provider.review([{'role':'system','content':'rules'}],{},100,4)
            self.assertEqual(got,decision)
            self.assertEqual(transport.request.call_count,1)
            self.assertEqual(provenance['usage']['total_tokens'],150)
            request=loads(transport.request.call_args.args[0])
            self.assertEqual(request['model'],'ORGANIZER/MODEL')
            self.assertFalse(request['provider']['allow_fallbacks'])
            self.assertEqual(request['provider']['data_collection'],'deny')
            self.assertTrue(request['provider']['zdr'])
            self.assertNotIn('dummy',str(provenance))
            self.assertTrue(provider.simulation)

    def test_malformed_success_no_semantic_repair_retry(self):
        with example() as (_,effective,_):
            transport=Mock();transport.request.return_value=HttpResponse(200,b'not JSON')
            provider=OpenRouter(effective['config']['provider'],transport=transport,api_key='dummy')
            with self.assertRaises(InfrastructureError):provider.review([],{},100,4)
            self.assertEqual(transport.request.call_count,1)

    def test_bounded_response_requests_and_wall_deadline(self):
        with example() as (_,effective,_):
            transport=Mock();transport.request.return_value=HttpResponse(200,b'x'*300000)
            provider=OpenRouter(effective['config']['provider'],transport=transport,api_key='dummy')
            with self.assertRaisesRegex(InfrastructureError,'response_limit'):provider.review([],{},100,4)
            transport.reset_mock()
            with self.assertRaisesRegex(InfrastructureError,'attempt_budget'):provider.review([],{},100,0)
            transport.request.assert_not_called()
            with self.assertRaisesRegex(InfrastructureError,'time_budget'):provider.review([],{},0,4)
            transport.request.assert_not_called()

    def test_real_http_reader_drops_errors_no_redirect_and_bounds_reads(self):
        conn=Mock();response=Mock();conn.getresponse.return_value=response
        response.status=302
        with patch('http.client.HTTPSConnection',return_value=conn):
            got=HttpTransport()._request_in_child(b'{}','KEY',1,32)
        self.assertEqual(got,HttpResponse(302,b''))
        response.read1.assert_not_called()
        response.status=200;response.read1.side_effect=[b'a'*33]
        with patch('http.client.HTTPSConnection',return_value=conn),self.assertRaises(InfrastructureError):
            HttpTransport()._request_in_child(b'{}','KEY',1,32)
        conn.close.assert_called()

    def test_missing_credential_and_arbitrary_metadata(self):
        with example() as (_,effective,_),patch.dict('os.environ',{'OPENROUTER_API_KEY':''}):
            with self.assertRaisesRegex(InfrastructureError,'credential_missing'):OpenRouter(effective['config']['provider'])
        body=loads(completion({}).body);body['usage']['private']='SECRET';body['private']='SECRET'
        _,provenance=parse_response(HttpResponse(200,canonical(body)),'test/model')
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
                with patch.object(module,'_CHILD_BOOTSTRAP',bootstrap),self.assertRaises(InfrastructureError):
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
