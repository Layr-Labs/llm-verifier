import copy
import http.client
import unittest
from unittest.mock import Mock, patch

from llm_verifier.config import normalize
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.jsonutil import canonical, loads
from llm_verifier.provider import HttpResponse, HttpTransport, OpenAI, parse_response
from llm_verifier.streaming import WIRE_MULTIPLIER, assemble_stream
from tests.support import completion, example


def chunks(decision=None):
    if decision is None:
        decision = {"kind": "judgment", "judgment": {"valid": False}, "requests": []}
    base = loads(completion(decision).body)
    text = canonical(decision).decode()
    def chunk(choices, usage=None):
        return {"id": base["id"], "model": base["model"], "object": "chat.completion.chunk",
                "choices": choices, "usage": usage}
    def choice(delta, finish=None):
        return [{"index": 0, "delta": delta, "finish_reason": finish}]
    split = len(text) // 2
    return [chunk(choice({"role": "assistant", "content": text[:split]})),
            chunk(choice({"content": text[split:]})),
            chunk(choice({}, "stop")),
            chunk([], base["usage"])]


def wire(events, *, done=True):
    result = b": keepalive\n\n" + b"".join(b"data: " + canonical(e) + b"\n\n" for e in events)
    return result + (b"data: [DONE]\n\n" if done else b"")


class Streaming(unittest.TestCase):
    def test_complete_unicode_crlf_and_usage_match_plain_completion(self):
        decision = {"kind": "judgment", "text": "α漢字"}
        body = wire(chunks(decision)).replace(b"\n", b"\r\n")
        got = parse_response(HttpResponse(200, assemble_stream(body, 4096)), "test-model")
        expected = parse_response(completion(decision), "test-model")
        self.assertEqual(got, expected)

    def test_incomplete_ambiguous_and_nontext_streams_fail_safely(self):
        variants = []
        base = chunks()
        for index, mutate in [
            (0, lambda c: c.update(model="changed")),
            (1, lambda c: c["choices"].append(copy.deepcopy(c["choices"][0]))),
            (1, lambda c: c["choices"][0].update(index=True)),
            (1, lambda c: c["choices"][0]["delta"].update(refusal="SECRET")),
            (1, lambda c: c["choices"][0]["delta"].update(tool_calls=[{"SECRET": "SECRET"}])),
            (1, lambda c: c["choices"][0]["delta"].update(function_call={"name": "SECRET"})),
            (1, lambda c: c["choices"][0]["delta"].update(content=[])),
            (0, lambda c: c["choices"][0]["delta"].update(role="user")),
            (1, lambda c: c.update(error={"message": "SECRET"})),
            (1, lambda c: c.update(object="SECRET")),
            (1, lambda c: c["choices"][0]["delta"].update(audio="SECRET")),
        ]:
            data = copy.deepcopy(base); mutate(data[index]); variants.append(wire(data))
        variants += [wire(base, done=False), wire(base[:-1]), wire(base[:2]),
                     wire([base[3], *base[:3]]), wire([*base, base[3]]),
                     wire([*base[:3], base[1], base[3]]),
                     wire(base) + b"data: [DONE]\n\n",
                     wire(base) + b"data: SECRET\n\n", wire(base)[:-1], wire(base)[:-2], b"data: SECRET\n\n",
                     b"data: [DONE]\n\n", b"data: \xff\n\n"]
        for body in variants:
            with self.subTest(body=body), self.assertRaisesRegex(InfrastructureError, "^provider_stream_invalid$") as error:
                assemble_stream(body, 4096)
            self.assertNotIn("SECRET", str(error.exception))

    def test_length_termination_reaches_existing_rejection(self):
        data = chunks(); data[2]["choices"][0]["finish_reason"] = "length"
        response = HttpResponse(200, assemble_stream(wire(data), 4096))
        with self.assertRaisesRegex(InfrastructureError, "^provider_response_invalid_output_token_limit$"):
            parse_response(response, "test-model")

    def test_wire_and_assembled_response_limits(self):
        with self.assertRaisesRegex(InfrastructureError, "^provider_response_limit$"):
            assemble_stream(b":" * (WIRE_MULTIPLIER * 1024 + 1), 1024)
        with self.assertRaisesRegex(InfrastructureError, "^provider_response_limit$"):
            assemble_stream(wire(chunks({"text": "x" * 1100})), 1024)
        body = wire(chunks({"text": "x" * 700}))
        with self.assertRaisesRegex(InfrastructureError, "^provider_response_limit$"):
            assemble_stream(body, 800)

    def test_http_reader_assembles_arbitrary_byte_fragments_and_checks_type(self):
        body = wire(chunks({"text": "漢α"}))
        conn = Mock(); response = Mock(); conn.getresponse.return_value = response
        response.status = 200; response.getheader.return_value = "text/event-stream; charset=utf-8"
        response.read1.side_effect = [body[i:i+7] for i in range(0, len(body), 7)] + [b""]
        with patch("http.client.HTTPSConnection", return_value=conn):
            result = HttpTransport()._request_in_child(b'{"stream":true}', "dummy", 5, 4096)
        self.assertEqual(parse_response(result, "test-model")[0], {"text": "漢α"})
        self.assertEqual(conn.request.call_args.kwargs["headers"]["Accept"], "text/event-stream")
        response.getheader.return_value = "application/json"
        with patch("http.client.HTTPSConnection", return_value=conn), self.assertRaisesRegex(InfrastructureError, "^provider_stream_invalid$"):
            HttpTransport()._request_in_child(b'{"stream":true}', "dummy", 5, 4096)
        conn.close.assert_called()

    def test_stream_transport_hard_wire_limit(self):
        conn = Mock(); response = Mock(); conn.getresponse.return_value = response
        response.status = 200; response.getheader.return_value = "text/event-stream"
        response.read1.side_effect = [b"x" * (16 * 1024 + 1)]
        with patch("http.client.HTTPSConnection", return_value=conn), self.assertRaisesRegex(InfrastructureError, "^provider_response_limit$"):
            HttpTransport()._request_in_child(b'{"stream":true}', "dummy", 5, 1024)

    def test_connection_close_category_never_contains_exception_text(self):
        for error in [http.client.RemoteDisconnected("SECRET"),
                      http.client.IncompleteRead(b"SECRET"), ConnectionResetError("SECRET")]:
            conn = Mock(); conn.getresponse.side_effect = error
            with patch("http.client.HTTPSConnection", return_value=conn), self.assertRaisesRegex(InfrastructureError, "^provider_connection_closed$") as got:
                HttpTransport()._request_in_child(b"{}", "dummy", 5, 1024)
            self.assertNotIn("SECRET", str(got.exception))

    def test_opt_in_request_and_config_boolean(self):
        with example() as (_, effective, _):
            for value in [True, False]:
                config = copy.deepcopy(effective["config"]); config["provider"]["stream"] = value
                normalized = normalize(config)
                transport = Mock(); transport.request.return_value = completion({})
                OpenAI(normalized["provider"], transport=transport, api_key="dummy").review([], {}, 5, 1)
                request = loads(transport.request.call_args.args[0])
                self.assertIs(request["stream"], value)
                self.assertIs(request["store"], False)
                if value:
                    self.assertEqual(request["stream_options"], {"include_usage": True})
                else:
                    self.assertNotIn("stream_options", request)
            for value in [None, 0, 1, "true", []]:
                config = copy.deepcopy(effective["config"]); config["provider"]["stream"] = value
                with self.assertRaises(VerifierError):
                    normalize(config)

    def test_invalid_stream_is_not_semantically_retried(self):
        with example() as (_, effective, _):
            transport = Mock(); transport.request.side_effect = InfrastructureError("provider_stream_invalid")
            with self.assertRaisesRegex(InfrastructureError, "^provider_stream_invalid$"):
                OpenAI(effective["config"]["provider"], transport=transport, api_key="dummy").review([], {}, 10, 3)
            transport.request.assert_called_once()
