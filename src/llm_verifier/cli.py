"""Independent credential-separated phases. No execute-and-review command."""

import argparse
import os
from pathlib import Path
import sys

from .artifacts import intake_snapshot, verify_intake
from .config import load_config
from .errors import VerifierError
from .files import clear_output, outside, read_json, relative, write_json
from .jsonutil import loads
from .pipeline import execute, finalize, review
from .workflow import github_output, prepare, prepare_runtime


def parser():
    p = argparse.ArgumentParser(prog="llm-verifier", description="Snapshot, execute, review and finalize in separate trust domains.")
    sub = p.add_subparsers(dest="command", required=True)
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
        cmd.add_argument("--out", required=True, help="Organizer-owned output, outside candidate paths")
    intake_options(sub.add_parser("intake", help="Create a bounded immutable submission snapshot"))
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
    cmd = sub.add_parser("review-final", help="OpenAI final review using OPENAI_API_KEY, judgment only")
    artifacts(cmd, True, True, True)
    cmd.add_argument("--out", required=True)
    cmd = sub.add_parser("finalize", help="Validate chain and emit exactly one Yukon score plus dossier")
    artifacts(cmd, True, True)
    cmd.add_argument("--followup")
    cmd.add_argument("--final-review")
    paths = cmd.add_mutually_exclusive_group(required=True)
    paths.add_argument("--score", help="Protected final score file")
    paths.add_argument("--score-dir", help="Fresh artifact staging directory; use --score-path")
    cmd.add_argument("--score-path", help="Manifest-relative scorePath when using --score-dir")
    cmd.add_argument("--dossier", required=True)
    cmd = sub.add_parser("prepare-runtime", help="Explicitly pull organizer pinned Docker image if this phase needs it; no execution")
    artifacts(cmd)
    cmd.add_argument("--phase", choices=("initial", "followup"), default="initial")
    cmd = sub.add_parser("workflow-prepare", help="Verify pinned checkouts, paths and workflow context before intake")
    for name in ("checkout", "verifier-checkout", "candidate-revision", "verifier-revision", "root-dir", "config", "submission", "score-path", "out", "run-id"):
        cmd.add_argument("--" + name, required=True)
    cmd.add_argument("--evaluation", default=os.environ.get("YUKON_EVALUATION"))
    return p


def run(args):
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
    if args.command == "finalize":
        if args.score_dir:
            if not args.score_path:
                raise VerifierError("manifest_score_path_required")
            args.score = str(Path(args.score_dir) / relative(args.score_path))
        # Must happen before reading ANY artifact, including intake.
        clear_output(args.score)
        clear_output(args.dossier)
    elif hasattr(args, "out"):
        if args.command in {"intake", "simulate"}:
            outside(args.out, Path(args.root) / relative(args.submission))
        clear_output(args.out)
    if args.command == "workflow-prepare":
        prepare(args)
        return
    if args.command in {"intake", "simulate"}:
        effective = load_config(args.root, args.config, args.submission, loads(args.evaluation) if args.evaluation else None)
        candidate = Path(args.root) / relative(args.submission)
        if args.command == "intake":
            result = intake_snapshot(effective, candidate)
            write_json(args.out, result)
            github_output("intake_sha256", result["sha256"])
        else:
            from .simulation import simulate
            simulate(effective, candidate, read_json(args.script), args.out)
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
        result = review(intake, initial, first, followup)
        github_output("kind", result["data"]["decision"]["kind"])
    else:
        if args.score_dir and intake["data"]["context"].get("score_path") != args.score_path:
            raise VerifierError("manifest_score_path_binding")
        last = read_json(args.final_review) if args.final_review else None
        finalize(intake, initial, first, args.score, args.dossier, followup, last)
        return
    write_json(args.out, result)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        run(args)
        print("llm-verifier: completed" + (" (SIMULATION ONLY)" if args.command == "simulate" else ""))
        return 0
    except VerifierError as exc:
        # VerifierError messages are static categories, never raw provider/file text.
        print("llm-verifier: " + str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("llm-verifier: infrastructure_or_input_failure", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
