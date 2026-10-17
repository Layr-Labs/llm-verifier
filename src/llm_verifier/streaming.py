"""Bounded Chat Completions SSE assembly; partial streams are never decisions."""

from .errors import InfrastructureError, VerifierError
from .jsonutil import canonical, loads

WIRE_MULTIPLIER = 16


def assemble_stream(body, limit):
    if len(body) > WIRE_MULTIPLIER * limit:
        raise InfrastructureError("provider_response_limit")
    try:
        source = body.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        if not source.endswith("\n\n"):
            raise VerifierError("stream_incomplete")
        identity = None
        role = None
        parts = []
        content_bytes = 0
        finish = None
        usage = None
        done = False
        for block in source.split("\n\n"):
            data = []
            for line in block.split("\n"):
                if line.startswith("data:"):
                    value = line[5:]
                    data.append(value[1:] if value.startswith(" ") else value)
            if not data:
                continue  # SSE comments/keepalives and non-data fields.
            value = "\n".join(data)
            if done:
                raise VerifierError("stream_after_done")
            if value == "[DONE]":
                done = True
                continue
            chunk = loads(value, limit=WIRE_MULTIPLIER * limit)
            if (type(chunk) is not dict or chunk.get("object") != "chat.completion.chunk"
                    or chunk.get("error") is not None):
                raise VerifierError("stream_envelope")
            current = (chunk["id"], chunk["model"])
            if any(type(item) is not str for item in current):
                raise VerifierError("stream_identity")
            if identity is None:
                identity = current
            if current != identity:
                raise VerifierError("stream_identity_changed")
            choices = chunk["choices"]
            if type(choices) is not list:
                raise VerifierError("stream_choices")
            if not choices:
                if finish is None or usage is not None or type(chunk.get("usage")) is not dict:
                    raise VerifierError("stream_usage")
                usage = chunk["usage"]
                continue
            if len(choices) != 1 or finish is not None or usage is not None or chunk.get("usage") is not None:
                raise VerifierError("stream_ambiguous")
            choice = choices[0]
            if (type(choice) is not dict or type(choice.get("index")) is not int
                    or choice["index"] != 0 or choice.get("error") is not None):
                raise VerifierError("stream_choice")
            delta = choice["delta"]
            if (type(delta) is not dict
                    or set(delta) - {"role", "content", "refusal", "tool_calls", "function_call"}
                    or delta.get("refusal") is not None or delta.get("tool_calls")
                    or delta.get("function_call")):
                raise VerifierError("stream_message")
            if "role" in delta:
                if delta["role"] != "assistant":
                    raise VerifierError("stream_role")
                role = "assistant"
            content = delta.get("content")
            if content is not None:
                if type(content) is not str:
                    raise VerifierError("stream_text")
                content_bytes += len(content.encode("utf-8"))
                if content_bytes > limit:
                    raise InfrastructureError("provider_response_limit")
                parts.append(content)
            finish = choice.get("finish_reason")
            if finish is not None and type(finish) is not str:
                raise VerifierError("stream_finish")
        if not done or finish is None or role != "assistant" or usage is None:
            raise VerifierError("stream_incomplete")
        output = canonical({"id": identity[0], "model": identity[1], "object": "chat.completion",
                            "choices": [{"finish_reason": finish, "message": {
                                "role": role, "content": "".join(parts), "refusal": None}}],
                            "usage": usage})
        if len(output) > limit:
            raise InfrastructureError("provider_response_limit")
        return output
    except InfrastructureError:
        raise
    except (VerifierError, KeyError, TypeError, ValueError):
        raise InfrastructureError("provider_stream_invalid") from None
