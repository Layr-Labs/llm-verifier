"""Generated authoring guidance and credential-free public checker tests."""

BUILD_SKILL = '''---
name: build-challenge
description: Build a generic llm-verifier challenge from an approved definition and controller receipt.
---

# Build a generic verifier challenge

Process adapted from Layr-Labs/yukon-template at
72a6b96b4b5385f199a5a29ecdd742aeb45ad55f:
.agents/skills/build-challenge/SKILL.md, AGENTS.md and docs/authoring.md.
This is the llm-verifier recipe, not the stock Moonshot math-review recipe.

Required input: approved external definition, external controller receipt,
builder authority and finish point. Start with:

    llm-verifier context --root . --task build --definition /controller/definition.json --receipt /controller/receipt.json

Read the returned read files and inspect files, and turn the edit map into a
short implementation checklist. Managed builders edit only those files; rules,
configuration, runtime pins, workflows, this guidance, scaffold inventory and
controller receipt remain protected. Never run init or finalize the inventory
as a managed builder. Source code and public examples do not confer authority.

Implement the approved definition, including checker bundles, baseline example,
and independently labeled public cases. A checker pass establishes only its
encoding/arithmetic predicate, never proof certification. Accepting an auxiliary
lemma cannot authorize the main-claim score. Keep exact rational records and the
terminal endpoint distinct from numeric Yukon projections.

Run the permitted offline check command and return its observed file hashes and
results. The controller reviews edits, synchronizes its definition, and generates
a fresh finalized scaffold/receipt. Missing scientific rules stay explicit
blockers; examples do not decide them. Public conformance is not qualification.

Standalone author/controllers may initialize from their approved definition and
finalize a new inventory by regeneration. Commit, publication, live review and
paid calls require task authorization. Never execute candidates in a process or
job containing model credentials. Keep private evidence and receipts outside the
export. No frontend is included in this harness.
'''

AUTHORING = '''# Generic llm-verifier challenge

This starter adapts the approved-definition/edit-map process from
Layr-Labs/yukon-template@72a6b96b4b5385f199a5a29ecdd742aeb45ad55f.
It uses the generic llm-verifier phase protocol. Stock AutoYukon init, Moonshot,
accept/revision/reject and its sealed runtime are not being invoked.

The controller keeps the approved definition and generated receipt outside this
export. Use `llm-verifier context --root . --task build --definition PATH
--receipt PATH`, then edit only the returned paths. `llm-verifier check --root .
--definition PATH --receipt PATH` validates protected bytes, the bounded config,
candidate snapshots and scripted public cases. Managed context/check reject all
unlisted files/directories and symlinks, skipping only root .git metadata without
traversal. Keep receipts, environments, caches and evaluation output external
when validating an authoring export. It executes no Python candidate
or checker and makes no model calls. Scripts only test orchestration.

`python3 tools/public_conformance.py --root .` separately executes declared
public checker cases with Docker and no model credentials. Prepare the image
explicitly first with `docker pull IMAGE_FROM_VERIFIER_CONFIG`. Execution never
pulls implicitly. The public-conformance workflow does that setup and needs no
cross-repository access or model secrets. These known-answer tests are not an
independent mathematical assessment and do not emit official scores.

Standalone controllers synchronize approved edits back into their external
definition, then initialize a fresh directory/receipt to finalize the inventory.
Managed builders do not change rules, workflows, configuration, instruction
files, scaffold inventory or the external receipt. Baseline/example content and
independently labeled public cases remain visible and are not private tests.

## Live integration

Install llm-verifier at the reviewed full commit pinned in yukon-scaffold.json.
Use separate intake, prepare-runtime, experiments, review-initial, optional
followup/review-final, and finalize commands from the verifier README. Only the
review subprocess/jobs receive OPENROUTER_API_KEY. There is no combined local
execute-with-secrets command. `yukon run` does not orchestrate these phases.

The benchmark workflow uses a pinned reusable workflow and a matching source
revision. Before hosted use, the verifier repository must permit reusable
workflow access for this caller. A private/internal verifier also needs read-only
checkout access via VERIFIER_CHECKOUT_TOKEN when the caller token is insufficient.
Configure OPENROUTER_API_KEY only for authorized review. A generated workflow is
not evidence that access, hosted execution, import or qualification has occurred.
No settings are changed by this starter.

Finite scores use the raw coefficient in binary64 transport; exact reduced
rational values remain in checker evidence and dossier score_records. Precision
ties remain a platform limitation. An explicitly accepted terminal record is
retained as positive_infinity with projection status unsupported, and its Yukon
scores are null. It cannot enter native Yukon frontier ordering; manual review
and promotion must inspect the typed dossier. Ordinary unscored lemmas have no
terminal record. No normalization, sentinel or numeric Infinity is used.
'''

# This is a public known-answer test runner for organizer-owned checker code,
# not an alternative production verifier or mathematical certification path.
PUBLIC_RUNNER = '''"""Public protected-checker known-answer tests; no model calls or score output."""
import argparse
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import time
import uuid


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def capture(command, request, env):
    # Bounded pipe capture; never buffer arbitrary checker output on the host.
    with tempfile.TemporaryFile() as stdin:
        stdin.write(request); stdin.seek(0)
        process = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env)
        selector = selectors.DefaultSelector()
        output = {"stdout": bytearray(), "stderr": bytearray()}
        for label in output:
            selector.register(getattr(process, label), selectors.EVENT_READ, label)
        deadline = time.monotonic() + 30
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise RuntimeError("Public checker timeout")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.data].extend(chunk)
                        if len(output[key.data]) > 262144:
                            raise RuntimeError("Public checker output limit")
            if process.wait(timeout=max(0.01, deadline-time.monotonic())) != 0:
                raise RuntimeError("Public checker execution failed")
            return bytes(output["stdout"])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            selector.close()
            process.stdout.close(); process.stderr.close()


def run(root):
    if os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("Run public execution without model credentials")
    config = json.loads((root / "verifier.json").read_text())
    cases = json.loads((root / "public-checks.json").read_text())
    checks = {c["id"]: c for c in config.get("checkers", [])}
    if checks and not cases:
        raise RuntimeError("Registered checkers need public known-answer cases")
    image = config["runtime"]["image"]
    if not re.fullmatch(r"(?:docker\\.io/library/)?python:[A-Za-z0-9._-]+@sha256:[a-f0-9]{64}", image):
        raise RuntimeError("Pinned Python image required")
    for case in cases:
        check = checks[case["check"]]
        with tempfile.TemporaryDirectory(prefix="llmv-public-") as tmp:
            snapshot = Path(tmp) / "files"
            snapshot.mkdir()
            for path in check["files"]:
                parts = Path(path).parts
                if Path(path).is_absolute() or any(p in (".", "..", ".git") for p in parts):
                    raise RuntimeError("Unsafe public bundle path")
                current = root
                for part in parts:
                    current = current / part
                    if current.is_symlink():
                        raise RuntimeError("Symlink in public bundle")
                if not current.is_file() or current.stat().st_nlink != 1:
                    raise RuntimeError("Regular public bundle files required")
                target = snapshot / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(current, target)
            os.chmod(tmp, 0o755)
            name = "llmv-public-" + uuid.uuid4().hex
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp}
            command = ["docker", "run", "--name", name, "--pull=never", "--platform=linux/amd64",
                       "--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "--memory=256m", "--cpus=1", "--pids-limit=32",
                       "--tmpfs=/tmp:rw,noexec,nosuid,size=16m", "--log-driver=none",
                       "--mount", "type=bind,src=" + str(snapshot) + ",dst=/snapshot/files,readonly",
                       "-i", image, "python", "-I", "/snapshot/files/" + check["source"]]
            try:
                stdout = capture(command, canonical(case["inputs"]).encode(), env)
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("Duplicate JSON key")
                        result[key] = value
                    return result
                output = json.loads(stdout, object_pairs_hook=unique)
                if canonical(output) != canonical(case["expected"]):
                    raise RuntimeError("Public known-answer mismatch: " + case["id"])
            finally:
                subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10, env=env, check=False)
                probe = subprocess.run(["docker", "container", "ls", "--all", "--filter",
                                        "name=^/" + name + "$", "--format", "{{.Names}}"],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       timeout=10, env=env, check=False)
                if probe.returncode != 0 or probe.stdout != b"":
                    raise RuntimeError("Public checker container cleanup unverified")
    print("Public checker cases passed:", len(cases), "(not mathematical qualification)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    run(parser.parse_args().root.absolute())
'''
