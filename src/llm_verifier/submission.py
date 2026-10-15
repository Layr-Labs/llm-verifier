"""Inert submission declarations and budgeted immutable entrypoint requests."""

from .config import identifier
from .errors import VerifierError
from .files import relative, utf8
from .input_schema import validate_schema, validate_value
from .jsonutil import canonical, integer, keys, loads, sha, text


def validate_files(files, limits):
    if type(files) is not dict or not 1 <= len(files) <= limits["max_files"]:
        raise VerifierError("file_count_limit")
    total = 0
    for path, record in files.items():
        relative(path)
        keys(record, {"text", "sha256", "size"})
        if type(record["text"]) is not str:
            raise VerifierError("source_text_required")
        data = record["text"].encode("utf-8")
        integer(record["size"], 0, limits["max_file_bytes"])
        if sha(data) != record["sha256"] or len(data) != record["size"]:
            raise VerifierError("source_hash_mismatch")
        utf8(data, python=path.endswith(".py"))
        total += len(data)
    if total > limits["max_submission_bytes"]:
        raise VerifierError("submission_byte_limit")


def manifest_from(files, limits):
    validate_files(files, limits)
    if "submission.json" not in files:
        raise VerifierError("missing_submission_manifest")
    manifest = loads(files["submission.json"]["text"])
    keys(manifest, {"version", "title", "claims", "experiments"})
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise VerifierError("unsupported_submission_version")
    text(manifest["title"], 300)
    claims = manifest["claims"]
    if type(claims) is not list or not 1 <= len(claims) <= 32:
        raise VerifierError("claim_count")
    for claim in claims:
        text(claim, 8000)
    entries = manifest["experiments"]
    if type(entries) is not list or len(entries) > 16:
        raise VerifierError("experiment_count")
    seen = set()
    for entry in entries:
        keys(entry, {"id", "source", "purpose", "input_schema", "initial_inputs"})
        identifier(entry["id"])
        if entry["id"] in seen:
            raise VerifierError("duplicate_experiment_id")
        seen.add(entry["id"])
        path = relative(entry["source"])
        if not path.endswith(".py") or path not in files:
            raise VerifierError("missing_python_entrypoint")
        text(entry["purpose"], 4000)
        validate_schema(entry["input_schema"])
        if entry["input_schema"]["type"] != "object":
            raise VerifierError("input_root_object_required")
        if type(entry["initial_inputs"]) is not list or len(entry["initial_inputs"]) > limits["max_initial_runs"]:
            raise VerifierError("initial_run_limit")
        for value in entry["initial_inputs"]:
            validate_value(entry["input_schema"], value)
    plan = initial_plan(manifest)
    check_budget(plan, limits, "initial")
    return manifest


def initial_plan(manifest):
    return [{"experiment": e["id"], "inputs": value, "reason": "submission-declared initial experiment"}
            for e in manifest["experiments"] for value in e["initial_inputs"]]


def check_budget(plan, limits, round_name, previous_runs=0):
    maximum = limits["max_initial_runs" if round_name == "initial" else "max_followups"]
    if len(plan) > maximum:
        raise VerifierError("run_count_budget")
    if len(plan) * limits["run_seconds"] > limits[round_name + "_seconds"] or (len(plan) + previous_runs) * limits["run_seconds"] > limits["total_seconds"]:
        raise VerifierError("allotted_time_budget")
    if (len(plan) + previous_runs) * (limits["stdout_bytes"] + limits["stderr_bytes"]) > limits["total_output_bytes"]:
        raise VerifierError("allotted_output_budget")


def followup_plan(requests, manifest, limits, previous_runs):
    if type(requests) is not list or not requests:
        raise VerifierError("nonempty_followup_batch_required")
    check_budget(requests, limits, "followup", previous_runs)
    entries = {e["id"]: e for e in manifest["experiments"]}
    for request in requests:
        keys(request, {"experiment", "inputs", "reason"})
        if type(request["experiment"]) is not str or request["experiment"] not in entries:
            raise VerifierError("undeclared_entrypoint")
        text(request["reason"], 4000)
        validate_value(entries[request["experiment"]]["input_schema"], request["inputs"])
    return loads(canonical(requests))
