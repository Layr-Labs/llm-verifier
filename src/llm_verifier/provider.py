"""Bounded OpenAI adapter. Transport failures never become judgments."""

from dataclasses import dataclass
import http.client
import os
import re
from pathlib import Path
import subprocess
import sys
import time

from .errors import InfrastructureError, VerifierError
from .jsonutil import canonical, digest, loads

ENDPOINT = "https://api.openai.com/v1/chat/completions"


@dataclass
class HttpResponse:
    status: int
    body: bytes


class HttpTransport:
    def _request_in_child(self, body, api_key, timeout, limit):
        # http.client does not automatically follow redirects (unlike urlopen).
        # Each read updates the socket timeout to the remaining wall budget.
        deadline = time.monotonic() + timeout
        conn = http.client.HTTPSConnection("api.openai.com", timeout=timeout)
        response = None
        try:
            conn.request("POST", "/v1/chat/completions", body=body,
                         headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                                  "Accept": "application/json", "User-Agent": "llm-verifier/0.1"})
            response = conn.getresponse()
            if response.status != 200:
                # Do not read arbitrary provider error messages at all.
                return HttpResponse(response.status, b"")
            output = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InfrastructureError("provider_timeout")
                if conn.sock is not None:
                    conn.sock.settimeout(remaining)
                elif response.fp is not None:
                    response.fp.raw._sock.settimeout(remaining)
                chunk = response.read1(min(65536, limit + 1 - len(output)))
                output.extend(chunk)
                if len(output) > limit:
                    raise InfrastructureError("provider_response_limit")
                if not chunk:
                    return HttpResponse(response.status, bytes(output))
        except Exception:
            raise InfrastructureError("provider_transport_failed") from None
        finally:
            if response is not None:
                response.close()
            conn.close()


    def request(self, body, api_key, timeout, limit):
        # A killable worker bounds DNS, TLS, request writes, trickled headers AND
        # response bodies. Socket timeouts alone only bound individual operations.
        # Key/body pass through a private pipe, never argv, env, files or logs.
        started = time.monotonic()
        process = None
        try:
            process = subprocess.Popen([sys.executable, "-I", "-B", "-c", _CHILD_BOOTSTRAP,
                                       str(Path(__file__).resolve().parent.parent)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath, "LANG": "C.UTF-8"})
            metadata = canonical({"key": api_key, "timeout": timeout, "limit": limit}) + b"\n"
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired("provider_worker", timeout)
            output, _ = process.communicate(metadata + body, timeout=remaining)
            if process.returncode != 0 or len(output) > limit + 16:
                raise InfrastructureError("provider_transport_failed")
            status, payload = output.split(b"\n", 1)
            if status not in {str(n).encode() for n in range(100, 600)}:
                raise InfrastructureError("provider_transport_failed")
            if time.monotonic() - started > timeout:
                raise InfrastructureError("provider_timeout")
            return HttpResponse(int(status), payload)
        except Exception:
            raise InfrastructureError("provider_transport_failed") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.communicate(timeout=1)
                except subprocess.SubprocessError:
                    process.kill()
                    process.wait(timeout=1)


_CHILD_BOOTSTRAP = "import sys; sys.path.insert(0, sys.argv[1]); from llm_verifier.provider import _http_worker; _http_worker()"


def _http_worker():
    try:
        metadata = loads(sys.stdin.buffer.readline(16384), limit=16384)
        body = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
        if len(body) > 16 * 1024 * 1024:
            raise ValueError()
        response = HttpTransport()._request_in_child(body, metadata["key"], metadata["timeout"], metadata["limit"])
        sys.stdout.buffer.write(str(response.status).encode() + b"\n" + response.body)
        sys.stdout.buffer.flush()
    except BaseException:
        # No tracebacks or arbitrary provider response/error text from the child.
        raise SystemExit(1) from None


def _safe_identifier(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
        raise InfrastructureError("provider_metadata_invalid")
    return value


def parse_response(response, configured_model):
    try:
        payload = loads(response.body)
        if type(payload) is not dict or payload.get("object") != "chat.completion" or payload.get("error") is not None:
            raise VerifierError("provider_envelope")
        choices = payload["choices"]
        if type(choices) is not list or len(choices) != 1:
            raise VerifierError("ambiguous_response")
        choice = choices[0]
        if type(choice) is not dict or choice.get("finish_reason") != "stop" or choice.get("error") is not None:
            raise VerifierError("unfinished_response")
        message = choice["message"]
        if type(message) is not dict or message.get("role") != "assistant" or message.get("tool_calls") or message.get("function_call") or message.get("refusal") is not None:
            raise VerifierError("refusal_or_tool_response")
        if type(message.get("content")) is not str:
            raise VerifierError("text_response_required")
        decision = loads(message["content"])
        returned_model = _safe_identifier(payload["model"])
        response_id = _safe_identifier(payload["id"]) if payload.get("id") is not None else None
        # Preserve useful numeric usage only, never arbitrary provider metadata.
        usage = {}
        if "usage" in payload:
            if type(payload["usage"]) is not dict:
                raise VerifierError("usage_invalid")
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if key in payload["usage"]:
                    value = payload["usage"][key]
                    if type(value) is not int or not 0 <= value <= 10**9:
                        raise VerifierError("usage_invalid")
                    usage[key] = value
        return decision, {"configured_model": configured_model, "returned_model": returned_model,
                          "request_id": response_id, "usage": usage}
    except (VerifierError, KeyError, TypeError, ValueError):
        raise InfrastructureError("provider_response_invalid") from None


class OpenAI:
    simulation = False

    def __init__(self, config, *, transport=None, api_key=None, sleeper=time.sleep):
        self.config = config
        # Dependency injection is for trusted library tests only. The CLI reads
        # exactly this environment variable and never takes a key in argv/config.
        if config["model"] == "SELECT_OPENAI_MODEL":
            raise InfrastructureError("explicit_provider_model_required")
        self._key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        if type(self._key) is not str or not self._key or any(not 33 <= ord(char) <= 126 for char in self._key):
            raise InfrastructureError("provider_credential_missing")
        self.transport = transport or HttpTransport()
        self.simulation = transport is not None
        self.sleeper = sleeper

    def review(self, messages, schema, seconds, requests):
        config = self.config
        response_format = {"type": config["response_format"]}
        if config["response_format"] == "json_schema":
            response_format["json_schema"] = {"name": "llm_verifier_decision_v1", "strict": True, "schema": schema}
        request = {"model": config["model"], "messages": messages,
                   "max_completion_tokens": config["max_completion_tokens"],
                   "stream": False, "n": 1, "store": False,
                   "response_format": response_format}
        # Omission preserves historical request bytes and provider defaults.
        if "reasoning_effort" in config:
            request["reasoning_effort"] = config["reasoning_effort"]
        body = canonical(request)
        started = time.monotonic()
        attempts = min(requests, config["max_attempts"])
        for attempt in range(1, attempts + 1):
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise InfrastructureError("provider_time_budget")
            response = None
            try:
                response = self.transport.request(body, self._key, min(remaining, config["timeout_seconds"]), config["max_response_bytes"])
            except Exception:
                # Never expose exception text, headers or a chained traceback.
                pass
            if response is not None:
                if len(response.body) > config["max_response_bytes"]:
                    raise InfrastructureError("provider_response_limit")
                if response.status == 200:
                    decision, metadata = parse_response(response, config["model"])
                    elapsed = time.monotonic() - started
                    if elapsed > seconds:
                        raise InfrastructureError("provider_time_budget")
                    metadata.update(provider="openai", endpoint=ENDPOINT,
                                    prompt_sha256=digest(messages), schema_sha256=digest(schema),
                                    provider_config_sha256=digest(config), attempts=attempt,
                                    elapsed_seconds=elapsed)
                    return decision, metadata
                if response.status != 429 and not 500 <= response.status <= 599:
                    raise InfrastructureError("provider_http_failed")
            if attempt < attempts:
                self.sleeper(min(0.5 * attempt, max(0, seconds - (time.monotonic() - started))))
        raise InfrastructureError("provider_attempt_budget")
