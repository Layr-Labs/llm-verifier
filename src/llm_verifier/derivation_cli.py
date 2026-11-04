"""Explicit CLI and hosted intake for score derivation; no validity fallback."""

import os
from pathlib import Path
import re

from . import derivation
from .derivation_transport import fetch_entry
from .errors import VerifierError
from .files import clear_output, distinct_paths, outside, read_json, relative, snapshot, write_json
from .jsonutil import canonical, digest, loads, sha
from .workflow import checkout_revision, github_output

COMMANDS = {"derivation-prepare", "derivation-fetch", "derivation-workflow-prepare",
            "derive-score", "derivation-check", "simulate-derivation"}


def add_parsers(sub):
    for name in ("derivation-prepare", "derivation-fetch"):
        cmd = sub.add_parser(name, help="Protected retained-result input; no scientific review or candidate execution")
        cmd.add_argument("--root", required=True)
        cmd.add_argument("--config", default="derivation.json")
        cmd.add_argument("--submission", default="submission")
        cmd.add_argument("--evaluation", default=os.environ.get("YUKON_EVALUATION"))
        cmd.add_argument("--archives", required=True)
        if name == "derivation-prepare":
            cmd.add_argument("--context", help="Trusted execution metadata JSON; omitted means local/unasserted")
            cmd.add_argument("--out", required=True)
    cmd = sub.add_parser("derivation-workflow-prepare", help="Verify current Git pins, fetch protected old artifacts and prepare derivation intake")
    for name in ("checkout", "verifier-checkout", "candidate-revision", "verifier-revision", "root-dir",
                 "config", "submission", "score-path", "out", "run-id", "archives", "repository"):
        cmd.add_argument("--" + name, required=True)
    cmd.add_argument("--evaluation", default=os.environ.get("YUKON_EVALUATION"))
    cmd.add_argument("--accepted-results-context", default=os.environ.get("YUKON_ACCEPTED_RESULTS"))
    cmd.add_argument("--receipt-out")
    for name in ("derive-score", "simulate-derivation"):
        cmd = sub.add_parser(name, help="Review representation only using retained claims; never changes validity")
        cmd.add_argument("--intake", required=True)
        cmd.add_argument("--expected-intake", default=os.environ.get("LLMV_EXPECTED_INTAKE"))
        cmd.add_argument("--out", required=True)
        if name == "derive-score":
            cmd.add_argument("--failure-out")
        else:
            cmd.add_argument("--script", required=True, help="A scripted derivation decision; output is always simulation")
    cmd = sub.add_parser("derivation-check", help="Validate a derivation artifact, its original source and new provenance")
    cmd.add_argument("--derivation", required=True)
    cmd.add_argument("--expected-intake", default=os.environ.get("LLMV_EXPECTED_INTAKE"))
    cmd.add_argument("--require-publishable", action="store_true")


def _evaluation(value):
    if not value:
        raise VerifierError("derivation_retained_context_required")
    parsed = loads(value, 65536)
    derivation.retained_result.evaluation(parsed)
    return parsed


def _fetch(root, config, submission, evaluation, archives):
    conf, _, _, entry = derivation.load_plan(root, config, submission, evaluation)
    files = snapshot(Path(root) / relative(submission), conf["limits"])
    if digest(files) != entry["source_files_sha256"]:
        raise VerifierError("derivation_changed_candidate")
    outside(archives, Path(root) / relative(submission))
    fetch_entry(entry, archives)


def workflow_prepare(args):
    checkout_revision(args.checkout, args.candidate_revision)
    checkout_revision(args.verifier_checkout, args.verifier_revision)
    if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", args.run_id):
        raise VerifierError("workflow_run_identity")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise VerifierError("derivation_artifact_repository")
    root_dir = relative(args.root_dir, dot=True)
    root = Path(args.checkout) / root_dir
    submission = relative(args.submission)
    score_path = relative(args.score_path)
    outside(root / score_path, root / submission)
    outside(args.out, root / submission)
    evaluation = _evaluation(args.evaluation)
    config, _, _, _ = derivation.load_plan(root, args.config, submission, evaluation)
    if score_path in {args.config, config["plan_path"]}:
        raise VerifierError("score_path_overwrites_config")
    _fetch(root, args.config, submission, evaluation, args.archives)
    context = {"kind": "github-actions", "repository": args.repository,
               "candidate_revision": args.candidate_revision, "verifier_revision": args.verifier_revision,
               "root_dir": root_dir, "submission_path": submission, "score_path": score_path,
               "run_id": args.run_id,
               "workflow_content_sha256": sha((Path(args.verifier_checkout) / ".github/workflows/derive-score.yml").read_bytes())}
    accepted_context = loads(args.accepted_results_context, 16384) if args.accepted_results_context else None
    if accepted_context is not None and not args.receipt_out:
        raise VerifierError("derivation_receipt_output_required")
    if args.receipt_out:
        outside(args.receipt_out, root / submission)
        distinct_paths([root / args.config, root / config["plan_path"]], [args.out, args.receipt_out])
        clear_output(args.receipt_out)
    intake = derivation.prepare(root, args.config, submission, evaluation, context, args.archives,
                                accepted_results_context=accepted_context)
    if args.receipt_out and accepted_context is not None:
        write_json(args.receipt_out, derivation.receipt(intake))
    write_json(args.out, intake)
    github_output("intake_sha256", intake["sha256"])
    return intake


def run(args):
    if args.command == "derivation-check":
        result = read_json(args.derivation)
        data = derivation.verify_derivation(result, args.expected_intake)
        if args.require_publishable and (result["simulation"] or data["intake"]["data"]["context"].get("kind") != "github-actions"):
            raise VerifierError("derivation_unpublishable")
        print(canonical({"mode": derivation.MODE, "status": data["decision"]["status"],
                         "sha256": result["sha256"], "simulation": result["simulation"]}).decode())
        return
    if args.command == "derivation-fetch":
        _fetch(args.root, args.config, args.submission, _evaluation(args.evaluation), args.archives)
        return
    if args.command in {"derivation-prepare", "derivation-workflow-prepare"}:
        root = Path(args.root) if hasattr(args, "root") else Path(args.checkout) / relative(args.root_dir, dot=True)
        outside(args.out, root / relative(args.submission))
        config, _, _, _ = derivation.load_plan(root, args.config, args.submission, _evaluation(args.evaluation))
        distinct_paths([root / args.config, root / config["plan_path"]], [args.out])
        clear_output(args.out)
        if args.command == "derivation-workflow-prepare":
            workflow_prepare(args)
        else:
            context = loads(args.context, 65536) if args.context else {"kind": "local", "revision": "unasserted; use content hash"}
            result = derivation.prepare(args.root, args.config, args.submission, _evaluation(args.evaluation), context, args.archives)
            write_json(args.out, result)
            github_output("intake_sha256", result["sha256"])
        return
    failure = getattr(args, "failure_out", None) or (args.out + ".failure.json" if args.command == "derive-score" else None)
    distinct_paths([args.intake, getattr(args, "script", None)], [args.out, failure])
    clear_output(args.out)
    if failure:
        clear_output(failure)
    intake = read_json(args.intake)
    derivation.verify_intake(intake, args.expected_intake)
    if args.command == "simulate-derivation":
        from .simulation import ScriptedProvider
        provider = ScriptedProvider(intake["data"]["config"]["provider"], read_json(args.script))
        result = derivation.review(intake, provider=provider)
        write_json(args.out, {"simulation_only": True, "publishable": False, "derivation": result})
    else:
        result = derivation.review(intake, failure_out=failure)
        write_json(args.out, result)
