"""Only static, safe diagnostic categories cross the CLI boundary."""


class VerifierError(ValueError):
    """Malformed input, incompatible policy, integrity failure or exhausted budget."""


class InfrastructureError(VerifierError):
    """The provider or isolated executor did not produce usable evidence."""
