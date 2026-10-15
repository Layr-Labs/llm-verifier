"""Protected checker execution identities and deterministic numeric-score gate.

Callers validate the complete artifact chain before deriving gate status. Hashes
bind artifacts on the trusted organizer channel; they are not signatures.
"""

from .errors import VerifierError
from .input_schema import validate_value
from .jsonutil import canonical, digest
from .score_binding import derive_bindings, enforce_bindings


def execution_source(data, request):
    """Select exactly one namespace and bundle, never merge candidate source."""
    if "check" in request:
        entry = next(c for c in data["effective"]["config"]["checkers"] if c["id"] == request["check"])
        files = {path: data["effective"]["checker_files"][path] for path in entry["files"]}
    else:
        entry = next(e for e in data["manifest"]["experiments"] if e["id"] == request["experiment"])
        files = data["files"]
    return entry, files


def run_identity(intake, request, round_name, index):
    data = intake["data"]
    entry, files = execution_source(data, request)
    return {"id": f"run:{round_name}:{index}", "request": request, "source": entry["source"],
            "source_sha256": files[entry["source"]]["sha256"],
            "provenance": {"kind": "organizer_check" if "check" in request else "candidate_experiment",
                           "requester": "submission" if round_name == "initial" else "judge",
                           "descriptor_sha256": digest(entry), "bundle_sha256": digest(files),
                           "input_sha256": digest(request["inputs"]), "config_sha256": intake["bindings"]["config"]}}


def check_validation(entry, execution):
    """Schema failure is evidence, not an infrastructure error or new output."""
    if execution["status"] != "completed":
        return {"status": "execution_failed", "passed": False}
    try:
        validate_value(entry["output_schema"], execution["result"])
    except VerifierError:
        return {"status": "invalid_output", "passed": False}
    return {"status": "valid", "passed": execution["result"].get("passed") is True}


def score_gate(data, initial, followup=None):
    """Derive static categories, IDs, hashes and strictly typed score records.

    All reports must already be verified against this intake and exact plans.
    Judge-selected different inputs remain scoped evidence, never replacements
    for the single immutable submission-declared payload.
    """
    declarations = {c["check"]: c["inputs"] for c in data["manifest"].get("checks", [])}
    runs = initial["data"]["runs"] + (followup["data"]["runs"] if followup is not None else [])
    required = []
    for checker in data["effective"]["config"].get("checkers", []):
        if not checker["required_for_score"]:
            continue
        declared = checker["id"] in declarations
        inputs = declarations.get(checker["id"])
        passing = []
        if declared:
            for run in runs:
                if (run["request"].get("check") == checker["id"]
                        and canonical(run["request"]["inputs"]) == canonical(inputs)
                        and check_validation(checker, run["execution"])["passed"]):
                    passing.append(run["id"])
        required.append({"check": checker["id"], "declared_input_sha256": digest(inputs) if declared else None,
                         "status": "satisfied" if passing else "unsatisfied" if declared else "missing_declaration",
                         "passing_runs": passing})
    gate = {"satisfied": all(c["status"] == "satisfied" for c in required), "required": required}
    bindings = derive_bindings(data, runs, required)
    if bindings:
        gate["bindings"] = bindings
    return gate


def enforce_score_gate(judgment, gate):
    # Deliberately independent of judgment validity and requested Yukon fields.
    if judgment["scores"] is not None and not gate["satisfied"]:
        raise VerifierError("required_score_checks_unsatisfied")
    enforce_bindings(judgment, gate)
