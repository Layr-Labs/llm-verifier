"""One protected, versioned manifest transformation; no candidate code or I/O.

Adapters are shipped as reviewed verifier code and selected only by organizer
configuration. There is no dynamic import, executable path or file rewrite hook.
A versioned adapter's rules must remain available unchanged for historical replay.
"""

from .errors import VerifierError, error_context
from .files import validate_files
from .jsonutil import canonical, loads, sha
from .scoring import canonical_score

ADAPTERS = ("curvesmash-v1",)


def validate_adapter(adapter):
    if type(adapter) is not str or adapter not in ADAPTERS:
        raise VerifierError("unknown_manifest_preprocessor")


def _curvesmash_v1(manifest):
    changes = []

    def assign(obj, key, value, path):
        if key in obj and obj[key] == value:
            return
        change = {"op": "replace" if key in obj else "add", "path": [*path, key], "after": value}
        if key in obj:
            change["before"] = obj[key]
        changes.append(change)
        obj[key] = value

    if type(manifest) is not dict:
        return changes  # Ordinary manifest validation supplies the diagnostic.
    claims = manifest.get("claims")
    if type(claims) is list:
        for index, claim in enumerate(claims):
            if type(claim) is dict and "score" in claim:
                with error_context("claims", index, "score"):
                    score = canonical_score(claim["score"])
                assign(claim, "score", score, ["claims", index])

    def schema_bounds(schema, path, depth=0):
        if depth > 8 or len(canonical(schema)) > 16384:
            raise VerifierError("input_schema_limit")
        if type(schema) is not dict:
            return
        kind, values = schema.get("type"), schema.get("enum")
        if "enum" in schema and kind in ("integer", "string"):
            expected = int if kind == "integer" else str
            if (type(values) is not list or not 1 <= len(values) <= 64
                    or any(type(v) is not expected for v in values) or len(set(values)) != len(values)):
                raise VerifierError("input_enum")
            # Do not override explicit restrictions. Normal validation checks all
            # enum members against both supplied and derived bounds and hard caps.
            bounds = ({"minimum": min(values), "maximum": max(values)} if kind == "integer"
                      else {"maxLength": max(map(len, values))})
            for key, value in bounds.items():
                if key not in schema:
                    assign(schema, key, value, path)
        if kind == "object" and type(schema.get("properties")) is dict:
            for key, child in schema["properties"].items():
                with error_context("properties", key):
                    schema_bounds(child, [*path, "properties", key], depth + 1)
        elif kind == "array" and "items" in schema:
            with error_context("items"):
                schema_bounds(schema["items"], [*path, "items"], depth + 1)

    entries = manifest.get("experiments")
    if type(entries) is list:
        for index, entry in enumerate(entries):
            if type(entry) is dict and "input_schema" in entry:
                path = ["experiments", index, "input_schema"]
                with error_context(*path):
                    schema_bounds(entry["input_schema"], path)
    return changes


def preprocess(files, config):
    """Return effective files and audit; preserve every original input record.

    Both snapshots satisfy the same limits. This function is replayed when an
    intake is consumed, so a resealed substituted manifest/audit is insufficient.
    """
    limits = config["limits"]
    validate_files(files, limits)
    if "submission.json" not in files:
        raise VerifierError("missing_submission_manifest")
    if "manifest_preprocessor" not in config:
        return files, None
    adapter = config["manifest_preprocessor"]
    validate_adapter(adapter)
    original = files["submission.json"]
    with error_context("submission.json"):
        manifest = loads(original["text"])
        # Single explicit dispatch, kept deliberately small for this first hook.
        changes = _curvesmash_v1(manifest)
    raw = canonical(manifest) if changes else original["text"].encode("utf-8")
    record = {"text": raw.decode("utf-8"), "size": len(raw), "sha256": sha(raw)}
    effective_files = {**files, "submission.json": record}
    validate_files(effective_files, limits)
    audit = {"adapter": adapter, "original_sha256": original["sha256"],
             "effective_sha256": record["sha256"], "changes": changes}
    return effective_files, audit


def execution_files(data):
    """Materialize only the effective manifest; callers verify the intake first."""
    if "preprocessing" not in data:
        return data["files"]
    return preprocess(data["files"], data["effective"]["config"])[0]
