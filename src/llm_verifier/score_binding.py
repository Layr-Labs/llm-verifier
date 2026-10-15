"""Opt-in v1 rational/terminal records from protected canonical checker runs."""

from fractions import Fraction
import math
import re

from .errors import VerifierError
from .jsonutil import canonical, keys


def validate_bindings(objectives, checkers):
    registry = {check["id"]: check for check in checkers}
    for objective in objectives:
        if "score_binding" not in objective:
            continue
        binding = objective["score_binding"]
        keys(binding, {"version", "check", "field"})
        if type(binding["version"]) is not int or binding["version"] != 1:
            raise VerifierError("score_binding_version")
        if type(binding["check"]) is not str or binding["check"] not in registry:
            raise VerifierError("score_binding_checker")
        check = registry[binding["check"]]
        field = binding["field"]
        schema = check["output_schema"]
        if (not check["required_for_score"] or type(field) is not str
                or field not in schema["required"]
                or schema["properties"][field]["type"] != "object"):
            raise VerifierError("score_binding_required_record")


def validate_record(record):
    """Closed semantic union, layered over the existing bounded schema dialect."""
    if type(record) is not dict:
        raise VerifierError("score_record_object")
    kind = record.get("kind")
    if kind == "finite":
        keys(record, {"kind", "claim_id", "numerator", "denominator"})
        numerator, denominator = record["numerator"], record["denominator"]
        if (type(numerator) is not str or type(denominator) is not str
                or not 1 <= len(numerator) <= 256 or not 1 <= len(denominator) <= 256
                or not re.fullmatch(r"(?:0|-?[1-9][0-9]*)", numerator)
                or not re.fullmatch(r"[1-9][0-9]*", denominator)):
            raise VerifierError("score_record_rational")
        fraction = Fraction(int(numerator), int(denominator))
        if str(fraction.numerator) != numerator or str(fraction.denominator) != denominator:
            raise VerifierError("score_record_not_reduced")
        value = float(fraction)
        if not math.isfinite(value) or (value == 0 and fraction != 0):
            raise VerifierError("score_record_not_representable")
    elif kind == "terminal":
        keys(record, {"kind", "claim_id", "terminal"})
        if record["terminal"] != "positive_infinity":
            raise VerifierError("score_record_terminal")
        value = None
    else:
        raise VerifierError("score_record_kind")
    if type(record["claim_id"]) is not str or not re.fullmatch(r"claim:[a-f0-9]{64}", record["claim_id"]):
        raise VerifierError("score_record_claim_id")
    return value


def derive_bindings(data, runs, required):
    """Only already-verified, passing, canonical-input reports are eligible."""
    result = []
    by_check = {entry["check"]: entry for entry in required}
    by_run = {run["id"]: run for run in runs}
    for index, objective in enumerate(data["effective"]["config"]["objectives"]):
        binding = objective.get("score_binding")
        if binding is None:
            continue
        check_gate = by_check[binding["check"]]
        records, status = [], check_gate["status"]
        for run_id in check_gate["passing_runs"]:
            record = by_run[run_id]["execution"]["result"][binding["field"]]
            try:
                validate_record(record)
                if record["kind"] == "terminal" and objective["direction"] != "+":
                    raise VerifierError("score_record_terminal_direction")
            except VerifierError:
                status = "invalid_record"
                break
            records.append(record)
        if status == "satisfied" and len({canonical(r) for r in records}) != 1:
            status = "conflicting_records"
        record = records[0] if status == "satisfied" else None
        result.append({"objective": objective["name"], "index": index,
                       "check": binding["check"], "field": binding["field"],
                       "status": status, "record": record,
                       "value": validate_record(record) if record else None,
                       "passing_runs": check_gate["passing_runs"]})
    return result


def enforce_bindings(judgment, gate):
    scores = judgment["scores"]
    accepted = judgment["accepted_claims"]
    for binding in gate.get("bindings", []):
        record = binding["record"]
        if scores is not None:
            if binding["status"] != "satisfied":
                raise VerifierError("score_binding_unsatisfied")
            if record["kind"] == "terminal":
                raise VerifierError("terminal_requires_null_scores")
            if not judgment["valid"] or record["claim_id"] not in accepted:
                raise VerifierError("score_claim_not_accepted")
            # Number spelling 5 vs 5.0 is immaterial here. Input and evidence
            # bindings still retain their existing typed JSON identities.
            if scores[binding["index"]] != binding["value"]:
                raise VerifierError("score_binding_mismatch")
        elif record and record["kind"] == "terminal" and record["claim_id"] in accepted:
            if not judgment["valid"] or not gate["satisfied"]:
                raise VerifierError("terminal_score_checks_unsatisfied")


def accepted_records(judgment, gate):
    """Exact semantic records; Yukon has no terminal/exact-rational ordering."""
    enforce_bindings(judgment, gate)
    records = []
    for binding in gate.get("bindings", []):
        record = binding["record"]
        if (record and judgment["valid"] and record["claim_id"] in judgment["accepted_claims"]
                and (judgment["scores"] is not None or record["kind"] == "terminal")):
            records.append({"objective": binding["objective"], "record": record,
                            "check": binding["check"], "passing_runs": binding["passing_runs"],
                            "projection": ({"status": "unsupported", "reason": "yukon_positive_infinity_unsupported"}
                                           if record["kind"] == "terminal" else
                                           {"status": "finite_binary64", "value": binding["value"]})})
    return records
