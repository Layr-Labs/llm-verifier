"""Small trusted workflow helpers; candidates never select executable behavior."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .artifacts import intake_snapshot, verify_intake
from .config import load_config, resolve_submission
from .errors import InfrastructureError, VerifierError
from .files import directory_fd, outside, relative, write_json
from .jsonutil import loads, sha
from .submission import initial_plan


def checkout_revision(directory, expected):
    if type(expected) is not str or not re.fullmatch("[0-9a-f]{40}", expected):
        raise VerifierError("full_git_revision_required")
    try:
        actual = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                                check=True, capture_output=True, timeout=10).stdout.decode().strip()
        dirty = subprocess.run(["git", "-C", str(directory), "status", "--porcelain", "--untracked-files=no"],
                               check=True, capture_output=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        raise InfrastructureError("checkout_verification_failed") from None
    if actual != expected or dirty:
        raise VerifierError("checkout_revision_mismatch")


def prepare(args):
    checkout_revision(args.checkout, args.candidate_revision)
    checkout_revision(args.verifier_checkout, args.verifier_revision)
    root_dir = relative(args.root_dir, dot=True)
    root = Path(args.checkout) / root_dir
    with directory_fd(root):
        pass
    submission = resolve_submission(root, args.config, args.submission)
    score_path = relative(args.score_path)
    outside(root / score_path, root / submission)
    # The score must not overwrite protected config or rules either.
    effective = load_config(root, args.config, submission, loads(args.evaluation) if args.evaluation else None)
    if score_path == effective["config"].get("submission_selector", {}).get("path"):
        raise VerifierError("score_path_overwrites_config")
    if score_path in {args.config, effective["config"]["rules_path"], *effective["config"].get("rules_files", []), *effective["checker_files"]}:
        raise VerifierError("score_path_overwrites_config")
    outside(args.out, root / submission)
    context = {"kind": "github-actions", "candidate_revision": args.candidate_revision,
               "verifier_revision": args.verifier_revision, "root_dir": root_dir,
               "submission_path": submission, "score_path": score_path, "run_id": args.run_id,
               "workflow_content_sha256": sha((Path(args.verifier_checkout) / ".github/workflows/verify.yml").read_bytes())}
    if not re.fullmatch(r"[0-9]+:[0-9]+", args.run_id):
        raise VerifierError("workflow_run_identity")
    intake = intake_snapshot(effective, root / submission, context,
                             accepted_results_context=loads(args.accepted_results_context, 16384) if args.accepted_results_context else None)
    write_json(args.out, intake)
    github_output("intake_sha256", intake["sha256"])
    return intake


def github_output(key, value):
    if "GITHUB_OUTPUT" in os.environ:
        # Only internal key names and a digest or enum are written, no candidate
        # text and no arbitrary input interpolation into workflow commands.
        if key not in {"intake_sha256", "kind"} or not re.fullmatch(r"[a-z0-9_]{1,64}", value):
            raise VerifierError("workflow_output_format")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(key + "=" + value + "\n")


def prepare_runtime(intake, expected=None, *, phase="initial"):
    data = verify_intake(intake, expected)
    if phase not in {"initial", "followup"}:
        raise VerifierError("runtime_phase")
    # Follow-up runs on a fresh worker; its conditional job prepares the image
    # for any declared/registered entrypoint the judge may have requested.
    needed = initial_plan(data["manifest"]) if phase == "initial" else (
        data["manifest"]["experiments"] or data["effective"]["config"].get("checkers"))
    if not needed:
        return
    binary = shutil.which("docker")
    if not binary:
        raise InfrastructureError("docker_unavailable")
    with tempfile.TemporaryDirectory(prefix="llmv-pull-") as config:
        env = {"PATH": os.path.dirname(binary) + ":/usr/bin:/bin", "DOCKER_CONFIG": config,
               "DOCKER_HOST": "unix:///var/run/docker.sock"}
        try:
            result = subprocess.run([binary, "pull", "--platform=linux/amd64", data["effective"]["config"]["runtime"]["image"]],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=180, check=False)
        except (OSError, subprocess.SubprocessError):
            raise InfrastructureError("runtime_setup_failed") from None
        if result.returncode:
            raise InfrastructureError("runtime_setup_failed")
