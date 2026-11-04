"""Synthetic original accepted sources; never uses live providers or artifacts."""

from contextlib import contextmanager
from copy import deepcopy
import io
from pathlib import Path
import zipfile

from llm_verifier.artifacts import intake_snapshot
from llm_verifier import derivation, pipeline
from llm_verifier.files import write_json
from llm_verifier.jsonutil import canonical, digest, sha
from tests.support import direct, example


def uid(n):
    return f"00000000-0000-4000-8000-{n:012x}"


def descriptor(valid=True):
    def evidence(n):
        return {"result_id": uid(n), "evaluation_id": uid(n + 1), "policy_version_id": uid(n + 2),
                "source_ref": "source:" + str(n), "job_id": uid(n + 3), "commit_sha": "a" * 40}
    return {"version": "yukon-retained-result-v1", "benchmark_id": uid(1), "submission_id": uid(2),
            "candidate_key": "digest:" + "b" * 64, "packet_id": uid(3), "score": evidence(10),
            "validity": {**evidence(20), "valid": valid},
            "destination": {"job_id": uid(30), "evaluation_id": uid(31), "packet_id": uid(32), "source_ref": "source:new"}}


def config():
    return {"version": 1, "mode": "score-derivation-v1", "id": "synthetic-derivation", "plan_path": "plan.json",
            "instructions": "Copy the accepted exact rational coefficient without reviewing proof validity.",
            "payload_schema": {"type": "object", "additionalProperties": False, "required": ["coefficient"],
                "properties": {"coefficient": {"type": "string", "maxLength": 64}}},
            "provider": {"model": "test-model", "max_completion_tokens": 256, "max_attempts": 1},
            "limits": {"total_review_tokens": 256, "total_provider_requests": 1, "total_review_seconds": 30}}


def archive(root, name, artifact_id, value):
    raw = canonical(value) + b"\n"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr(name + ".json", raw)
    content = output.getvalue()
    root.mkdir(exist_ok=True)
    (root / (artifact_id + ".zip")).write_bytes(content)
    return {"repository": "organizer/challenge", "run_id": "100", "run_attempt": "1",
            "artifact_id": artifact_id, "artifact_name": name, "archive_sha256": sha(content),
            "path": name + ".json", "file_sha256": sha(raw)}


@contextmanager
def case(*, valid=True, action=None, policy="atomic-claims-v1"):
    def mutate(raw):
        raw["acceptance_policy"] = policy
    with example("scored-proof", mutate=mutate) as (root, effective, _):
        old = intake_snapshot(effective, root / "submission",
            {"kind": "github-actions", "candidate_revision": "a" * 40, "run_id": "100:1"})
        selected = descriptor(valid)
        evaluation = {"policies": {"scoring": {"id": "new-score", "retained_result": selected["version"]},
                                   "validity": effective["config"]["policies"]["validity"]},
                      "fields": {"scores": True, "validity": False}, "retained_result": selected}
        archives = root / "archives"
        refs = None
        bundle = None
        if valid:
            initial, first = direct(root, old)
            bundle = pipeline.export_results(pipeline.dossier(old, initial, first))
            refs = {"intake": archive(archives, "intake", "101", old),
                    "accepted_results": archive(archives, "accepted-results", "102", bundle)}
        cfg = config()
        raw = canonical(cfg) + b"\n"
        (root / "derivation.json").write_bytes(raw)
        entry = {"origin": {k: v for k, v in selected.items() if k != "destination"},
                 "action": action or ("derive" if valid else "not_applicable"),
                 "reason": "Original record has an applicable declared coefficient." if valid else "Retained validity is false.",
                 "source_files_sha256": old["bindings"]["submission"], "artifacts": refs}
        plan = {"version": derivation.PLAN_VERSION, "config_sha256": sha(raw), "entries": [entry]}
        write_json(root / "plan.json", plan)
        context = {"kind": "github-actions", "candidate_revision": "d" * 40, "verifier_revision": "e" * 40,
                   "run_id": "200:1", "repository": "organizer/challenge", "score_path": ".yukon/score.json"}
        yield {"root": root, "config": cfg, "plan": plan, "old": old, "bundle": bundle,
               "archives": archives, "evaluation": evaluation, "context": context}


def prepare(fixture):
    return derivation.prepare(fixture["root"], "derivation.json", "submission", fixture["evaluation"],
                              fixture["context"], fixture["archives"])


def decision(intake, status="derived"):
    content = derivation.sources(intake)["claim:0"]
    return {"status": status, "payload": {"coefficient": "1/2"} if status == "derived" else None,
            "reason": "This is the exact coefficient in the retained full declaration.",
            "source_refs": [{"source": "claim:0", "sha256": sha(content.encode()), "start_line": 1,
                             "end_line": len(content.split("\n")), "quote": content}]}
