"""Editable source starters and validation using the production contracts."""

import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .checks import check_validation
from .config import identifier, load_config, submission_references_enabled
from .errors import VerifierError
from .files import directory_fd, read_bytes, read_json, write_bytes, snapshot
from .submission import preflight_manifest
from .input_schema import validate_value
from .jsonutil import canonical, keys
from .runner import DockerRunner
from .scoring import validate_objective_claims

TEMPLATES = Path(__file__).parent / "templates"
# The starter marks every problem-specific value with a "TODO:" placeholder line.
TODO = re.compile(r"\bTODO:")
# Starter files are tiny; a larger file cannot be an unfilled starter file, and intake enforces size.
SCAN_BYTES = 262144
# Reviewer settings qualified on a hosted review, filled in by init for that model.
KNOWN_REVIEWERS = {
    "gpt-6-astra": ({"reasoning_effort": "high", "max_completion_tokens": 32768, "timeout_seconds": 1200,
                     "background": True, "max_attempts": 1}, {"total_review_seconds": 2400}),
}


def _lines(path):
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > SCAN_BYTES:
            return []
        return Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def _json(path):
    try:
        return json.loads("\n".join(_lines(path)))
    except (ValueError, RecursionError):
        return None


def _strings(value, path=""):
    if type(value) is str:
        yield path, value.strip()
    elif type(value) is dict:
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else key)
    elif type(value) is list:
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}.{index}" if path else str(index))


def _starter(names):
    """The starter's placeholder lines and, for JSON files, its placeholder values."""
    lines, values = set(), set()
    for name in names:
        path = TEMPLATES / "starter" / name
        lines |= {line.strip().rstrip(",") for line in _lines(path) if TODO.search(line)}
        if name.endswith(".json"):
            values |= {text for _, text in _strings(_json(path)) if TODO.search(text)}
    return lines, values


def _found(root, name, starter):
    lines, values = starter
    data = _json(root / name) if name.endswith(".json") else None
    if data is not None:
        # Decoded values, so reformatted or compacted JSON is still checked.
        try:
            return [f"{name}:{path}" for path, text in _strings(data) if text in values]
        except RecursionError:
            pass  # Deeply nested participant JSON: fall back to whole-line matching.
    return [f"{name}:{n}" for n, line in enumerate(_lines(root / name), 1) if line.strip().rstrip(",") in lines]


def unfilled(root):
    """Locations that still repeat one of the starter's own placeholders.

    Whole starter placeholders, not the word TODO, decide: organizer notes and participant
    text stay valid. Participants edit submission/, and check runs on their pull requests.
    """
    root = Path(root)
    rules = "rules.md"
    config = _json(root / "verifier.json")
    configured = config.get("rules_path") if type(config) is dict else None
    if type(configured) is str and configured and not os.path.isabs(configured) and ".." not in Path(configured).parts:
        rules = configured
    found = (_found(root, rules, _starter(["rules.md"])) + _found(root, "README.md", _starter(["README.md"]))
             + _found(root, "verifier.json", _starter(["verifier.json"])))
    submission = root / "submission"
    if submission.is_dir() and not submission.is_symlink():
        starter = _starter([str(p.relative_to(TEMPLATES / "starter"))
                            for p in (TEMPLATES / "starter" / "submission").rglob("*") if p.is_file()])
        for path in sorted(p for p in submission.rglob("*") if p.is_file()):
            found += _found(root, str(path.relative_to(root)), starter)
    return found


def refuse_unfilled(root):
    todo = unfilled(root)
    if todo:
        raise VerifierError("unfilled_todo: " + ", ".join(todo))


def formatted(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def solver_documents(root, repository=None, revision=None):
    """Prepare only our marked blocks; leave all surrounding author bytes intact."""
    if revision is None:
        location = (
            "Read `docs/solving.md` and `skills/llm-verifier-solver/SKILL.md` in the\n"
            "verifier source used for this install. A wheel also ships both under\n"
            "`share/llm-verifier` in its Python environment. Locate that directory with\n"
            "the same Python used to install the CLI:\n\n"
            "```sh\n"
            "python -c \"import sysconfig; print(sysconfig.get_path('data') + '/share/llm-verifier')\"\n"
            "```\n\n"
            "No hosted verifier revision is pinned yet. Before publishing, the author\n"
            "must run `llm-verifier github` with the selected full commit to refresh\n"
            "these pointers. If the matching guide is unavailable, ask the author\n"
            "for it; do not substitute a guide from a moving branch."
        )
    else:
        base = f"https://github.com/{repository}/blob/{revision}"
        location = (f"Read the [solver guide]({base}/docs/solving.md) and\n"
                    f"[solver skill]({base}/skills/llm-verifier-solver/SKILL.md)\n"
                    "from the same pinned verifier revision as this challenge.")
    block = read_bytes(TEMPLATES / "solver-guidance.md").replace(
        b"SOLVER_GUIDANCE_LOCATION", location.encode()).rstrip(b"\n")
    begin, end = block.splitlines()[0], block.splitlines()[-1]
    pattern = re.compile(rb"(?ms)^" + re.escape(begin) + rb"\r?\n.*?^" + re.escape(end) + rb"(?=\r?$)")
    documents = {}
    for name in ("README.md", "AGENTS.md"):
        path = Path(root) / name
        original = read_bytes(path) if os.path.lexists(path) else b""
        if begin in original or end in original:
            match = pattern.search(original)
            if original.count(begin) != 1 or original.count(end) != 1 or match is None:
                raise VerifierError("ambiguous_solver_guidance_block: " + name)
            documents[name] = original[:match.start()] + block + original[match.end():]
        else:
            documents[name] = original + (b"\n\n" if original else b"") + block + b"\n"
    return documents


def initialize(output, name, model):
    identifier(name)
    if model == "SELECT_OPENAI_MODEL":
        raise VerifierError("explicit_model_required")
    # The form cannot pass full validation until it is filled, so check the model here as config does.
    if (type(model) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", model)
            or model.lower() in {"auto", "default"}):
        raise VerifierError("explicit_provider_model_required")
    output = Path(os.path.abspath(output))
    with directory_fd(output.parent):
        if os.path.lexists(output):
            raise VerifierError("output_already_exists")
        staging = Path(tempfile.mkdtemp(prefix=".llmv-init-", dir=output.parent))
        try:
            shutil.copytree(TEMPLATES / "starter", staging, dirs_exist_ok=True)
            config = read_json(staging / "verifier.json")
            config["id"] = name
            config["review_standard"] = f"{name}-review-v1"
            config["provider"]["model"] = model
            if model in KNOWN_REVIEWERS:
                provider, limits = KNOWN_REVIEWERS[model]
                config["provider"].update(provider)
                config["limits"].update(limits)
            write_bytes(staging / "verifier.json", formatted(config))
            for path, content in solver_documents(staging).items():
                write_bytes(staging / path, content)
            # The starter is a form: problem-specific TODOs remain until the author fills them.
            report = {"unfilled": unfilled(staging)}
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
        "submissionAdmission": {"version": 1, "submissionReferencesEnabled": False},
        "setupCommand": ["llm-verifier", "intake", "--root", ".", "--out", ".yukon/work/intake.json"],
        "benchmarkCommand": ["llm-verifier", "finalize", "--intake", ".yukon/work/intake.json",
                             "--initial", ".yukon/work/initial.json", "--initial-review", ".yukon/work/review.json",
                             "--score", ".yukon/score.json", "--dossier", ".yukon/dossier.json"],
        "scorePath": ".yukon/score.json", "maxSubmissionBytes": config["limits"]["max_submission_bytes"],
        "runner": {"provider": "github-actions", "workflow": "benchmark.yml",
                   "scoreArtifactName": "benchmark-score", "maxConcurrentWorkflows": 2}}]}


def check(root):
    refuse_unfilled(root)
    effective = load_config(root, "verifier.json", "submission")
    files = snapshot(Path(root) / "submission", effective["config"]["limits"])
    submission, audit = preflight_manifest(files, effective["config"])
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
        submission_references_enabled(tracks[0].get("submissionAdmission"))
        expected = manifest(config)["tracks"][0]
        fields = ("objectives", "paretoPolicies", "editablePaths", "maxSubmissionBytes", "direction")
        if current.get("schemaVersion") != 2 or any(canonical(tracks[0].get(k)) != canonical(expected[k]) for k in fields):
            raise VerifierError("stale_benchmark_run_github_command")
    return {"valid": True, "submission_files": len(files), "checker_cases": len(cases),
            **({"preprocessing": audit} if audit is not None else {})}


def github(root, repository, revision):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise VerifierError("immutable_verifier_pin_required")
    refuse_unfilled(root)
    effective = load_config(root, "verifier.json", "submission")
    if not effective["config"]["objectives"]:
        raise VerifierError("yukon_requires_objective")
    generated = manifest(effective["config"])
    benchmark = Path(root) / "benchmark.json"
    if os.path.lexists(benchmark):
        current = read_json(benchmark)
        tracks = current.get("tracks", []) if type(current) is dict else []
        if type(tracks) is not list or len(tracks) != 1 or type(tracks[0]) is not dict:
            raise VerifierError("single_track_benchmark_required")
        if "submissionAdmission" in tracks[0]:
            admission = tracks[0]["submissionAdmission"]
            submission_references_enabled(admission)
            generated["tracks"][0]["submissionAdmission"] = admission
    # Validate both documentation targets before changing any generated output.
    documents = solver_documents(root, repository, revision)
    write_bytes(benchmark, formatted(generated))
    files = ["benchmark.json"]
    for name in ("benchmark.yml", "checker-tests.yml"):
        workflow = read_bytes(TEMPLATES / name).decode().replace("VERIFIER_REPOSITORY", repository).replace("VERIFIER_REVISION", revision)
        path = ".github/workflows/" + name
        write_bytes(Path(root) / path, workflow.encode())
        files.append(path)
    for path, content in documents.items():
        write_bytes(Path(root) / path, content)
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
