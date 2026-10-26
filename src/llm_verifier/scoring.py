"""Deterministic current-source scores after atomic submission acceptance."""

from fractions import Fraction
import math
import re

from .errors import VerifierError, error_context
from .files import relative
from .jsonutil import integer, keys, text
from .structured_claims import source_sha256 as source_digest, validate as validate_structured


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

def review_instructions(config):
    if config.get("acceptance_policy", "atomic-claims-v1") == "exploratory-v1":
        return REVIEW_INSTRUCTIONS.replace(
            "ALL original submission.claims are established as written",
            "ALL original submission.claims meet exploratory-v1 exactly as written"
        ).replace("atomic validity", "whole-submission exploratory validity")
    return REVIEW_INSTRUCTIONS


def canonical_score(value):
    """Validate bounded rational syntax and return its exact reduced spelling.

    Intake still requires that spelling unless protected preprocessing opts in.
    Accepted-source parsing may validate an equivalent original spelling without
    rewriting the authenticated manifest or changing its declared claim.
    """
    if value == "positive_infinity":
        return value
    keys(value, {"numerator", "denominator"})
    n, d = value["numerator"], value["denominator"]
    if (type(n) is not str or type(d) is not str or not 1 <= len(n) <= 256
            or not 1 <= len(d) <= 256 or not re.fullmatch(r"0|-?[1-9][0-9]*", n)
            or not re.fullmatch(r"[1-9][0-9]*", d)):
        raise VerifierError("canonical_rational_required")
    exact = Fraction(int(n), int(d))
    return {"numerator": str(exact.numerator), "denominator": str(exact.denominator)}


def score_value(value):
    normalized = canonical_score(value)
    if normalized == "positive_infinity":
        return None
    if normalized != value:
        raise VerifierError("reduced_rational_required")
    exact = Fraction(int(value["numerator"]), int(value["denominator"]))
    projected = float(exact)
    if not math.isfinite(projected) or (projected == 0 and exact != 0):
        raise VerifierError("unrepresentable_score")
    return projected


def validate_claim(claim, files, *, allow_structured=False):
    if type(claim) is str:
        text(claim, 8000)
        return
    structured = type(claim) is dict and "structured" in claim
    if structured and not allow_structured:
        raise VerifierError("structured_claims_disabled")
    keys(claim, {"statement", "evidence"} if structured else {"statement", "score", "evidence"},
         {"score", "structured"} if structured else ())
    text(claim["statement"], 8000)
    if structured:
        with error_context("structured"):
            validate_structured(claim["structured"])
    if "score" in claim:
        with error_context("score"):
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
        if type(claim) is not dict or "score" not in claim:
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
        source_sha256 = source_digest(data, source)
        record, value, status = None, None, "unavailable"
        if "claim" in source:
            claims = data["manifest"]["claims"]
            claim = claims[source["claim"]] if source["claim"] < len(claims) else None
            if type(claim) is dict and "score" in claim:
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
