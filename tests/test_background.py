import copy
import json
import unittest
from unittest.mock import Mock, patch

from llm_verifier import background, pipeline
from llm_verifier.config import normalize
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.jsonutil import canonical, loads
from llm_verifier.provider import ENDPOINT, HttpResponse, HttpTransport, OpenAI, parse_response
from tests.support import completion, direct, example, reseal


def native(state="completed", identifier="resp_test"):
    result = {"id": identifier, "object": "response", "status": state, "model": "test-model",
              "error": None, "incomplete_details": None, "output": [],
              "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}}
    if state == "completed":
        result["output"] = [{"type": "reasoning", "summary": []},
            {"type": "message", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": '{"kind":"judgment"}'}]}]
    return result


def response(value):
    return HttpResponse(200, canonical(value))


class Clock:
    def __init__(self):
        self.now = 0
    def time(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class Background(unittest.TestCase):
    def run_request(self, request, timeout=1200):
        clock = Clock()
        result = background.request_background(b'{"background":true,"store":false}', "dummy",
            timeout, 4096, request, clock=clock.time, sleeper=clock.sleep)
        return result, clock

    def test_completed_response_reuses_strict_decision_parser(self):
        body = background.normalize_response(native(), 4096)
        decision, metadata = parse_response(HttpResponse(200, body), "test-model")
        self.assertEqual(decision, {"kind": "judgment"})
        self.assertEqual(metadata["request_id"], "resp_test")
        self.assertEqual(metadata["usage"], {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        self.assertNotIn("summary", body.decode())

    def test_failures_truncation_refusals_tools_and_invalid_usage_are_fatal(self):
        variants = []
        for state in ["queued", "in_progress", "failed", "cancelled"]:
            variants.append(native(state))
        for mutate in [
            lambda v: v.update(output=[]),
            lambda v: v["output"].append(copy.deepcopy(v["output"][1])),
            lambda v: v["output"].append({"type": "function_call", "arguments": "SECRET"}),
            lambda v: v["output"][1].update(role="user"),
            lambda v: v["output"][1].update(status="in_progress"),
            lambda v: v["output"][1].update(content=[{"type": "refusal", "refusal": "SECRET"}]),
            lambda v: v["output"][1].update(content=[{"type": "output_text", "text": []}]),
            lambda v: v.update(usage={"input_tokens": True, "output_tokens": 0, "total_tokens": 0}),
            lambda v: v.update(error={"message": "SECRET"}),
        ]:
            value = native(); mutate(value); variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(InfrastructureError) as error:
                background.normalize_response(value, 4096)
            self.assertIn(str(error.exception), background.ERRORS)
            self.assertNotIn("SECRET", str(error.exception))
        value = native("incomplete"); value["incomplete_details"] = {"reason": "max_output_tokens"}
        with self.assertRaisesRegex(InfrastructureError, "^provider_response_invalid_output_token_limit$"):
            background.normalize_response(value, 4096)

    def test_native_response_byte_and_json_limits(self):
        with self.assertRaisesRegex(InfrastructureError, "^provider_response_limit$"):
            background.normalize_response(native(), 64)
        for body in [b"SECRET", b'{"object":"other"}', b'{"object":"response","object":"response"}']:
            with self.assertRaisesRegex(InfrastructureError, "^provider_background_invalid_envelope$"):
                background.payload(body, 4096)

    def test_polling_uses_one_generation_and_cleans_up(self):
        request = Mock(side_effect=[response(native("queued")), response(native("in_progress")),
                                    response(native()), HttpResponse(200, b"{}")])
        (status, body), clock = self.run_request(request)
        self.assertEqual(status, 200)
        self.assertEqual(parse_response(HttpResponse(status, body), "test-model")[0], {"kind": "judgment"})
        self.assertEqual(clock.now, 20)
        self.assertEqual([(c.kwargs["method"], c.kwargs["path"]) for c in request.call_args_list],
            [("POST", "/v1/responses"), ("GET", "/v1/responses/resp_test"),
             ("GET", "/v1/responses/resp_test"), ("DELETE", "/v1/responses/resp_test")])
        self.assertTrue(all(0 < c.args[2] <= 30 for c in request.call_args_list))

    def test_transient_poll_failures_never_start_another_generation(self):
        request = Mock(side_effect=[response(native("queued")), HttpResponse(503, b"SECRET"),
            InfrastructureError("provider_connection_closed"), HttpResponse(429, b"SECRET"),
            response(native()), HttpResponse(200, b"{}")])
        result, _ = self.run_request(request)
        self.assertEqual(result[0], 200)
        self.assertEqual(sum(c.kwargs["method"] == "POST" for c in request.call_args_list), 1)
        self.assertTrue(all(c.kwargs["path"] == "/v1/responses/resp_test" for c in request.call_args_list[1:]))

    def test_poll_cap_and_timeout_reserve_cancel_then_delete(self):
        for timeout, cap, expected in [(1200, 2, "provider_background_poll_budget"),
                                        (35, 120, "provider_timeout")]:
            request = Mock(return_value=response(native("in_progress")))
            clock = Clock()
            with patch.object(background, "MAX_POLLS", cap), self.assertRaisesRegex(InfrastructureError, "^"+expected+"$"):
                background.request_background(b"{}", "dummy", timeout, 4096, request,
                    clock=clock.time, sleeper=clock.sleep)
            calls = request.call_args_list
            self.assertLessEqual(len(calls), cap + 3)
            self.assertEqual([(c.kwargs["method"], c.kwargs["path"]) for c in calls[-2:]],
                [("POST", "/v1/responses/resp_test/cancel"), ("DELETE", "/v1/responses/resp_test")])
            self.assertLess(clock.now, timeout)

    def test_untrusted_ids_cannot_change_endpoint_or_cleanup_target(self):
        for value in ["resp_../SECRET", "https://SECRET", "resp_test?SECRET"]:
            request = Mock(return_value=response(native("queued", value)))
            with self.assertRaisesRegex(InfrastructureError, "^provider_background_invalid_identity$"):
                self.run_request(request)
            request.assert_called_once()
        request = Mock(side_effect=[response(native("queued")), response(native("completed", "resp_changed")),
                                   HttpResponse(200, b"{}"), HttpResponse(200, b"{}")])
        with self.assertRaisesRegex(InfrastructureError, "^provider_background_invalid_identity$"):
            self.run_request(request)
        self.assertTrue(all("resp_changed" not in c.kwargs["path"] for c in request.call_args_list))

    def test_cleanup_failure_preserves_obtained_judgment(self):
        request = Mock(side_effect=[response(native()), InfrastructureError("SECRET")])
        result, _ = self.run_request(request)
        self.assertEqual(result[0], 200)
        request = Mock(side_effect=[response(native("failed")), InfrastructureError("SECRET")])
        with self.assertRaisesRegex(InfrastructureError, "^provider_background_failed$"):
            self.run_request(request)

    def test_nonretryable_http_and_envelope_errors_cancel_original_generation(self):
        for failed, expected in [(HttpResponse(403, b"SECRET"), "provider_background_http_failed"),
                                  (HttpResponse(200, b"SECRET"), "provider_background_invalid_envelope")]:
            request = Mock(side_effect=[response(native("queued")), failed,
                                       HttpResponse(200, b"{}"), HttpResponse(200, b"{}")])
            with self.assertRaisesRegex(InfrastructureError, "^"+expected+"$"):
                self.run_request(request)
            self.assertEqual(request.call_args_list[-1].kwargs["method"], "DELETE")

    def test_native_request_schema_model_effort_and_storage_are_explicit(self):
        with example() as (_, effective, _):
            config = {**effective["config"]["provider"], "background": True, "reasoning_effort": "high"}
            transport = Mock(return_value=None); transport.request.return_value = completion({})
            _, provenance = OpenAI(config, transport=transport, api_key="dummy").review(
                [{"role": "system", "content": "rules"}, {"role": "user", "content": "proof"}], {}, 1200, 1)
            body = loads(transport.request.call_args.args[0])
            self.assertEqual(body, {"model": "test-model", "input": [
                {"role": "system", "content": "rules"}, {"role": "user", "content": "proof"}],
                "max_output_tokens": 8192, "background": True, "stream": False, "store": False,
                "truncation": "disabled", "reasoning": {"effort": "high"}, "text": {"format": {
                    "type": "json_schema", "name": "llm_verifier_decision_v1", "strict": True, "schema": {}}}})
            self.assertEqual(provenance["endpoint"], background.ENDPOINT)
            self.assertEqual(provenance["attempts"], 1)
            for bad in [None, 0, 1, "true", []]:
                raw = copy.deepcopy(effective["config"]); raw["provider"]["background"] = bad
                with self.assertRaises(VerifierError):
                    normalize(raw)

    def test_endpoint_provenance_is_bound_to_the_protected_mode(self):
        with example(mutate=lambda c: c["provider"].update(background=True)) as (root, _, intake):
            initial, first = direct(root, intake)
            self.assertFalse(first["simulation"])
            self.assertEqual(first["data"]["provenance"]["endpoint"], background.ENDPOINT)
            pipeline.verify_initial_review(intake, initial, first)
            tampered = copy.deepcopy(first); tampered["data"]["provenance"]["endpoint"] = ENDPOINT
            with self.assertRaisesRegex(VerifierError, "^untrusted_provider_provenance$"):
                pipeline.verify_initial_review(intake, initial, reseal(tampered))


    def test_private_worker_completes_background_protocol_end_to_end(self):
        import http.client
        import http.server
        import threading
        from llm_verifier import provider as module
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def handle_request(self):
                seen.append((self.command, self.path))
                if self.command == "POST":
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    value = native("queued")
                elif self.command == "GET":
                    value = native()
                else:
                    value = {"id": "resp_test", "object": "response.deleted", "deleted": True}
                body = canonical(value)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            do_POST = handle_request
            do_GET = handle_request
            do_DELETE = handle_request
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        port = server.server_port
        bootstrap = ("import sys,http.client; sys.path.insert(0,sys.argv[1]); "
                     "from llm_verifier import provider as p,background as b; b.POLL_SECONDS=0; "
                     f"p.http.client.HTTPSConnection=lambda host,timeout:http.client.HTTPConnection('127.0.0.1',{port},timeout=timeout); "
                     "p._http_worker()")
        try:
            with patch.object(module, "_CHILD_BOOTSTRAP", bootstrap):
                result = HttpTransport().request(b'{"background":true,"store":false}', "dummy", 15, 4096)
            self.assertEqual(parse_response(result, "test-model")[0], {"kind": "judgment"})
            self.assertEqual(seen, [("POST", "/v1/responses"), ("GET", "/v1/responses/resp_test"),
                                    ("DELETE", "/v1/responses/resp_test")])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
