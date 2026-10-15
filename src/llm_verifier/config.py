"""Closed organizer configuration and exact Yukon policy selection."""

from pathlib import Path
import re

from .errors import VerifierError
from .files import outside, read_bytes, relative, snapshot_declared, utf8, validate_files
from .input_schema import validate_schema
from .jsonutil import canonical, integer, keys, loads, number, text
from .scoring import validate_sources

DEFAULT_IMAGE = "python:3.12.12-slim-bookworm@sha256:2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301"
IMAGE = re.compile(r"(?:docker\.io/library/)?python:[A-Za-z0-9._-]+@sha256:[a-f0-9]{64}\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
# default, minimum, hard maximum. Time budgets reserve full allotted slots,
# not observed fast completion, so a judge cannot buy unlimited cheap reruns.
LIMITS = {
    "max_files": (32, 1, 128), "max_file_bytes": (65536, 1, 262144),
    "max_submission_bytes": (262144, 1, 1048576),
    "max_initial_runs": (3, 0, 12), "max_followups": (3, 0, 8),
    "run_seconds": (20, 1, 120), "initial_seconds": (60, 1, 600),
    "followup_seconds": (60, 1, 600), "total_seconds": (60, 1, 600),
    "stdout_bytes": (65536, 64, 262144), "stderr_bytes": (65536, 64, 262144),
    "total_output_bytes": (393216, 128, 4194304),
    "memory_mb": (256, 64, 1024), "cpus": (1, 1, 4),
    "pids": (32, 8, 128), "tmpfs_mb": (16, 1, 64),
    "max_prompt_bytes": (2097152, 1024, 8388608),
    "total_review_seconds": (180, 1, 600), "total_provider_requests": (4, 1, 6),
}
PROVIDER_DEFAULTS = {"max_tokens": 8192, "timeout_seconds": 60, "max_attempts": 2,
                     "max_response_bytes": 262144, "response_format": "json_schema",
                     "zdr": True, "data_collection": "deny", "upstream": None}


def identifier(value):
    if type(value) is not str or not ID.fullmatch(value):
        raise VerifierError("invalid_identifier")


def normalize(raw):
    keys(raw, {"version", "id", "rules_path", "review_standard", "objectives", "allow_unscored", "policies", "supported_policies", "provider"}, {"limits", "runtime", "checkers"})
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise VerifierError("unsupported_config_version")
    identifier(raw["id"])
    relative(raw["rules_path"])
    text(raw["review_standard"], 2000)
    if type(raw["allow_unscored"]) is not bool:
        raise VerifierError("allow_unscored_boolean")
    objectives = raw["objectives"]
    if type(objectives) is not list or len(objectives) > 8:
        raise VerifierError("objective_count")
    names = set()
    for item in objectives:
        keys(item, {"name", "direction", "unit", "semantics"}, {"score_source"})
        identifier(item["name"])
        if item["name"] in names or item["direction"] not in ("+", "-"):
            raise VerifierError("objective_identity")
        names.add(item["name"])
        text(item["unit"], 100)
        text(item["semantics"], 4000)
    if not objectives and not raw["allow_unscored"]:
        raise VerifierError("zero_objectives_require_unscored")
    keys(raw["policies"], {"scoring", "validity"})
    keys(raw["supported_policies"], {"scoring", "validity"})
    for field in ("scoring", "validity"):
        options = raw["supported_policies"][field]
        if type(options) is not list or not 1 <= len(options) <= 16:
            raise VerifierError("policy_options")
        selectors = set()
        for option in options:
            keys(option, {"selector", "instructions"})
            encoded = canonical(option["selector"])
            if len(encoded) > 8192 or encoded in selectors:
                raise VerifierError("policy_selector")
            selectors.add(encoded)
            text(option["instructions"], 16000)
        if canonical(raw["policies"][field]) not in selectors:
            raise VerifierError("unsupported_default_policy")
    supplied = raw.get("limits", {})
    keys(supplied, set(), set(LIMITS))
    limits = {name: supplied.get(name, spec[0]) for name, spec in LIMITS.items()}
    for name, (_, low, high) in LIMITS.items():
        integer(limits[name], low, high)
    checkers = raw.get("checkers", [])
    if type(checkers) is not list or len(checkers) > 16:
        raise VerifierError("checker_count")
    checker_ids, paths = set(), set()
    for checker in checkers:
        keys(checker, {"id", "description", "source", "files", "input_schema", "output_schema", "required_for_score"})
        identifier(checker["id"])
        if checker["id"] in checker_ids:
            raise VerifierError("duplicate_checker_id")
        checker_ids.add(checker["id"])
        text(checker["description"], 4000)
        source = relative(checker["source"])
        bundle = checker["files"]
        if type(bundle) is not list or not 1 <= len(bundle) <= limits["max_files"]:
            raise VerifierError("checker_file_count")
        for path in bundle:
            relative(path)
        if len(set(bundle)) != len(bundle) or source not in bundle or not source.endswith(".py"):
            raise VerifierError("checker_python_entrypoint")
        paths.update(bundle)
        for field in ("input_schema", "output_schema"):
            validate_schema(checker[field])
            if checker[field]["type"] != "object":
                raise VerifierError("checker_schema_root_object_required")
        if type(checker["required_for_score"]) is not bool:
            raise VerifierError("required_for_score_boolean")
        output = checker["output_schema"]
        if checker["required_for_score"] and (
            "passed" not in output["required"] or output["properties"].get("passed") != {"type": "boolean"}
        ):
            raise VerifierError("required_checker_boolean_passed")
    if len(paths) > limits["max_files"]:
        raise VerifierError("checker_registry_file_limit")
    validate_sources(objectives, checkers)
    runtime = raw.get("runtime", {"image": DEFAULT_IMAGE, "platform": "linux/amd64"})
    keys(runtime, {"image", "platform"})
    if type(runtime["image"]) is not str or not IMAGE.fullmatch(runtime["image"]) or runtime["platform"] != "linux/amd64":
        raise VerifierError("pinned_python_runtime_required")
    keys(raw["provider"], {"model"}, set(PROVIDER_DEFAULTS))
    provider = {**PROVIDER_DEFAULTS, **raw["provider"]}
    # No fallback aliases, list of models or implicitly selected user defaults.
    if type(provider["model"]) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+", provider["model"]):
        raise VerifierError("explicit_provider_model_required")
    integer(provider["max_tokens"], 128, 32768)
    integer(provider["timeout_seconds"], 1, 180)
    integer(provider["max_attempts"], 1, 3)
    integer(provider["max_response_bytes"], 1024, 1048576)
    if provider["response_format"] not in {"json_schema", "json_object"} or type(provider["zdr"]) is not bool or provider["data_collection"] not in {"allow", "deny"}:
        raise VerifierError("provider_settings")
    if provider["upstream"] is not None:
        text(provider["upstream"], 100)
    return loads(canonical({**raw, "limits": limits, "runtime": runtime, "provider": provider}))


def select_evaluation(config, evaluation=None):
    if evaluation is None:
        evaluation = {"policies": config["policies"], "fields": {"scores": True, "validity": True}}
    keys(evaluation, {"policies", "fields"}, {"admission"})
    keys(evaluation["policies"], {"scoring", "validity"})
    keys(evaluation["fields"], {"scores", "validity"})
    if any(type(v) is not bool for v in evaluation["fields"].values()) or not any(evaluation["fields"].values()):
        raise VerifierError("evaluation_fields")
    # Deliberately explicit. A future admission dialect must receive a version;
    # arbitrary challenge admission JSON is never silently ignored.
    if evaluation.get("admission") is not None:
        raise VerifierError("unsupported_admission_policy")
    instructions = {}
    for field in ("scoring", "validity"):
        selected = canonical(evaluation["policies"][field])
        match = [p for p in config["supported_policies"][field] if canonical(p["selector"]) == selected]
        if len(match) != 1:
            raise VerifierError("unsupported_selected_policy")
        instructions[field] = match[0]["instructions"]
    return loads(canonical(evaluation)), instructions


def load_config(root, config_path, submission_path, evaluation=None):
    root = Path(root)
    config_path, submission_path = relative(config_path), relative(submission_path)
    outside(root / config_path, root / submission_path)
    config = normalize(loads(read_bytes(root / config_path, 262144)))
    outside(root / config["rules_path"], root / submission_path)
    rules = utf8(read_bytes(root / config["rules_path"], 131072))
    text(rules, 131072)
    evaluation, instructions = select_evaluation(config, evaluation)
    paths = {path for checker in config.get("checkers", []) for path in checker["files"]}
    checker_files = snapshot_declared(root, paths, root / submission_path, config["limits"])
    return {"config": config, "rules": rules, "evaluation": evaluation, "policy_instructions": instructions,
            "checker_files": checker_files}


def verify_effective(effective):
    keys(effective, {"config", "rules", "evaluation", "policy_instructions", "checker_files"})
    if canonical(normalize(effective["config"])) != canonical(effective["config"]):
        raise VerifierError("noncanonical_config")
    text(effective["rules"], 131072)
    _, instructions = select_evaluation(effective["config"], effective["evaluation"])
    if canonical(instructions) != canonical(effective["policy_instructions"]):
        raise VerifierError("policy_binding")
    paths = {path for checker in effective["config"].get("checkers", []) for path in checker["files"]}
    validate_files(effective["checker_files"], effective["config"]["limits"], empty=True)
    if set(effective["checker_files"]) != paths:
        raise VerifierError("checker_bundle_binding")
