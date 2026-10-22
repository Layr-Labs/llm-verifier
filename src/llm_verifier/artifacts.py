"""Content-addressed phase envelopes. Trust comes from the organizer job channel."""

from pathlib import Path
import re
import uuid
import time

from . import __version__
from .config import submission_references_enabled, verify_effective
from .errors import VerifierError
from .files import snapshot
from .jsonutil import canonical, digest, keys, loads, sha
from .submission import manifest_from
from .scoring import validate_objective_claims
from .prior import intake_imports, consumer_key
from .jsonutil import integer

HASH = re.compile(r"[a-f0-9]{64}\Z")


def implementation():
    root = Path(__file__).parent
    content = {p.name: sha(p.read_bytes()) for p in sorted(root.glob("*.py"))}
    return {"package": "llm-verifier", "version": __version__, "content_sha256": digest(content),
            "scope": "all installed llm_verifier/*.py source bytes", "files": content}


def seal(stage, bindings, data, parents=(), simulation=False):
    value = {"version": 1, "stage": stage, "bindings": bindings, "parents": list(parents),
             "simulation": simulation, "data": data}
    value["sha256"] = digest(value)
    return loads(canonical(value))


def verify_envelope(value, stage, intake=None, parents=None):
    keys(value, {"version", "stage", "bindings", "parents", "simulation", "data", "sha256"})
    if type(value["version"]) is not int or value["version"] != 1 or value["stage"] != stage or type(value["simulation"]) is not bool:
        raise VerifierError("artifact_stage")
    if value["sha256"] != digest({k: v for k, v in value.items() if k != "sha256"}):
        raise VerifierError("artifact_digest")
    if intake is not None and canonical(value["bindings"]) != canonical(intake["bindings"]):
        raise VerifierError("artifact_binding")
    if parents is not None and canonical(value["parents"]) != canonical([p["sha256"] for p in parents]):
        raise VerifierError("artifact_parent")
    if parents and any(p["simulation"] for p in parents) and not value["simulation"]:
        raise VerifierError("simulation_downgrade")
    return value["data"]


def admit_submission(effective, manifest):
    # The original candidate-bound policy travels in the effective snapshot.
    # No current environment, mutable challenge setting or resolver result can
    # authorize an originally disabled candidate during replay or a later phase.
    enabled = submission_references_enabled(effective["evaluation"].get("admission"))
    if manifest.get("prior_results") and not enabled:
        raise VerifierError("submission_references_disabled")


def intake_snapshot(effective, candidate_root, context=None, simulation=False, *, accepted_results_context=None):
    verify_effective(effective)
    files = snapshot(candidate_root, effective["config"]["limits"])
    manifest = manifest_from(files, effective["config"]["limits"], effective["config"].get("checkers", []))
    validate_objective_claims(effective["config"]["objectives"], manifest["claims"])
    admit_submission(effective, manifest)
    code = implementation()
    context = dict(context or {"kind": "local", "revision": "unasserted; use content hash"})
    created = time.time_ns()
    prior_snapshot = None
    if accepted_results_context is not None:
        from .accepted import context_identity, resolve
        context["accepted_results"] = context_identity(accepted_results_context)
        prior_snapshot = resolve(accepted_results_context, manifest.get("prior_results", []))
        if canonical(consumer_key(prior_snapshot.get("consumer"))) != canonical(consumer_key(context["accepted_results"])):
            raise VerifierError("prior_consumer_binding")
    bindings = {"evaluation_id": uuid.uuid4().hex, "config": digest(effective),
                "submission": digest(files), "verifier": code["content_sha256"], "context": digest(context)}
    intake_imports(prior_snapshot, manifest, bindings["evaluation_id"], created)
    return seal("intake", bindings, {"effective": effective, "files": files,
                                     "manifest": manifest, "implementation": code, "context": context, "created_at_ns": created, "prior_snapshot": prior_snapshot}, simulation=simulation)


def verify_intake(intake, expected=None):
    data = verify_envelope(intake, "intake", parents=[])
    if expected is not None and intake["sha256"] != expected:
        raise VerifierError("unexpected_intake")
    keys(data, {"effective", "files", "manifest", "implementation", "context", "created_at_ns", "prior_snapshot"})
    verify_effective(data["effective"])
    manifest = manifest_from(data["files"], data["effective"]["config"]["limits"], data["effective"]["config"].get("checkers", []))
    validate_objective_claims(data["effective"]["config"]["objectives"], manifest["claims"])
    if canonical(manifest) != canonical(data["manifest"]):
        raise VerifierError("manifest_binding")
    admit_submission(data["effective"], manifest)
    bindings = intake["bindings"]
    keys(bindings, {"evaluation_id", "config", "submission", "verifier", "context"})
    if type(bindings["evaluation_id"]) is not str or not re.fullmatch("[a-f0-9]{32}", bindings["evaluation_id"]):
        raise VerifierError("evaluation_identity")
    integer(data["created_at_ns"], 1, 2**63 - 1)
    intake_imports(data["prior_snapshot"], manifest, bindings["evaluation_id"], data["created_at_ns"])
    if data["prior_snapshot"] is not None:
        if canonical(consumer_key(data["context"].get("accepted_results"))) != canonical(consumer_key(data["prior_snapshot"]["consumer"])):
            raise VerifierError("prior_consumer_binding")
    elif "accepted_results" in data["context"]:
        raise VerifierError("prior_consumer_binding")
    for field, obj in (("config", data["effective"]), ("submission", data["files"]), ("context", data["context"])):
        if bindings[field] != digest(obj):
            raise VerifierError("intake_binding")
    if canonical(data["implementation"]) != canonical(implementation()) or bindings["verifier"] != data["implementation"]["content_sha256"]:
        raise VerifierError("verifier_revision_mismatch")
    return data
