"""Source-bound region plans and coverage for the offline decomposed protocol."""

from .errors import VerifierError
from .jsonutil import canonical, digest, integer, keys, text

PROTOCOL = "decomposed-review-v1"
DEFAULT_MAX_REGIONS = 8
MAX_REGIONS = 16


def lines(record):
    # Match the line numbers shown to the planner and workers, including empty files.
    return record["text"].splitlines() or [""]


def source_index(files):
    return {path: {"sha256": record["sha256"], "line_count": len(lines(record))}
            for path, record in sorted(files.items())}


def preflight(files, maximum):
    # Each file needs at least one region; never omit a file to fit the budget.
    if len(files) > maximum:
        raise VerifierError("region_source_count_exceeds_max_regions")


def planning_context(files, maximum):
    return {"max_regions": maximum, "sources": source_index(files),
            "numbered_sources": {path: [[i, line] for i, line in enumerate(lines(record), 1)]
                                 for path, record in sorted(files.items())}}


def freeze_plan(report, files, maximum):
    from .config import identifier
    keys(report, {"regions"})
    regions = report["regions"]
    if type(regions) is not list or not 1 <= len(regions) <= maximum:
        raise VerifierError("region_plan_count")
    sources = source_index(files)
    next_line = dict.fromkeys(sources, 1)
    seen = set()
    for region in regions:
        keys(region, {"id", "path", "start_line", "end_line", "purpose"})
        identifier(region["id"])
        if region["id"] in seen:
            raise VerifierError("duplicate_region_id")
        seen.add(region["id"])
        path = region["path"]
        if type(path) is not str or path not in sources:
            raise VerifierError("region_source_path")
        integer(region["start_line"], 1, sources[path]["line_count"])
        integer(region["end_line"], region["start_line"], sources[path]["line_count"])
        if region["start_line"] != next_line[path]:
            raise VerifierError("region_plan_gap_or_overlap")
        next_line[path] = region["end_line"] + 1
        text(region["purpose"], 2000)
    if any(next_line[path] != source["line_count"] + 1 for path, source in sources.items()):
        raise VerifierError("region_plan_incomplete")
    return {"regions": regions, "sources": sources}


def reuse_plan(plan, files, maximum):
    keys(plan, {"regions", "sources"})
    frozen = freeze_plan({"regions": plan["regions"]}, files, maximum)
    if canonical(plan) != canonical(frozen):
        raise VerifierError("region_plan_source_binding")
    return frozen


def assignment(region, plan, files):
    start, end = region["start_line"], region["end_line"]
    return {"region": region, "plan_sha256": digest(plan),
            "source_sha256": files[region["path"]]["sha256"],
            "numbered_lines": [[i, line] for i, line in enumerate(lines(files[region["path"]])[start - 1:end], start)]}


def coverage(report, region, member, files):
    keys(report, {"region_id", "start_line", "end_line", "complete"})
    if report["region_id"] != region["id"]:
        raise VerifierError("region_coverage_identity")
    for field in ("start_line", "end_line"):
        integer(report[field], region["start_line"], region["end_line"])
    if (report["complete"] is not True or report["start_line"] != region["start_line"]
            or report["end_line"] != region["end_line"]):
        raise VerifierError("region_review_incomplete:" + region["id"])
    return {**report, "member_id": member["id"], "path": region["path"],
            "source_sha256": files[region["path"]]["sha256"]}
