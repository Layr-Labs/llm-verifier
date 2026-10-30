"""Independent credential-separated phases. No execute-and-review command."""

import argparse
import os
from pathlib import Path
import sys

from .artifacts import intake_snapshot, preflight_snapshot, verify_intake
from .config import load_config, resolve_submission
from .errors import VerifierError
from .files import distinct_paths, clear_output, ensure_directory, outside, read_bytes, read_json, relative, write_json
from .jsonutil import loads
from .pipeline import execute, finalize, review, export_results
from .workflow import github_output, prepare, prepare_runtime


def parser():
    p = argparse.ArgumentParser(prog="llm-verifier", description="Snapshot, execute, review and finalize in separate trust domains.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("judges", help="List bundled judge personas and committee profiles")
    cmd = sub.add_parser("inspect-results", help="Display result selectors from a Yukon discovery response; never authorizes intake")
    cmd.add_argument("--discovery", required=True, help="GET /api/submissions/:id/accepted-results response file, or - for stdin")
    cmd = sub.add_parser("init", help="Create editable challenge source files")
    cmd.add_argument("output")
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--model", required=True, help="Explicit native OpenAI model ID; no default")
    for command, help_text in (("check", "Validate challenge source without execution"),
                               ("test-checkers", "Run known-answer cases with the production Docker runner"),
                               ("github", "Generate or refresh the Yukon manifest and pinned Actions caller")):
        cmd = sub.add_parser(command, help=help_text)
        cmd.add_argument("--root", required=True)
        if command == "github":
            cmd.add_argument("--verifier-repository", default="Layr-Labs/llm-verifier")
            cmd.add_argument("--verifier-revision", required=True)
    def intake_options(cmd):
        cmd.add_argument("--root", required=True, help="Protected challenge root")
        cmd.add_argument("--config", default="verifier.json", help="Protected root-relative JSON configuration")
        cmd.add_argument("--submission", default="submission", help="Candidate subtree relative to root")
        cmd.add_argument("--evaluation", default=os.environ.get("YUKON_EVALUATION"), help="Yukon evaluation JSON; defaults to YUKON_EVALUATION")
        cmd.add_argument("--accepted-results-context", default=os.environ.get("YUKON_ACCEPTED_RESULTS"), help="Trusted Yukon workflow context; automatic in hosted evaluation")
        cmd.add_argument("--out", required=True, help="Organizer-owned output, outside candidate paths")
    intake_options(sub.add_parser("intake", help="Create a bounded immutable submission snapshot"))
    cmd = sub.add_parser("preflight", help="Check a selected package and provider schemas without execution, credentials or network")
    cmd.add_argument("--root", required=True)
    cmd.add_argument("--config", default="verifier.json")
    cmd.add_argument("--submission", default="submission")
    cmd.add_argument("--evaluation", default=os.environ.get("YUKON_EVALUATION"))
    sim = sub.add_parser("simulate", help="Offline scripted plumbing only; NEVER emits an official score")
    intake_options(sim)
    sim.add_argument("--script", required=True)
    def artifacts(cmd, initial=False, initial_review=False, followup=False):
        cmd.add_argument("--intake", required=True)
        cmd.add_argument("--expected-intake", default=os.environ.get("LLMV_EXPECTED_INTAKE"))
        if initial:
            cmd.add_argument("--initial", required=True)
        if initial_review:
            cmd.add_argument("--initial-review", required=True)
        if followup:
            cmd.add_argument("--followup", required=True)
    cmd = sub.add_parser("experiments", help="Credential-free initial experiments")
    artifacts(cmd)
    cmd.add_argument("--out", required=True)
    cmd = sub.add_parser("followup", help="Credential-free single requested experiment batch")
    artifacts(cmd, True, True)
    cmd.add_argument("--out", required=True)
    cmd = sub.add_parser("review-initial", help="OpenAI review using OPENAI_API_KEY; never executes candidates")
    artifacts(cmd, True)
    cmd.add_argument("--out", required=True)
    cmd.add_argument("--failure-out", help="Separate diagnostic artifact; defaults to OUT.failure.json")
    cmd.add_argument("--resume", help="Trusted same-phase escalation failure checkpoint; retains completed primary work")
    cmd = sub.add_parser("review-final", help="OpenAI final review using OPENAI_API_KEY, judgment only")
    artifacts(cmd, True, True, True)
    cmd.add_argument("--out", required=True)
    cmd.add_argument("--failure-out", help="Separate diagnostic artifact; defaults to OUT.failure.json")
    cmd.add_argument("--resume", help="Trusted same-phase escalation failure checkpoint; retains completed primary work")
    cmd = sub.add_parser("finalize", help="Validate chain and emit exactly one Yukon score plus dossier")
    artifacts(cmd, True, True)
    cmd.add_argument("--followup")
    cmd.add_argument("--final-review")
    paths = cmd.add_mutually_exclusive_group(required=True)
    paths.add_argument("--score", help="Protected final score file")
    paths.add_argument("--score-dir", help="Fresh artifact staging directory; use --score-path")
    cmd.add_argument("--score-path", help="Manifest-relative scorePath when using --score-dir")
    cmd.add_argument("--dossier", required=True)
    cmd.add_argument("--results", help="Atomic submission source; absent for invalid judgments")
    cmd.add_argument("--receipt", help="Platform receipt (default: accepted-results-receipt.json beside --results)")
    cmd = sub.add_parser("export-results", help="Revalidate a final dossier and export its original manifest and atomic verdict")
    cmd.add_argument("--dossier", required=True)
    cmd.add_argument("--out", required=True)
    cmd = sub.add_parser("prepare-runtime", help="Explicitly pull organizer pinned Docker image if this phase needs it; no execution")
    artifacts(cmd)
    cmd.add_argument("--phase", choices=("initial", "followup"), default="initial")
    cmd = sub.add_parser("workflow-prepare", help="Verify pinned checkouts, paths and workflow context before intake")
    for name in ("checkout", "verifier-checkout", "candidate-revision", "verifier-revision", "root-dir", "config", "submission", "score-path", "out", "run-id"):
        cmd.add_argument("--" + name, required=True)
    cmd.add_argument("--evaluation", default=os.environ.get("YUKON_EVALUATION"))
    cmd.add_argument("--accepted-results-context", default=os.environ.get("YUKON_ACCEPTED_RESULTS"))
    return p


def run(args):
    if args.command == "judges":
        from .judges import catalog
        from .jsonutil import canonical
        print(canonical(catalog()).decode())
        return
    if args.command == "inspect-results":
        from .prior import discovery_results
        from .jsonutil import canonical
        value = loads(sys.stdin.buffer.read(2 * 1024 * 1024 + 1), 2 * 1024 * 1024) if args.discovery == "-" else loads(read_bytes(args.discovery, 2 * 1024 * 1024), 2 * 1024 * 1024)
        print(canonical(discovery_results(value)).decode())
        return
    if args.command in {"init", "check", "test-checkers", "github"}:
        from . import scaffold
        from .jsonutil import canonical
        if args.command == "init":
            result = scaffold.initialize(args.output, args.name, args.model)
        elif args.command == "github":
            result = scaffold.github(args.root, args.verifier_repository, args.verifier_revision)
        else:
            result = scaffold.check(args.root) if args.command == "check" else scaffold.test_checkers(args.root)
        print(canonical(result).decode())
        return
    if args.command in {"intake", "simulate", "preflight"}:
        args.submission = resolve_submission(args.root, args.config, args.submission)
    if args.command == "preflight":
        from .jsonutil import canonical, digest
        effective = load_config(args.root, args.config, args.submission, loads(args.evaluation) if args.evaluation else None)
        prepared = preflight_snapshot(effective, Path(args.root) / relative(args.submission))
        print(canonical({"valid": True, "submission_files": len(prepared["files"]),
                         "submission_sha256": digest(prepared["files"]), "config_sha256": digest(effective),
                         "preprocessing": prepared.get("preprocessing")}).decode())
        return
    if args.command == "export-results":
        distinct_paths([args.dossier], [args.out])
        clear_output(args.out)
        result = export_results(read_json(args.dossier))
        write_json(args.out, result)
        return
    if args.command == "finalize":
        if args.score_dir:
            if not args.score_path:
                raise VerifierError("manifest_score_path_required")
            args.score = str(Path(args.score_dir) / relative(args.score_path))
        if args.receipt is None and args.results:
            args.receipt = str(Path(args.results).parent / "accepted-results-receipt.json")
        distinct_paths([args.intake, args.initial, args.initial_review, args.followup, args.final_review],
                       [args.score, args.dossier, args.results, args.receipt])
        # Must happen before reading ANY artifact, including intake.
        clear_output(args.score)
        clear_output(args.dossier)
        if args.results:
            clear_output(args.results)
        if args.receipt:
            clear_output(args.receipt)
    elif hasattr(args, "out"):
        if args.command in {"review-initial", "review-final"}:
            args.failure_out = args.failure_out or args.out + ".failure.json"
            distinct_paths([args.intake, args.initial, getattr(args, "initial_review", None),
                            getattr(args, "followup", None), args.resume], [args.out, args.failure_out])
            ensure_directory(Path(args.out).parent)
            ensure_directory(Path(args.failure_out).parent)
            clear_output(args.failure_out)
        if args.command in {"intake", "simulate"}:
            outside(args.out, Path(args.root) / relative(args.submission))
        clear_output(args.out)
    if args.command == "workflow-prepare":
        prepare(args)
        return
    if args.command in {"intake", "simulate"}:
        effective = load_config(args.root, args.config, args.submission, loads(args.evaluation) if args.evaluation else None)
        candidate = Path(args.root) / relative(args.submission)
        accepted_context = loads(args.accepted_results_context, 16384) if args.accepted_results_context else None
        if args.command == "intake":
            result = intake_snapshot(effective, candidate, accepted_results_context=accepted_context)
            write_json(args.out, result)
            github_output("intake_sha256", result["sha256"])
        else:
            from .simulation import simulate
            simulate(effective, candidate, read_json(args.script), args.out, accepted_results_context=accepted_context)
        return
    intake = read_json(args.intake)
    verify_intake(intake, args.expected_intake)
    if args.command == "prepare-runtime":
        prepare_runtime(intake, args.expected_intake, phase=args.phase)
        return
    initial = read_json(args.initial) if hasattr(args, "initial") else None
    first = read_json(args.initial_review) if hasattr(args, "initial_review") else None
    followup = read_json(args.followup) if getattr(args, "followup", None) else None
    if args.command == "experiments":
        result = execute(intake)
    elif args.command == "followup":
        result = execute(intake, first, initial)
    elif args.command in {"review-initial", "review-final"}:
        result = review(intake, initial, first, followup, failure_out=args.failure_out,
                        resume=read_json(args.resume) if args.resume else None, defer_checkpoint_cleanup=True)
        github_output("kind", result["data"]["decision"]["kind"])
    else:
        if args.score_dir and intake["data"]["context"].get("score_path") != args.score_path:
            raise VerifierError("manifest_score_path_binding")
        last = read_json(args.final_review) if args.final_review else None
        finalize(intake, initial, first, args.score, args.dossier, followup, last, results_path=args.results, receipt_path=args.receipt)
        return
    write_json(args.out, result)
    if args.command in {"review-initial", "review-final"} and result["data"].get("execution", {}).get("version") == "parallel-children-v1":
        entries = [result["data"][name] for name in ("primary", "escalation")]
        if not any(e is not None and not e["carried"] and e["status"] == "failed" for e in entries):
            clear_output(args.failure_out)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        run(args)
        if args.command not in {"inspect-results", "judges"}:
            print("llm-verifier: completed" + (" (SIMULATION ONLY)" if args.command == "simulate" else ""))
        return 0
    except VerifierError as exc:
        # Diagnostics contain static codes and escaped field/path names, never input values or provider text.
        print("llm-verifier: " + str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("llm-verifier: infrastructure_or_input_failure", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
