"""Closed Yukon dispatch identity for an explicitly retained current result.

Validation is not authentication. The platform issues and fences this object;
the protected organizer plan independently authenticates historical bytes.
"""

import re

from .config import submission_references_enabled
from .errors import VerifierError
from .jsonutil import keys

VERSION = "yukon-retained-result-v1"
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
EVIDENCE = {"result_id", "evaluation_id", "policy_version_id", "source_ref", "job_id", "commit_sha"}


def identity(value):
    if type(value) is not str or not UUID.fullmatch(value):
        raise VerifierError("retained_result_identity")


def bounded_ref(value):
    if type(value) is not str:
        raise VerifierError("retained_result_reference")
    try:
        units = len(value.encode("utf-16-le")) // 2
    except UnicodeError:
        raise VerifierError("retained_result_reference") from None
    if not 1 <= units <= 1024:
        raise VerifierError("retained_result_reference")


def evidence(value, *, validity=False):
    keys(value, EVIDENCE | ({"valid"} if validity else set()))
    for field in ("result_id", "evaluation_id", "policy_version_id", "job_id"):
        identity(value[field])
    for field in ("source_ref", "commit_sha"):
        bounded_ref(value[field])
    if validity and type(value["valid"]) is not bool:
        raise VerifierError("retained_result_validity")


def origin(value):
    keys(value, {"version", "benchmark_id", "submission_id", "candidate_key", "packet_id", "score", "validity"})
    if value["version"] != VERSION:
        raise VerifierError("retained_result_version")
    for field in ("benchmark_id", "submission_id", "packet_id"):
        identity(value[field])
    if type(value["candidate_key"]) is not str or not re.fullmatch(
        r"(?:digest|archive):[a-f0-9]{64}", value["candidate_key"]
    ):
        raise VerifierError("retained_result_candidate_key")
    evidence(value["score"])
    evidence(value["validity"], validity=True)
    return value


def validate(value):
    keys(value, {"version", "benchmark_id", "submission_id", "candidate_key", "packet_id",
                 "score", "validity", "destination"})
    origin({k: v for k, v in value.items() if k != "destination"})
    destination = value["destination"]
    keys(destination, {"job_id", "evaluation_id", "packet_id", "source_ref"})
    for field in ("job_id", "evaluation_id", "packet_id"):
        identity(destination[field])
    bounded_ref(destination["source_ref"])
    return value


def evaluation(value):
    keys(value, {"policies", "fields", "retained_result"}, {"admission"})
    keys(value["policies"], {"scoring", "validity"})
    keys(value["fields"], {"scores", "validity"})
    if value["fields"]["scores"] is not True or value["fields"]["validity"] is not False:
        raise VerifierError("derivation_requires_score_only")
    scoring = value["policies"]["scoring"]
    if type(scoring) is not dict or scoring.get("retained_result") != VERSION:
        raise VerifierError("derivation_requires_protected_capability")
    submission_references_enabled(value.get("admission"))
    validate(value["retained_result"])
    return value
