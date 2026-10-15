"""Content-addressed phase envelopes. Trust comes from the organizer job channel."""

from pathlib import Path
import re
import uuid

from . import __version__
from .config import verify_effective
from .errors import VerifierError
from .files import snapshot
from .jsonutil import canonical, digest, keys, loads, sha
from .submission import manifest_from

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


def intake_snapshot(effective, candidate_root, context=None, simulation=False):
    verify_effective(effective)
    files = snapshot(candidate_root, effective["config"]["limits"])
    manifest = manifest_from(files, effective["config"]["limits"], effective["config"].get("checkers", []))
    code = implementation()
    context = context or {"kind": "local", "revision": "unasserted; use content hash"}
    bindings = {"evaluation_id": uuid.uuid4().hex, "config": digest(effective),
                "submission": digest(files), "verifier": code["content_sha256"], "context": digest(context)}
    return seal("intake", bindings, {"effective": effective, "files": files,
                                     "manifest": manifest, "implementation": code, "context": context}, simulation=simulation)


def verify_intake(intake, expected=None):
    data = verify_envelope(intake, "intake", parents=[])
    if expected is not None and intake["sha256"] != expected:
        raise VerifierError("unexpected_intake")
    keys(data, {"effective", "files", "manifest", "implementation", "context"})
    verify_effective(data["effective"])
    manifest = manifest_from(data["files"], data["effective"]["config"]["limits"], data["effective"]["config"].get("checkers", []))
    if canonical(manifest) != canonical(data["manifest"]):
        raise VerifierError("manifest_binding")
    bindings = intake["bindings"]
    keys(bindings, {"evaluation_id", "config", "submission", "verifier", "context"})
    if type(bindings["evaluation_id"]) is not str or not re.fullmatch("[a-f0-9]{32}", bindings["evaluation_id"]):
        raise VerifierError("evaluation_identity")
    for field, obj in (("config", data["effective"]), ("submission", data["files"]), ("context", data["context"])):
        if bindings[field] != digest(obj):
            raise VerifierError("intake_binding")
    if canonical(data["implementation"]) != canonical(implementation()) or bindings["verifier"] != data["implementation"]["content_sha256"]:
        raise VerifierError("verifier_revision_mismatch")
    return data
