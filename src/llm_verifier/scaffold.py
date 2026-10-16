"""Editable source starters and validation using the production contracts."""

import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .checks import check_validation
from .config import identifier, load_config
from .errors import VerifierError
from .files import directory_fd, read_bytes, read_json, write_bytes, snapshot
from .submission import manifest_from
from .input_schema import validate_value
from .jsonutil import canonical, keys
from .runner import DockerRunner
from .scoring import validate_objective_claims

TEMPLATES = Path(__file__).parent / "templates"


def formatted(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def initialize(output, name, model):
    identifier(name)
    if model == "SELECT_OPENAI_MODEL":
        raise VerifierError("explicit_model_required")
    output = Path(os.path.abspath(output))
    with directory_fd(output.parent):
        if os.path.lexists(output):
            raise VerifierError("output_already_exists")
        staging = Path(tempfile.mkdtemp(prefix=".llmv-init-", dir=output.parent))
        try:
            shutil.copytree(TEMPLATES / "starter", staging, dirs_exist_ok=True)
            config = read_json(staging / "verifier.json")
            config["id"] = name
            config["provider"]["model"] = model
            write_bytes(staging / "verifier.json", formatted(config))
            report = check(staging)
            os.rename(staging, output)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {"root": str(output), **report}


def checker_cases(root, effective):
    path = Path(root) / "checker-tests.json"
    cases = read_json(path) if os.path.lexists(path) else []
    if type(cases) is not list or len(cases) > 64:
        raise VerifierError("checker_test_count")
    registry = {c["id"]: c for c in effective["config"].get("checkers", [])}
    seen, covered = set(), set()
    for case in cases:
        keys(case, {"id", "check", "inputs", "expected"})
        identifier(case["id"])
        if (case["id"] in seen or type(case["check"]) is not str
                or case["check"] not in registry):
            raise VerifierError("checker_test_identity")
        seen.add(case["id"])
        covered.add(case["check"])
        checker = registry[case["check"]]
        validate_value(checker["input_schema"], case["inputs"])
        expected = case["expected"]
        schema = checker["output_schema"]
        if type(expected) is not dict or not expected or expected.keys() - schema["properties"].keys():
            raise VerifierError("checker_test_expected_fields")
        for field, value in expected.items():
            validate_value(schema["properties"][field], value)
    if covered != registry.keys():
        raise VerifierError("checker_tests_required")
    return cases


def manifest(config):
    return {"schemaVersion": 2, "name": config["id"], "tracks": [{
        "name": "main", "description": config["id"], "category": "math", "mode": "pareto",
        "promotionMode": "manual", "direction": config["objectives"][0]["direction"],
        "objectives": [{k: o[k] for k in ("name", "direction")} for o in config["objectives"]],
        "paretoPolicies": config["policies"], "editablePaths": ["submission"],
        "setupCommand": ["llm-verifier", "intake", "--root", ".", "--out", ".yukon/work/intake.json"],
        "benchmarkCommand": ["llm-verifier", "finalize", "--intake", ".yukon/work/intake.json",
                             "--initial", ".yukon/work/initial.json", "--initial-review", ".yukon/work/review.json",
                             "--score", ".yukon/score.json", "--dossier", ".yukon/dossier.json"],
        "scorePath": ".yukon/score.json", "maxSubmissionBytes": config["limits"]["max_submission_bytes"],
        "runner": {"provider": "github-actions", "workflow": "benchmark.yml",
                   "scoreArtifactName": "benchmark-score", "maxConcurrentWorkflows": 2}}]}


def check(root):
    effective = load_config(root, "verifier.json", "submission")
    files = snapshot(Path(root) / "submission", effective["config"]["limits"])
    submission = manifest_from(files, effective["config"]["limits"], effective["config"].get("checkers", []))
    config = effective["config"]
    validate_objective_claims(config["objectives"], submission["claims"])
    # Strict validation at trust boundaries; unrelated authoring files are not
    # part of the evaluation and do not need a second repository inventory.
    cases = checker_cases(root, effective)
    benchmark = Path(root) / "benchmark.json"
    if os.path.lexists(benchmark):
        current = read_json(benchmark)
        tracks = current.get("tracks", []) if type(current) is dict else []
        if not config["objectives"] or type(tracks) is not list or len(tracks) != 1 or type(tracks[0]) is not dict:
            raise VerifierError("single_track_benchmark_required")
        expected = manifest(config)["tracks"][0]
        fields = ("objectives", "paretoPolicies", "editablePaths", "maxSubmissionBytes", "direction")
        if current.get("schemaVersion") != 2 or any(canonical(tracks[0].get(k)) != canonical(expected[k]) for k in fields):
            raise VerifierError("stale_benchmark_run_github_command")
    return {"valid": True, "submission_files": len(files), "checker_cases": len(cases)}


def github(root, repository, revision):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise VerifierError("immutable_verifier_pin_required")
    effective = load_config(root, "verifier.json", "submission")
    if not effective["config"]["objectives"]:
        raise VerifierError("yukon_requires_objective")
    write_bytes(Path(root) / "benchmark.json", formatted(manifest(effective["config"])))
    files = ["benchmark.json"]
    for name in ("benchmark.yml", "checker-tests.yml"):
        workflow = read_bytes(TEMPLATES / name).decode().replace("VERIFIER_REPOSITORY", repository).replace("VERIFIER_REVISION", revision)
        path = ".github/workflows/" + name
        write_bytes(Path(root) / path, workflow.encode())
        files.append(path)
    return {"files": files, "verifier_revision": revision}


def test_checkers(root, *, runner=None):
    if os.environ.get("OPENAI_API_KEY"):
        raise VerifierError("checker_execution_requires_credential_free_process")
    effective = load_config(root, "verifier.json", "submission")
    cases = checker_cases(root, effective)
    config = effective["config"]
    registry = {c["id"]: c for c in config.get("checkers", [])}
    runner = runner or DockerRunner(config["runtime"], config["limits"])
    for case in cases:
        checker = registry[case["check"]]
        files = {p: effective["checker_files"][p] for p in checker["files"]}
        result = runner.run(files, checker["source"], case["inputs"], config["limits"]["run_seconds"])
        if check_validation(checker, result)["status"] != "valid":
            raise VerifierError("checker_test_invalid_execution_" + case["id"])
        if any(k not in result["result"] or canonical(result["result"][k]) != canonical(v) for k, v in case["expected"].items()):
            raise VerifierError("checker_test_mismatch_" + case["id"])
    return {"passed": len(cases)}
