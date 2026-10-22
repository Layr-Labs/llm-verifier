"""Protected, bounded persona catalogs and committee configuration resolution."""

from pathlib import Path
import re

from .errors import VerifierError
from .files import outside, read_bytes, relative, utf8, validate_files
from .jsonutil import canonical, digest, keys, loads, sha, text

DIRECTORY = Path(__file__).parent / "judges"
PROTOCOLS = ("single-v1", "review-defense-adjudication-v1")
STAGES = ("review", "defense", "adjudication")
SOURCE_LIMITS = {"max_files": 32, "max_file_bytes": 65536, "max_submission_bytes": 262144}
MEMBER_SETTINGS = {"model", "reasoning_effort", "max_completion_tokens", "timeout_seconds",
                   "max_attempts", "max_response_bytes", "response_format", "background"}
NAME = re.compile(r"(builtin|local):[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def reference(value, namespace=None):
    if (type(value) is not str or not NAME.fullmatch(value)
            or namespace is not None and not value.startswith(namespace + ":")):
        raise VerifierError("judge_reference")


def _member(value, *, override=False):
    from .config import identifier
    keys(value, set() if override else {"id", "persona", "stage"},
         {"persona", "provider"} if override else {"provider"})
    if not override:
        identifier(value["id"])
        if value["stage"] not in STAGES:
            raise VerifierError("judge_member_stage")
    if "persona" in value:
        reference(value["persona"])
    if "provider" in value:
        keys(value["provider"], set(), MEMBER_SETTINGS)


def _members(value):
    if type(value) is not list or not 1 <= len(value) <= 8:
        raise VerifierError("judge_member_count")
    for member in value:
        _member(member)
    if len({m["id"] for m in value}) != len(value):
        raise VerifierError("duplicate_judge_member")


def selection(value, *, file_allowed=True):
    """Validate the selector independently of filesystem resolution."""
    if type(value) is dict and "path" in value and file_allowed:
        keys(value, {"path"})
        relative(value["path"])
        return
    if type(value) is dict and "profile" in value:
        keys(value, {"profile"}, {"catalogs", "add_members", "overrides"})
        reference(value["profile"])
    else:
        keys(value, {"protocol", "members"}, {"catalogs"})
        if value["protocol"] not in PROTOCOLS:
            raise VerifierError("judge_protocol")
        _members(value["members"])
    catalogs = value.get("catalogs", [])
    if type(catalogs) is not list or len(catalogs) > 8:
        raise VerifierError("judge_catalog_count")
    for path in catalogs:
        relative(path)
    if len(set(catalogs)) != len(catalogs):
        raise VerifierError("duplicate_judge_catalog")
    if "add_members" in value:
        _members(value["add_members"])
        if any(m["stage"] != "review" for m in value["add_members"]):
            raise VerifierError("only_reviewers_may_be_added")
    overrides = value.get("overrides", {})
    if type(overrides) is not dict or len(overrides) > 8:
        raise VerifierError("judge_overrides")
    from .config import identifier
    for name, override in overrides.items():
        identifier(name)
        _member(override, override=True)


def _catalog(value, namespace, read):
    keys(value, {"version", "personas", "profiles"})
    if type(value["version"]) is not int or value["version"] != 1:
        raise VerifierError("judge_catalog_version")
    personas, profiles = {}, {}
    for field in ("personas", "profiles"):
        if type(value[field]) is not list or len(value[field]) > 32:
            raise VerifierError("judge_catalog_entries")
    for persona in value["personas"]:
        keys(persona, {"id"}, {"instructions", "instructions_path"})
        reference(persona["id"], namespace)
        if persona["id"] in personas:
            raise VerifierError("duplicate_judge_persona")
        if ("instructions" in persona) == ("instructions_path" in persona):
            raise VerifierError("judge_persona_instructions")
        instructions = persona.get("instructions")
        if "instructions_path" in persona:
            relative(persona["instructions_path"])
            instructions = read(persona["instructions_path"])
        text(instructions, 16000)
        personas[persona["id"]] = instructions
    for profile in value["profiles"]:
        keys(profile, {"id", "protocol", "members"})
        reference(profile["id"], namespace)
        if profile["id"] in profiles:
            raise VerifierError("duplicate_judge_profile")
        selection({k: v for k, v in profile.items() if k != "id"}, file_allowed=False)
        profiles[profile["id"]] = profile
    return personas, profiles


def _resolve(raw, read, provider, limits):
    from .config import normalize_provider
    selection(raw)
    selected = loads(read(raw["path"])) if "path" in raw else raw
    selection(selected, file_allowed=False)
    builtin_read = lambda path: utf8(read_bytes(DIRECTORY / relative(path), SOURCE_LIMITS["max_file_bytes"]))
    personas, profiles = _catalog(loads(builtin_read("catalog.json")), "builtin", builtin_read)
    for path in selected.get("catalogs", []):
        more_personas, more_profiles = _catalog(loads(read(path)), "local", read)
        if personas.keys() & more_personas.keys() or profiles.keys() & more_profiles.keys():
            raise VerifierError("duplicate_judge_definition")
        personas.update(more_personas)
        profiles.update(more_profiles)
    if "profile" in selected:
        if selected["profile"] not in profiles:
            raise VerifierError("unknown_judge_profile")
        profile = loads(canonical(profiles[selected["profile"]]))
        profile["members"].extend(selected.get("add_members", []))
        _members(profile["members"])
        registry = {member["id"]: member for member in profile["members"]}
        for name, override in selected.get("overrides", {}).items():
            if name not in registry:
                raise VerifierError("unknown_judge_override")
            member = registry[name]
            if "persona" in override:
                member["persona"] = override["persona"]
            member["provider"] = {**member.get("provider", {}), **override.get("provider", {})}
        profile_id = selected["profile"]
    else:
        profile, profile_id = selected, None
    members = []
    for member in profile["members"]:
        if member["persona"] not in personas:
            raise VerifierError("unknown_judge_persona")
        instructions = personas[member["persona"]]
        settings = normalize_provider({**provider, **member.get("provider", {})})
        members.append({**member, "instructions": instructions,
                        "persona_sha256": digest({"id": member["persona"], "instructions": instructions}),
                        "provider": settings})
    stages = [m["stage"] for m in members]
    if profile["protocol"] == "single-v1":
        if stages != ["review"]:
            raise VerifierError("single_judge_members")
    elif (not 1 <= stages.count("review") <= 6 or stages.count("defense") != 1
          or stages.count("adjudication") != 1):
        raise VerifierError("committee_required_stages")
    # Validate minimum initial-stage capacity before making any paid request.
    mandatory = [m for m in members if m["stage"] != "defense"]
    if (len(mandatory) > limits["total_provider_requests"]
            or sum(m["provider"]["max_completion_tokens"] for m in mandatory) > limits["total_review_tokens"]):
        raise VerifierError("committee_minimum_budget")
    return {"version": 1, "profile": profile_id, "protocol": profile["protocol"], "members": members}


def load_judging(root, raw, candidate, provider, limits):
    files = {}
    def read(path):
        relative(path)
        if path not in files:
            if len(files) >= SOURCE_LIMITS["max_files"]:
                raise VerifierError("judge_file_count")
            outside(Path(root) / path, candidate)
            remaining = SOURCE_LIMITS["max_submission_bytes"] - sum(r["size"] for r in files.values())
            data = read_bytes(Path(root) / path, min(SOURCE_LIMITS["max_file_bytes"], remaining))
            files[path] = {"text": utf8(data), "sha256": sha(data), "size": len(data)}
        return files[path]["text"]
    committee = _resolve(raw, read, provider, limits)
    return {"committee": committee, "files": files}


def verify_judging(value, raw, provider, limits):
    keys(value, {"committee", "files"})
    validate_files(value["files"], SOURCE_LIMITS, empty=True)
    used = set()
    def read(path):
        relative(path)
        if path not in value["files"]:
            raise VerifierError("missing_judge_source")
        used.add(path)
        return value["files"][path]["text"]
    expected = _resolve(raw, read, provider, limits)
    if canonical(expected) != canonical(value["committee"]) or used != value["files"].keys():
        raise VerifierError("judge_configuration_binding")


def catalog():
    """Public packaged definitions for authoring and discovery."""
    return loads(read_bytes(DIRECTORY / "catalog.json", SOURCE_LIMITS["max_file_bytes"]))
