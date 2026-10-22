"""One atomic source format. Digests bind bytes; the platform authenticates acceptance.

References and ancestry identify immutable submissions. Evaluation IDs/times are
provenance only. Yukon enforces original submission order and current acceptance,
including access/revocation checks again when consuming the publication receipt.
"""

import re
from datetime import datetime, timezone

from .errors import VerifierError
from .identity import submission_key
from .evidence import POLICY_ID
from .files import utf8
from .jsonutil import canonical, digest, integer, keys, loads, sha, text
from .scoring import validate_claim

MAX_BUNDLE_BYTES = 262144
MAX_IMPORT_BYTES = 1048576
MAX_IMPORTS = 16
MAX_ANCESTORS = 128
FORMAT = "atomic-submission-v1"
UNAVAILABLE_REASONS = {"rejected", "review_required", "dependency_unavailable"}
SOURCE_FIELDS = {"submission_id", "benchmark_id", "challenge_id", "evaluation_id", "validity_result_id",
                 "verifier_evaluation_id", "accepted", "submitted_at", "completed_at", "bundle"}


def array(value, limit):
    if type(value) is not list or len(value) > limit:
        raise VerifierError("prior_list_limit")
    return value


def hash_value(value):
    if type(value) is not str or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise VerifierError("prior_digest")


def evaluation_id(value):
    if type(value) is not str or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise VerifierError("prior_evaluation_identity")


def platform_id(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise VerifierError("prior_platform_identity")


def references(values, limit=MAX_IMPORTS):
    seen, normalized = set(), []
    for value in array(values, limit):
        keys(value, {"submission_id"})
        platform_id(value["submission_id"])
        sid = submission_key(value["submission_id"])
        if sid in seen:
            raise VerifierError("duplicate_prior_reference")
        seen.add(sid)
        normalized.append({"submission_id": sid})
    return sorted(normalized, key=lambda r: r["submission_id"])


def consumer_identity(value):
    keys(value, {"submission_id", "benchmark_id", "challenge_id", "evaluation_id", "job_id"})
    for key, item in value.items():
        if key != "challenge_id" or item is not None:
            platform_id(item)
    return value


def consumer_key(value):
    identity = consumer_identity(value)
    return {**identity, "submission_id": submission_key(identity["submission_id"])}


def completed_ns(value):
    if type(value) is not str or len(value) > 32:
        raise VerifierError("prior_completion_time")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
            raise ValueError()
        delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000
    except (ValueError, OverflowError):
        raise VerifierError("prior_completion_time") from None


def original_manifest(record):
    keys(record, {"text", "size", "sha256"})
    if type(record["text"]) is not str:
        raise VerifierError("prior_manifest_text")
    raw = record["text"].encode("utf-8")
    integer(record["size"], 1, MAX_BUNDLE_BYTES)
    if len(raw) != record["size"] or sha(raw) != record["sha256"]:
        raise VerifierError("prior_manifest_digest")
    manifest = loads(utf8(raw), MAX_BUNDLE_BYTES)
    keys(manifest, {"version", "title", "claims", "experiments"}, {"checks", "prior_results"})
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise VerifierError("prior_manifest_version")
    text(manifest["title"], 300)
    claims = array(manifest["claims"], 32)
    if not claims:
        raise VerifierError("prior_claims_required")
    for claim in claims:
        # File existence was checked in the authentic original intake. Only the
        # exact manifest is exported; no copied proof or judge-written theorem.
        paths = claim.get("evidence", []) if type(claim) is dict else []
        if type(paths) is not list or any(type(p) is not str for p in paths):
            raise VerifierError("prior_manifest_evidence")
        validate_claim(claim, paths)
    references(manifest.get("prior_results", []))
    return manifest


def validate_bundle(bundle):
    if len(canonical(bundle)) > MAX_BUNDLE_BYTES:
        raise VerifierError("prior_bundle_byte_limit")
    keys(bundle, {"version", "policy_id", "simulation", "source", "judgment", "ancestors", "sha256"})
    if type(bundle["version"]) is not int or bundle["version"] != 1:
        raise VerifierError("prior_unsupported_representation")
    text(bundle["policy_id"], 128)
    keys(bundle["judgment"], {"valid", "reason"})
    if bundle["judgment"]["valid"] is not True or bundle["simulation"] is not False:
        raise VerifierError("prior_final_accepted_required")
    text(bundle["judgment"]["reason"], 4000)
    if bundle["sha256"] != digest({k: v for k, v in bundle.items() if k != "sha256"}):
        raise VerifierError("prior_bundle_digest")
    source = bundle["source"]
    keys(source, {"evaluation_id", "submission_sha256", "manifest", "intake_sha256", "finalized_at_ns",
                  "intake_created_at_ns", "review_sha256", "dossier_sha256", "judgment_sha256", "bindings"})
    evaluation_id(source["evaluation_id"])
    integer(source["finalized_at_ns"], 1, 2**63 - 1)
    integer(source["intake_created_at_ns"], 1, source["finalized_at_ns"])
    for field in ("submission_sha256", "intake_sha256", "review_sha256", "dossier_sha256", "judgment_sha256"):
        hash_value(source[field])
    original_manifest(source["manifest"])
    bindings = source["bindings"]
    keys(bindings, {"evaluation_id", "config", "submission", "verifier", "context"})
    if bindings["evaluation_id"] != source["evaluation_id"] or bindings["submission"] != source["submission_sha256"]:
        raise VerifierError("prior_source_binding")
    for field in ("config", "submission", "verifier", "context"):
        hash_value(bindings[field])
    # Declared references may have supplied no accepted support. Exact lineage
    # comes from the authenticated intake partition and publication receipt, not
    # from interpreting every reference in the original manifest as a dependency.
    references(bundle["ancestors"], MAX_ANCESTORS)
    return bundle


def source_bundle(source):
    encoded = source["bundle"]
    keys(encoded, {"format", "sha256", "bytes", "text"})
    if type(encoded["text"]) is not str:
        raise VerifierError("prior_import_snapshot")
    raw = encoded["text"].encode("utf-8")
    integer(encoded["bytes"], 1, MAX_BUNDLE_BYTES)
    if len(raw) != encoded["bytes"] or sha(raw) != encoded["sha256"]:
        raise VerifierError("prior_import_pin")
    if encoded["format"] != FORMAT:
        raise VerifierError("prior_unsupported_representation")
    return validate_bundle(loads(raw, MAX_BUNDLE_BYTES))


def validate_source(source):
    keys(source, SOURCE_FIELDS)
    for field in ("submission_id", "benchmark_id", "evaluation_id", "validity_result_id"):
        platform_id(source[field])
    if source["challenge_id"] is not None:
        platform_id(source["challenge_id"])
    evaluation_id(source["verifier_evaluation_id"])
    if source["accepted"] is not True:
        raise VerifierError("prior_dependency_changed")
    # No comparison to consumer creation or other judgment times: fresh reorg
    # of an older submission must still import its original earlier dependencies.
    completed_ns(source["submitted_at"])
    completed_ns(source["completed_at"])
    bundle = source_bundle(source)
    if source["verifier_evaluation_id"] != bundle["source"]["evaluation_id"]:
        raise VerifierError("prior_native_evaluation_binding")
    return bundle


def discovery_results(value):
    keys(value, SOURCE_FIELDS | {"version"})
    if type(value["version"]) is not int or value["version"] != 1:
        raise VerifierError("prior_discovery_shape")
    bundle = validate_source({k: v for k, v in value.items() if k != "version"})
    return {"submission_id": value["submission_id"], "authorizes_intake": False,
            "policy_id": bundle["policy_id"], "manifest": bundle["source"]["manifest"],
            "judgment": bundle["judgment"]}


def intake_imports(snapshot, manifest, eid, created):
    requested = references(manifest.get("prior_results", []))
    if snapshot is None:
        if requested:
            raise VerifierError("prior_platform_context_required")
        return [], []
    if len(canonical(snapshot)) > 2 * 1024 * 1024:
        raise VerifierError("prior_snapshot_byte_limit")
    keys(snapshot, {"version", "snapshot_id", "consumer", "references", "sources", "ancestors"})
    if type(snapshot["version"]) is not int or snapshot["version"] != 1:
        raise VerifierError("prior_snapshot_version")
    platform_id(snapshot["snapshot_id"])
    consumer = consumer_key(snapshot["consumer"])
    pins, unavailable = {}, set()
    for ref in array(snapshot["references"], MAX_IMPORTS):
        field = "unavailable" if type(ref) is dict and "unavailable" in ref else "bundle_sha256"
        keys(ref, {"submission_id", field})
        platform_id(ref["submission_id"])
        sid = submission_key(ref["submission_id"])
        if sid in pins or sid in unavailable:
            raise VerifierError("duplicate_prior_reference")
        if sid == consumer["submission_id"]:
            raise VerifierError("prior_self_or_cyclic_import")
        if "unavailable" in ref:
            if type(ref["unavailable"]) is not str or ref["unavailable"] not in UNAVAILABLE_REASONS:
                raise VerifierError("prior_unavailable_reason")
            unavailable.add(sid)
        else:
            hash_value(ref["bundle_sha256"])
            pins[sid] = ref["bundle_sha256"]
    if (set(pins) | unavailable) != {r["submission_id"] for r in requested}:
        raise VerifierError("prior_reference_snapshot_binding")
    selected, ancestry, source_ids, graph, total = [], set(), set(), {}, 0
    for source in array(snapshot["sources"], MAX_IMPORTS):
        bundle = validate_source(source)
        sid = submission_key(source["submission_id"])
        if sid in source_ids:
            raise VerifierError("duplicate_prior_submission")
        source_ids.add(sid)
        if sid == consumer["submission_id"] or any(submission_key(a["submission_id"]) in (sid, consumer["submission_id"]) for a in bundle["ancestors"]):
            raise VerifierError("prior_self_or_cyclic_import")
        encoded = source["bundle"]
        if pins.get(sid) != encoded["sha256"]:
            raise VerifierError("prior_reference_snapshot_binding")
        total += encoded["bytes"]
        if total > MAX_IMPORT_BYTES:
            raise VerifierError("prior_import_byte_limit")
        graph[sid] = {a["submission_id"] for a in references(bundle["ancestors"], MAX_ANCESTORS)}
        ancestry.update(graph[sid])
        selected.append({"submission_id": sid, "manifest": bundle["source"]["manifest"],
                         "judgment": bundle["judgment"], "provenance": {
                             "policy_id": bundle["policy_id"], "source": {k: v for k, v in source.items() if k != "bundle"},
                             "original_source": {k: v for k, v in bundle["source"].items() if k != "manifest"},
                             "bundle_sha256": encoded["sha256"]}})
    if source_ids != set(pins):
        raise VerifierError("prior_reference_snapshot_binding")
    if ancestry & unavailable:
        raise VerifierError("prior_unavailable_ancestor")
    # Check the bounded graph already supplied; never fetch recursively. Native
    # immutable ordering remains authority for forward edges and unseen sources.
    remaining = set(source_ids)
    while remaining:
        ready = {sid for sid in remaining if not graph[sid] & remaining}
        if not ready:
            raise VerifierError("prior_self_or_cyclic_import")
        remaining -= ready
    attested = []
    for item in array(snapshot["ancestors"], MAX_ANCESTORS):
        keys(item, {"submission_id", "accepted"})
        if item["accepted"] is not True:
            raise VerifierError("prior_dependency_changed")
        attested.append({"submission_id": item["submission_id"]})
    attested = references(attested, MAX_ANCESTORS)
    if {a["submission_id"] for a in attested} != ancestry - source_ids:
        raise VerifierError("prior_ancestor_snapshot")
    ancestry.update(source_ids)
    if len(ancestry) > MAX_ANCESTORS:
        raise VerifierError("prior_aggregate_limit")
    return sorted(selected, key=lambda r: r["submission_id"]), [{"submission_id": sid} for sid in sorted(ancestry)]


def bundle_from_dossier(dossier):
    # Caller reconstructs and verifies the complete current phase chain first.
    data = dossier["data"]
    judgment = data["judgment"]
    if dossier["simulation"] or not judgment["valid"]:
        raise VerifierError("prior_final_accepted_required")
    intake = data["phases"][0]
    source = {"evaluation_id": dossier["bindings"]["evaluation_id"], "bindings": dossier["bindings"],
              "submission_sha256": dossier["bindings"]["submission"], "manifest": intake["data"]["files"]["submission.json"],
              "intake_sha256": intake["sha256"], "intake_created_at_ns": intake["data"]["created_at_ns"],
              "finalized_at_ns": data["finalized_at_ns"], "review_sha256": data["selected_review_sha256"],
              "dossier_sha256": dossier["sha256"], "judgment_sha256": digest(judgment)}
    _, ancestors = intake_imports(intake["data"]["prior_snapshot"], intake["data"]["manifest"], source["evaluation_id"], source["intake_created_at_ns"])
    bundle = {"version": 1, "policy_id": POLICY_ID, "simulation": False, "source": source,
              "judgment": {k: judgment[k] for k in ("valid", "reason")}, "ancestors": ancestors}
    bundle["sha256"] = digest(bundle)
    return validate_bundle(bundle)
