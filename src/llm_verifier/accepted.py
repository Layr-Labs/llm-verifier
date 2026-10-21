"""Yukon accepted-result resolution using prepare-job GitHub OIDC only.

The trusted dispatch supplies an origin/identity, never the submission. Tokens
travel in private worker pipes and headers, not command arguments or snapshots.
"""

import http.client
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import InfrastructureError, VerifierError
from .jsonutil import canonical, keys, loads, text
from .prior import consumer_identity, references

MAX_RESPONSE = 2 * 1024 * 1024
MAX_REQUEST = 16384
TIMEOUT = 15
ERRORS = {"unavailable", "unsupported_representation", "dependency_changed", "invalid_request", "forbidden", "service_unavailable", "conflict"}


def endpoint(value, *, origin=False):
    if type(value) is not str or len(value) > 8192:
        raise VerifierError("prior_endpoint")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or
                parsed.password is not None or parsed.fragment or
                (origin and (parsed.path not in ("", "/") or parsed.query))):
            raise ValueError()
        if any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError()
        parsed.port
        return parsed
    except ValueError:
        raise VerifierError("prior_endpoint") from None


def context_identity(context):
    keys(context, {"version", "api", "audience", "submission_id", "benchmark_id", "challenge_id", "evaluation_id", "job_id"})
    if type(context["version"]) is not int or context["version"] != 1:
        raise VerifierError("prior_context_version")
    endpoint(context["api"], origin=True)
    text(context["audience"], 256)
    if not context["audience"].startswith("yukon:accepted-results:") or any(ord(c) < 33 for c in context["audience"]):
        raise VerifierError("prior_audience")
    return consumer_identity({k: context[k] for k in ("submission_id", "benchmark_id", "challenge_id", "evaluation_id", "job_id")})


def resolve(context, requested):
    identity = context_identity(context)
    requested = references(requested)
    body = canonical({"job_id": identity["job_id"], "references": requested})
    if len(body) > MAX_REQUEST:
        raise VerifierError("prior_request_byte_limit")
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_token:
        raise InfrastructureError("prior_workflow_identity_unavailable")
    endpoint(request_url)
    if len(request_token) > 16384 or any(ord(c) < 33 for c in request_token):
        raise InfrastructureError("prior_workflow_identity_unavailable")
    value = PlatformTransport().request(context, body, request_url, request_token)
    if canonical(value.get("consumer")) != canonical(identity):
        raise VerifierError("prior_consumer_binding")
    return value


def _http(url, method, headers, body, deadline, limit):
    parsed = endpoint(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InfrastructureError("prior_transport_timeout")
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=remaining)
    response = None
    try:
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InfrastructureError("prior_transport_timeout")
            sock = conn.sock or (response.fp.raw._sock if response.fp is not None else None)
            if sock is not None:
                sock.settimeout(remaining)
            chunk = response.read1(min(65536, limit + 1 - len(output)))
            output.extend(chunk)
            if len(output) > limit:
                raise InfrastructureError("prior_response_byte_limit")
            if not chunk:
                return response.status, bytes(output)
    finally:
        if response is not None:
            response.close()
        conn.close()


def _exchange(context, body, request_url, request_token, timeout):
    deadline = time.monotonic() + timeout
    parsed = endpoint(request_url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k != "audience"]
    query.append(("audience", context["audience"]))
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    status, raw = _http(url, "GET", {"Authorization": "Bearer " + request_token}, None, deadline, 32768)
    if status != 200:
        raise InfrastructureError("prior_workflow_identity_unavailable")
    token_response = loads(raw, 32768)
    token = token_response.get("value") if type(token_response) is dict else None
    if type(token) is not str or not token or len(token) > 16384 or any(ord(c) < 33 for c in token):
        raise InfrastructureError("prior_workflow_identity_unavailable")
    # Retries cover the short dispatch/run-discovery race only. Denied,
    # missing/private, invalid and revoked dependencies are never retried.
    for attempt in range(4):
        status, raw = _http(context["api"].rstrip("/") + "/api/internal/accepted-results/resolve", "POST",
                     {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"},
                     body, deadline, MAX_RESPONSE)
        if status != 503 or _error_code(raw) != "service_unavailable" or attempt == 3:
            return status, raw
        delay = min(0.5 * 2**attempt, deadline - time.monotonic())
        if delay <= 0:
            raise InfrastructureError("prior_transport_timeout")
        time.sleep(delay)
    raise InfrastructureError("prior_transport_failed")


def _error_code(raw):
    try:
        value = loads(raw, 4096)
        code = value.get("error", {}).get("code") if type(value.get("error")) is dict else value.get("error", value.get("code"))
        return code if code in ERRORS else None
    except Exception:
        return None


class PlatformTransport:
    def request(self, context, body, request_url, request_token):
        started, process = time.monotonic(), None
        try:
            process = subprocess.Popen([sys.executable, "-I", "-B", "-c", _BOOTSTRAP,
                                       str(Path(__file__).resolve().parent.parent)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath, "LANG": "C.UTF-8"})
            payload = canonical({"context": context, "body": body.decode("ascii"), "request_url": request_url,
                                 "request_token": request_token, "timeout": TIMEOUT})
            output, _ = process.communicate(payload, timeout=max(0.001, TIMEOUT - (time.monotonic() - started)))
            if process.returncode != 0 or len(output) > MAX_RESPONSE + 16:
                raise InfrastructureError("prior_transport_failed")
            status, raw = output.split(b"\n", 1)
            if status != b"200":
                # The worker emits only a fixed category, never upstream prose.
                error = raw.decode("ascii")
                if error not in ERRORS:
                    error = "service_unavailable"
                raise InfrastructureError("prior_" + error)
            value = loads(raw, MAX_RESPONSE)
            if type(value) is not dict:
                raise VerifierError("prior_snapshot_shape")
            return value
        except subprocess.TimeoutExpired:
            raise InfrastructureError("prior_transport_timeout") from None
        except (OSError, ValueError) as exc:
            if isinstance(exc, VerifierError):
                raise
            raise InfrastructureError("prior_transport_failed") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.communicate(timeout=1)
                except subprocess.SubprocessError:
                    process.kill()
                    process.wait(timeout=1)


_BOOTSTRAP = "import sys; sys.path.insert(0, sys.argv[1]); from llm_verifier.accepted import _worker; _worker()"


def _worker():
    try:
        request = loads(sys.stdin.buffer.read(65537), 65536)
        context_identity(request["context"])
        body = request["body"].encode("ascii")
        if len(body) > MAX_REQUEST:
            raise ValueError()
        status, raw = _exchange(request["context"], body, request["request_url"], request["request_token"], request["timeout"])
        if status != 200:
            error = _error_code(raw) or "service_unavailable"
            raw = error.encode("ascii")
        sys.stdout.buffer.write(str(status).encode() + b"\n" + raw)
        sys.stdout.buffer.flush()
    except Exception:
        raise SystemExit(1)
