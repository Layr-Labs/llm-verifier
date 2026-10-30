"""Bounded failure evidence, deliberately outside the review/judgment protocol."""

from contextlib import contextmanager
import os
import json
import re
from pathlib import Path
from threading import Lock

from .artifacts import seal
from .errors import VerifierError
from .files import clear_output, ensure_directory, write_json
from .jsonutil import canonical, digest, loads, sha

MAX_TEXT = 256 * 1024
MAX_CAPTURE = 4 * 1024 * 1024
MAX_EVENTS = 128


def error_code(exc):
    # Never copy formatted errors (which can include untrusted field names),
    # exception reprs, HTTP error bodies, headers, or credentials.
    code = exc.args[0] if isinstance(exc, VerifierError) and exc.args else ""
    if type(code) is str and code.startswith("region_review_incomplete:"):
        code = "region_review_incomplete"
    return code if type(code) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) else "review_failed"


def redact(value, secret=""):
    if secret:
        # Assistant text can itself be JSON, or malformed JSON. Match normal
        # JSON escapes as well as literal characters without needing to parse it.
        parts = []
        for char in secret:
            forms = {re.escape(char), re.escape(json.dumps(char)[1:-1]),
                     "(?i:" + re.escape("\\u" + format(ord(char), "04x")) + ")"}
            parts.append("(?:" + "|".join(sorted(forms)) + ")")
        value = re.sub("".join(parts), "[REDACTED]", value)
    return value


def response_capture(body, secret=""):
    """Project HTTP 200 evidence before strict envelope/decision validation.

    Raw assistant text is retained even when it is not JSON. Arbitrary envelope
    metadata, provider error text, tool arguments and reasoning are not evidence.
    A malformed envelope has no trustworthy field boundaries. Retain only its
    size and redacted-body digest, never arbitrary error/metadata/reasoning text.
    """
    result = {"outputs": [], "usage": {}}
    try:
        value = loads(body)
    except VerifierError:
        result.update(unparsed_body_omitted=True, body_bytes=len(body),
                      redacted_body_sha256=sha(redact(body.decode("utf-8", errors="replace"), secret).encode("utf-8")))
        return bounded_capture(result)
    if type(value) is not dict:
        return result
    for key in ("id", "model"):
        item = value.get(key)
        if type(item) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", item):
            result[key] = redact(item, secret)
    usage = value.get("usage")
    if type(usage) is dict:
        for native, standard in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
                                 ("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                                 ("total_tokens", "total_tokens")):
            count = usage.get(native)
            if type(count) is int and 0 <= count <= 10**9:
                result["usage"][standard] = count
    choices = value.get("choices")
    if type(choices) is list:
        if len(choices) > 16:
            result["truncated"] = True
        for choice in choices[:16]:
            if type(choice) is not dict:
                continue
            message = choice.get("message")
            if type(message) is dict and message.get("role") == "assistant" and type(message.get("content")) is str:
                result["outputs"].append(redact(message["content"], secret))
    output = value.get("output")
    if type(output) is list:
        if len(output) > 32:
            result["truncated"] = True
        for item in output[:32]:
            if type(item) is not dict or item.get("type") != "message" or item.get("role") != "assistant":
                continue
            content = item.get("content")
            if type(content) is list:
                if len(content) > 16:
                    result["truncated"] = True
                for part in content[:16]:
                    if type(part) is dict and part.get("type") == "output_text" and type(part.get("text")) is str:
                        result["outputs"].append(redact(part["text"], secret))
    return bounded_capture(result)


def bounded_capture(value, secret=""):
    """Bound serialized text including escaping; metadata has a fixed allowlist."""
    if type(value) is not dict:
        return {"outputs": [], "usage": {}}
    result = {"outputs": [], "usage": {}}
    for key in ("id", "model"):
        item = value.get(key)
        if type(item) is str and len(item) <= 200:
            result[key] = redact(item, secret)
    usage = value.get("usage")
    if type(usage) is dict:
        result["usage"] = {k: v for k, v in usage.items()
                           if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                           and type(v) is int and 0 <= v <= 10**9}
    if value.get("truncated") is True:
        result["truncated"] = True
    if value.get("unparsed_body_omitted") is True:
        result["unparsed_body_omitted"] = True
        count = value.get("body_bytes")
        if type(count) is int and 0 <= count <= 16 * 1024 * 1024:
            result["body_bytes"] = count
        fingerprint = value.get("redacted_body_sha256")
        if type(fingerprint) is str and re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            result["redacted_body_sha256"] = fingerprint
    remaining = MAX_TEXT
    outputs = value.get("outputs", [])
    if type(outputs) is not list:
        outputs = []
    for key, texts in (("outputs", outputs),):
        for text in texts[:16]:
            if type(text) is not str:
                continue
            text = redact(text, secret)
            low, high = 0, min(len(text), remaining)
            while low < high:
                mid = (low + high + 1) // 2
                if len(canonical(text[:mid])) <= remaining:
                    low = mid
                else:
                    high = mid - 1
            selected = text[:low]
            remaining = max(0, remaining - len(canonical(selected)))
            if key == "outputs":
                result[key].append(selected)
            else:
                result[key] = selected
            if selected != text:
                result["truncated"] = True
        if len(texts) > 16:
            result["truncated"] = True
    return result


class ReviewDiagnostics:
    def __init__(self, intake, parents, phase, simulation, *, budget=None):
        self.intake = intake
        self.parents = parents
        self.phase = phase
        self.simulation = simulation
        self.calls = []
        self.bytes = 0
        self.events = 0
        self.truncated = False
        self._budget = budget if budget is not None else self
        self._lock = budget._lock if budget is not None else Lock()

    def start(self, member, stage, config, messages, schema, region=None):
        record = {"member_id": member, "stage": stage, "provider_config_sha256": digest(config),
                  "prompt_sha256": digest(messages), "schema_sha256": digest(schema), "events": [], "grants": []}
        if region is not None:
            record["region_id"] = region
        with self._lock:
            self.calls.append(record)

        def observe(event):
            with self._lock:
                retain(event)

        def retain(event):
            budget = self._budget
            # Only internally built observations reach this collector.
            size = len(canonical(event))
            self.truncated |= event.get("response", {}).get("truncated", False)
            budget.truncated |= self.truncated
            if budget.events >= MAX_EVENTS:
                self.truncated = budget.truncated = True
                return
            if budget.bytes + size > MAX_CAPTURE:
                self.truncated = budget.truncated = True
                response = event.get("response", {})
                event = {k: v for k, v in event.items() if k != "response"}
                if response:
                    event["response"] = {k: v for k, v in response.items() if k not in {"outputs", "unparsed_body"}}
                event["capture_omitted"] = True
                size = len(canonical(event))
            if budget.bytes + size > MAX_CAPTURE:
                self.truncated = budget.truncated = True
                return
            budget.events += 1
            budget.bytes += size
            record["events"].append(event)
        def grant(attempt, tokens):
            # Fixed-size admission evidence is independent of the raw-event cap.
            with self._lock:
                record["grants"].append({"attempt": attempt, "completion_tokens": tokens})

        def finish(attempts, error, result):
            # A fixed-size receipt is never dropped when raw capture is full.
            # It binds settled dispatch counts and retained output disposition.
            with self._lock:
                record["outcome"] = {"attempts": attempts, "error": error,
                                     "record_sha256": digest(result) if result is not None else None}
        observe.grant = grant
        observe.finish = finish
        return observe

    def envelope(self, exc):
        return seal("review_failure", self.intake["bindings"],
                     {"phase": self.phase, "error": error_code(exc), "calls": self.calls,
                      "truncated": self.truncated, "publishable": False,
                      **({"escalation_checkpoint": self.checkpoint} if hasattr(self, "checkpoint") else {})},
                     [p["sha256"] for p in self.parents], self.simulation)
    def write(self, path, exc):
        write_json(path, self.envelope(exc))


@contextmanager
def capture_failure(path, intake, parents, phase, simulation):
    if path is None:
        yield None
        return
    # Validate the complete ancestor chain even when the immediate parent does
    # not exist; failure to persist diagnostics must precede a paid request.
    ensure_directory(Path(path).parent)
    clear_output(path)
    capture = ReviewDiagnostics(intake, parents, phase, simulation)
    try:
        yield capture
    except Exception as exc:
        try:
            capture.write(path, exc)
        except Exception:
            # A second persistence error must not replace the original failure.
            # The caller's exception can still carry the in-memory checkpoint.
            pass
        raise


def review_call(provider, config, messages, schema, seconds, requests, capture=None, *,
                member="single", stage="review", region=None, observer=None, reserve=None):
    """Instrument built-in providers without changing injected provider APIs."""
    from .provider import OpenAI
    observe = observer
    if capture is not None:
        capture.simulation |= type(provider) is not OpenAI or provider.simulation
        observe = capture.start(member, stage, config, messages, schema, region)
    if type(provider) is OpenAI and (observe is not None or reserve is not None):
        return provider.review(messages, schema, seconds, requests, observer=observe, reserve=reserve)
    result = provider.review(messages, schema, seconds, requests)
    if observe is not None:
        # Trusted test injection is always simulation, even on failed calls.
        decision, provenance = result
        secret = os.environ.get("OPENAI_API_KEY", "")
        observe({"response": bounded_capture({"outputs": [redact(canonical(decision).decode(), secret)],
                                             "usage": {k: v for k, v in provenance.get("usage", {}).items()
                                                       if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                                                       and type(v) is int and 0 <= v <= 10**9}})})
    return result
