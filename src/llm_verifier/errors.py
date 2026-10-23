"""Static error codes with bounded, escaped input locations (never values)."""

from contextlib import contextmanager
import json
import re


class VerifierError(ValueError):
    """Malformed input, incompatible policy, integrity failure or exhausted budget."""

    def __init__(self, code, *, missing=(), unexpected=()):
        super().__init__(code)
        self.path = ()
        self.missing = tuple(sorted(missing))[:32]
        self.unexpected = tuple(sorted(unexpected))[:32]

    def __str__(self):
        result = super().__str__()
        if self.path:
            path = ""
            for part in self.path[:32]:
                if type(part) is int:
                    path += f"[{part}]"
                elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
                    path += ("." if path else "") + part
                else:
                    path += "[" + json.dumps(part[:128], ensure_ascii=True) + "]"
            result += " at " + path
        for label, names in (("missing", self.missing), ("unexpected", self.unexpected)):
            if names:
                result += "; " + label + "=" + json.dumps([n[:64] for n in names], ensure_ascii=True)
        return result[:2048]


@contextmanager
def error_context(*path):
    try:
        yield
    except VerifierError as exc:
        exc.path = tuple(path) + exc.path
        raise


class InfrastructureError(VerifierError):
    """The provider or isolated executor did not produce usable evidence."""
