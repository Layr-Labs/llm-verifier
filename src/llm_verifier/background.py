"""One background generation, bounded status polls, and best-effort cleanup."""

import re
import time

from .errors import InfrastructureError, VerifierError
from .jsonutil import canonical, loads

ENDPOINT = "https://api.openai.com/v1/responses"
MAX_POLLS = 120
POLL_SECONDS = 10
CLEANUP_SECONDS = 10
ERRORS = frozenset({
    "provider_background_invalid_envelope", "provider_background_invalid_identity",
    "provider_background_invalid_status", "provider_background_invalid_output",
    "provider_background_invalid_message", "provider_background_invalid_usage",
    "provider_background_failed", "provider_background_http_failed",
    "provider_background_poll_budget",
    "provider_response_invalid_output_token_limit",
})
ID = re.compile(r"resp_[A-Za-z0-9_-]{1,190}\Z")


def payload(body, limit):
    try:
        value = loads(body, limit=limit)
        if type(value) is not dict or value.get("object") != "response":
            raise VerifierError("envelope")
        return value
    except (VerifierError, KeyError, TypeError, ValueError):
        raise InfrastructureError("provider_background_invalid_envelope") from None


def response_id(value):
    identifier = value.get("id")
    if type(identifier) is not str or not ID.fullmatch(identifier):
        raise InfrastructureError("provider_background_invalid_identity")
    return identifier


def normalize_response(value, limit):
    category = "output"
    try:
        if value.get("status") != "completed" or value.get("error") is not None or value.get("incomplete_details") is not None:
            if (value.get("status") == "incomplete" and type(value.get("incomplete_details")) is dict
                    and value["incomplete_details"].get("reason") == "max_output_tokens"):
                raise InfrastructureError("provider_response_invalid_output_token_limit")
            raise InfrastructureError("provider_background_failed")
        output = value["output"]
        if type(output) is not list:
            raise VerifierError("output")
        messages = []
        for item in output:
            if type(item) is not dict:
                raise VerifierError("output")
            if item.get("type") == "reasoning":
                continue  # No reasoning content or summary is retained.
            if item.get("type") != "message":
                raise VerifierError("tool_or_nontext_output")
            messages.append(item)
        if len(messages) != 1:
            raise VerifierError("ambiguous_output")
        category = "message"
        message = messages[0]
        if message.get("role") != "assistant" or message.get("status") != "completed":
            raise VerifierError("message")
        content = message["content"]
        if type(content) is not list or not content:
            raise VerifierError("message")
        parts = []
        for part in content:
            if type(part) is not dict or part.get("type") != "output_text" or type(part.get("text")) is not str:
                raise VerifierError("refusal_or_nontext")
            parts.append(part["text"])
        category = "usage"
        usage = value["usage"]
        if type(usage) is not dict:
            raise VerifierError("usage")
        mapped = {}
        for native, standard in [("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("total_tokens", "total_tokens")]:
            count = usage[native]
            if type(count) is not int or not 0 <= count <= 10**9:
                raise VerifierError("usage")
            mapped[standard] = count
        result = canonical({"object": "chat.completion", "id": response_id(value), "model": value["model"],
                            "choices": [{"finish_reason": "stop", "message": {
                                "role": "assistant", "content": "".join(parts), "refusal": None}}],
                            "usage": mapped})
        if len(result) > limit:
            raise InfrastructureError("provider_response_limit")
        return result
    except InfrastructureError:
        raise
    except (VerifierError, KeyError, TypeError, ValueError):
        raise InfrastructureError("provider_background_invalid_" + category) from None


def request_background(body, key, timeout, limit, request, *, sleeper=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    identifier = None
    terminal = False

    def http(method, path, data=b"", *, cleanup=False):
        remaining = deadline - clock() - (0 if cleanup else CLEANUP_SECONDS)
        if remaining <= 0:
            raise InfrastructureError("provider_timeout")
        return request(data, key, min(5 if cleanup else 30, remaining), limit, method=method, path=path)

    try:
        response = http("POST", "/v1/responses", body)
        if response.status != 200:
            return response.status, b""
        current = payload(response.body, limit)
        identifier = response_id(current)
        polls = 0
        while True:
            if response_id(current) != identifier:
                raise InfrastructureError("provider_background_invalid_identity")
            state = current.get("status")
            if state not in ("queued", "in_progress"):
                if state not in ("completed", "failed", "incomplete", "cancelled"):
                    raise InfrastructureError("provider_background_invalid_status")
                terminal = True
                result = normalize_response(current, limit)
                return 200, result
            if polls >= MAX_POLLS:
                raise InfrastructureError("provider_background_poll_budget")
            if deadline - clock() <= CLEANUP_SECONDS + POLL_SECONDS:
                raise InfrastructureError("provider_timeout")
            sleeper(POLL_SECONDS)
            polls += 1
            try:
                response = http("GET", "/v1/responses/" + identifier)
            except InfrastructureError as exc:
                if exc.args in (("provider_timeout",), ("provider_connection_closed",), ("provider_transport_failed",)):
                    continue  # Poll the same generation; never start a new one.
                raise
            if response.status == 429 or 500 <= response.status <= 599:
                continue
            if response.status != 200:
                raise InfrastructureError("provider_background_http_failed")
            current = payload(response.body, limit)
    finally:
        if identifier is not None:
            if not terminal:
                try:
                    http("POST", "/v1/responses/" + identifier + "/cancel", b"{}", cleanup=True)
                except Exception:
                    pass
            try:
                http("DELETE", "/v1/responses/" + identifier, cleanup=True)
            except Exception:
                pass
