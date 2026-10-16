"""Deterministic current-source scores after atomic submission acceptance."""

from fractions import Fraction
import math
import re

from .errors import VerifierError
from .files import relative
from .jsonutil import digest, integer, keys, text


REVIEW_INSTRUCTIONS = """When score_gate contains bindings, return scores=null.
valid=true means ALL original submission.claims are established as written.
A claim source selects that index in the current submission; its evidence names
immutable source files. A checker source selects the current canonical witness
and objective semantics. The verifier derives a complete score tuple only when
atomic validity and ALL current source/check gates permit it. No subset selection,
prior score or prior witness can authorize scoring. Missing/conflicting/bounded-out
sources leave scores unavailable; a valid unscored submission is allowed only when
configured. Positive infinity retains exact records with null numeric scores
because Yukon cannot order infinity.
"""

def score_value(value):
    if value == "positive_infinity":
        return None
    keys(value, {"numerator", "denominator"})
    n, d = value["numerator"], value["denominator"]
    if (type(n) is not str or type(d) is not str or not 1 <= len(n) <= 256
            or not 1 <= len(d) <= 256 or not re.fullmatch(r"0|-?[1-9][0-9]*", n)
            or not re.fullmatch(r"[1-9][0-9]*", d)):
        raise VerifierError("canonical_rational_required")
    exact = Fraction(int(n), int(d))
    if (str(exact.numerator), str(exact.denominator)) != (n, d):
        raise VerifierError("reduced_rational_required")
    projected = float(exact)
    if not math.isfinite(projected) or (projected == 0 and exact != 0):
        raise VerifierError("unrepresentable_score")
    return projected


def validate_claim(claim, files):
    if type(claim) is str:
        text(claim, 8000)
        return
    keys(claim, {"statement", "score", "evidence"})
    text(claim["statement"], 8000)
    score_value(claim["score"])
    paths = claim["evidence"]
    if type(paths) is not list or not 1 <= len(paths) <= 32:
        raise VerifierError("claim_evidence_required")
    for path in paths:
        relative(path)
        if path == "submission.json" or path not in files:
            raise VerifierError("claim_evidence_file_missing")
    if len(set(paths)) != len(paths):
        raise VerifierError("duplicate_claim_evidence")


def score_bound_status(record, objective):
    """Compare exact proof rationals or numeric checker outputs before rounding."""
    exact = (math.inf if record == "positive_infinity" else
             Fraction(int(record["numerator"]), int(record["denominator"]))
             if type(record) is dict else Fraction(record))
    for name, status in (("score_minimum", "below_minimum"), ("score_maximum", "above_maximum")):
        if name not in objective:
            continue
        limit = objective[name]
        bound = Fraction(int(limit["numerator"]), int(limit["denominator"]))
        if (exact < bound if name == "score_minimum" else exact > bound):
            return status
    return "ready"


def validate_objective_claims(objectives, claims):
    for objective in objectives:
        source = objective.get("score_source", {})
        if "claim" not in source or source["claim"] >= len(claims):
            continue
        claim = claims[source["claim"]]
        if type(claim) is not dict:
            continue  # An unscored lemma needs no numeric declaration.
        if claim["score"] == "positive_infinity" and objective["direction"] != "+":
            raise VerifierError("terminal_requires_maximization")
        status = score_bound_status(claim["score"], objective)
        if status != "ready":
            raise VerifierError("score_" + status)


def validate_sources(objectives, checkers):
    for objective in objectives:
        for name in ("score_minimum", "score_maximum"):
            if name in objective:
                if "score_source" not in objective:
                    raise VerifierError(name + "_requires_source")
                if score_value(objective[name]) is None:
                    raise VerifierError("finite_" + name + "_required")
        if "score_maximum" in objective and score_bound_status(objective["score_maximum"], objective) != "ready":
            raise VerifierError("objective_bounds_order")
    sources = [o.get("score_source") for o in objectives]
    if not any(s is not None for s in sources):
        return
    if any(s is None for s in sources):
        raise VerifierError("all_objectives_need_score_sources")
    registry = {c["id"]: c for c in checkers}
    for source in sources:
        if type(source) is dict and "claim" in source:
            keys(source, {"claim"})
            integer(source["claim"], 0, 31)
        else:
            keys(source, {"check", "field"})
            if type(source["check"]) is not str or source["check"] not in registry:
                raise VerifierError("unknown_score_checker")
            checker = registry[source["check"]]
            schema = checker["output_schema"]
            field = source["field"]
            if (not checker["required_for_score"] or type(field) is not str
                    or field not in schema["required"]
                    or schema["properties"][field]["type"] not in {"integer", "number"}):
                raise VerifierError("required_numeric_score_field")


def bindings(data, runs, required):
    validate_objective_claims(data["effective"]["config"]["objectives"], data["manifest"]["claims"])
    result = []
    by_run = {run["id"]: run for run in runs}
    by_check = {check["check"]: check for check in required}
    for objective in data["effective"]["config"]["objectives"]:
        source = objective.get("score_source")
        if source is None:
            continue
        # Binding the complete snapshot also includes every auxiliary file. No
        # participant-supplied digest or second copy of a proof is necessary.
        source_sha256 = digest({"source": source, "files": digest(data["files"])})
        record, value, status = None, None, "unavailable"
        if "claim" in source:
            claims = data["manifest"]["claims"]
            claim = claims[source["claim"]] if source["claim"] < len(claims) else None
            if type(claim) is dict:
                record = claim["score"]
                value = score_value(record)
                status = "ready"
        else:
            gate = by_check[source["check"]]
            values = [by_run[r]["execution"]["result"][source["field"]] for r in gate["passing_runs"]]
            if values:
                if any(v != values[0] for v in values):
                    status = "conflicting_results"
                else:
                    record = value = values[0]
                    status = score_bound_status(record, objective)
        result.append({"objective": objective["name"], "source": source, "source_sha256": source_sha256,
                       "status": status, "record": record, "value": value})
    return result


def project(judgment, gate, allow_unscored):
    """Retain the model response; derive a separate judgment for the dossier."""
    bound = gate.get("bindings", [])
    if not bound:
        return judgment, []
    if judgment["scores"] is not None:
        raise VerifierError("bound_scores_are_derived")
    complete = judgment["valid"] and gate["satisfied"] and all(b["status"] == "ready" for b in bound)
    if judgment["valid"] and not allow_unscored and not complete:
        raise VerifierError("scored_claim_required")
    scores = [b["value"] for b in bound] if complete and all(b["value"] is not None for b in bound) else None
    records = [{"objective": b["objective"], "source_sha256": b["source_sha256"], "record": b["record"],
                "projection": "finite" if b["value"] is not None else "unsupported_positive_infinity"}
               for b in bound] if complete else []
    return {**judgment, "scores": scores}, records
