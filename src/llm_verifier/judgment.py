"""Stage-specific model output contracts and Yukon projection."""

import sys

from .errors import VerifierError
from .jsonutil import keys, number, text
from .submission import followup_plan

JUDGMENT_FIELDS = {"valid", "scores", "reason", "accepted_claims", "assumptions", "unresolved_obligations", "evidence_refs", "review_standard"}


def validate_judgment(judgment, config, evidence_refs):
    keys(judgment, JUDGMENT_FIELDS)
    if type(judgment["valid"]) is not bool:
        raise VerifierError("explicit_validity_required")
    # Require a reason on every result, including invalid unscored work.
    text(judgment["reason"], 4000)
    if len(judgment["reason"].encode("utf-16-le")) // 2 > 4000:
        raise VerifierError("yukon_reason_length")
    if judgment["review_standard"] != config["review_standard"]:
        raise VerifierError("review_standard_mismatch")
    for field in ("accepted_claims", "assumptions", "unresolved_obligations", "evidence_refs"):
        values = judgment[field]
        if type(values) is not list or len(values) > 64:
            raise VerifierError("judgment_list_limit")
        for value in values:
            text(value, 8000)
        if len(set(values)) != len(values):
            raise VerifierError("duplicate_judgment_item")
    if not set(judgment["evidence_refs"]) <= set(evidence_refs):
        raise VerifierError("unknown_evidence_reference")
    if judgment["valid"] and (not judgment["accepted_claims"] or not judgment["evidence_refs"]):
        raise VerifierError("accepted_claim_evidence_required")
    if not judgment["valid"] and judgment["accepted_claims"]:
        raise VerifierError("invalid_accepted_claims")
    scores = judgment["scores"]
    if scores is None:
        if judgment["valid"] and not config["allow_unscored"] and not any("score_source" in o for o in config["objectives"]):
            raise VerifierError("scored_claim_required")
    else:
        if type(scores) is not list or not scores or len(scores) != len(config["objectives"]):
            raise VerifierError("score_objective_count")
        for score in scores:
            number(score, -sys.float_info.max, sys.float_info.max)
    return judgment


def validate_decision(value, config, manifest, refs, previous_runs, *, final=False):
    keys(value, {"kind", "judgment", "requests"})
    if value["kind"] == "judgment":
        if value["requests"] != []:
            raise VerifierError("judgment_with_requests")
        validate_judgment(value["judgment"], config, refs)
    elif value["kind"] == "followup" and not final:
        if value["judgment"] is not None:
            raise VerifierError("ambiguous_decision")
        followup_plan(value["requests"], manifest, config["limits"], previous_runs, config.get("checkers", []))
    else:
        raise VerifierError("final_judgment_required" if final else "decision_kind")
    return value


def response_schema(config, manifest=None, *, final=False):
    # Full semantic checking stays local; wire constraints aid supported models.
    strings = {"type": "array", "items": {"type": "string"}}
    judgment = {"type": "object", "additionalProperties": False,
                "required": sorted(JUDGMENT_FIELDS), "properties": {
                    "valid": {"type": "boolean"}, "scores": {"anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "number"}}]},
                    "reason": {"type": "string", "maxLength": 4000}, "review_standard": {"type": "string"},
                    **{field: strings for field in ("accepted_claims", "assumptions", "unresolved_obligations", "evidence_refs")}}}
    if any("score_source" in o for o in config["objectives"]):
        judgment["properties"]["scores"] = {"type": "null"}
    entries = (manifest or {"experiments": []})["experiments"] if not final and config["limits"]["max_followups"] else []
    checkers = config.get("checkers", []) if not final and config["limits"]["max_followups"] else []
    variants = [("experiment", e) for e in entries] + [("check", c) for c in checkers]
    def strict_objects(schema):
        if schema["type"] == "object":
            if set(schema["required"]) != set(schema["properties"]):
                raise VerifierError("strict_provider_optional_inputs_require_json_object")
            for child in schema["properties"].values():
                strict_objects(child)
        elif schema["type"] == "array":
            strict_objects(schema["items"])
    if not final and config["provider"]["response_format"] == "json_schema":
        for _, entry in variants:
            strict_objects(entry["input_schema"])
    requests = [{"type": "object", "additionalProperties": False,
                 "required": [selector, "inputs", "reason"], "properties": {
                     selector: {"type": "string", "enum": [entry["id"]]},
                     "inputs": entry["input_schema"], "reason": {"type": "string"}}} for selector, entry in variants]
    can_request = not final and bool(requests) and config["limits"]["max_followups"] > 0
    request_items = {"anyOf": requests} if requests else {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    return {"type": "object", "additionalProperties": False, "required": ["kind", "judgment", "requests"],
            "properties": {"kind": {"type": "string", "enum": ["judgment", "followup"] if can_request else ["judgment"]},
                           "judgment": {"anyOf": [judgment, {"type": "null"}]} if can_request else judgment,
                           "requests": {"type": "array", "maxItems": config["limits"]["max_followups"] if can_request else 0,
                                        "items": request_items}}}



def yukon(judgment, evaluation):
    fields = evaluation["fields"]
    # Reorg score-only evaluation must establish legitimate scored claims.
    if fields["scores"] and not fields["validity"] and not judgment["valid"]:
        raise VerifierError("score_only_claim_not_established")
    result = {}
    if fields["validity"]:
        result.update(valid=judgment["valid"], reason=judgment["reason"])
    if fields["scores"]:
        result["scores"] = judgment["scores"]
    return result
