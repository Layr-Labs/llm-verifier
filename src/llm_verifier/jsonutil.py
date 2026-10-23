"""Strict bounded JSON; canonical hashes bind data, never authenticate it."""

import hashlib
import json
import math
from typing import Any

from .errors import VerifierError

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


def check_json(value: Any, depth: int = 0, counter=None) -> None:
    counter = [0] if counter is None else counter
    counter[0] += 1
    if depth > 32 or counter[0] > 100_000:
        raise VerifierError("json_complexity_limit")
    if type(value) is float and not math.isfinite(value):
        raise VerifierError("nonfinite_json")
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise VerifierError("invalid_unicode") from None
    elif type(value) is dict:
        for k, v in value.items():
            if type(k) is not str:
                raise VerifierError("invalid_json_key")
            check_json(k, depth + 1, counter)
            check_json(v, depth + 1, counter)
    elif type(value) is list:
        for v in value:
            check_json(v, depth + 1, counter)
    elif value is not None and type(value) not in (int, float, bool):
        raise VerifierError("invalid_json_type")


def canonical(value: Any) -> bytes:
    check_json(value)
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False).encode("ascii")
    except (ValueError, RecursionError):
        raise VerifierError("invalid_json") from None


def loads(data: bytes | str, limit: int = MAX_ARTIFACT_BYTES) -> Any:
    try:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if type(raw) is not bytes or len(raw) > limit:
            raise VerifierError("json_byte_limit")
        text = raw.decode("utf-8")
        def pairs(items):
            result = {}
            for k, v in items:
                if k in result:
                    raise VerifierError("duplicate_json_key")
                result[k] = v
            return result
        def constant(_):
            raise VerifierError("nonfinite_json")
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        check_json(value)
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, VerifierError):
            raise
        raise VerifierError("invalid_json") from None


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    return sha(canonical(value))


def keys(obj, required, optional=()):
    if type(obj) is not dict:
        raise VerifierError("object_fields")
    missing = set(required) - obj.keys()
    unexpected = obj.keys() - set(required) - set(optional)
    if missing or unexpected:
        raise VerifierError("object_fields", missing=missing, unexpected=unexpected)


def text(value, maximum=4000):
    if type(value) is not str or not value.strip() or len(value) > maximum or "\x00" in value:
        raise VerifierError("bounded_text_required")
    check_json(value)


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise VerifierError("integer_range")


def number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high:
        raise VerifierError("number_range")
