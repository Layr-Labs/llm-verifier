# Shared LLM verifier

`llm_verifier` is an installable Python 3.11+ package and `llm-verifier` CLI for
organizer-defined mathematical challenges. It snapshots a submission, captures
bounded computational evidence in Docker, asks one OpenAI reviewer for a
judgment or one optional follow-up batch, and writes one Yukon result plus an
inspectable dossier. The core has no GitHub or database dependency.

Version 0.6 uses atomic acceptance of the original declared claim set, automatic
submission-ID references and the mandatory
[scientific evidence policy](docs/evidence-policy.md), retaining editable challenge
starters, shared checker tests and deterministic scores from current proof claims
and protected checker outputs. It is an LLM assessment, not formal certification or an
authoritative benchmark. The examples and scripted results demonstrate
plumbing, not mathematical reliability. The internal repository is
`Layr-Labs/llm-verifier`. Publishing reviewed source internally does not validate
hosted execution. No live model calls, hosted Actions run, challenge import or
deployment have been performed for this implementation.

Every review applies the [shared external-evidence policy](docs/external-evidence.md).

## Install and try offline

From this checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/llm-verifier --help
.venv/bin/llm-verifier simulate --root examples/proof \
  --script examples/proof/simulation.json --out .work/proof-simulation.json
.venv/bin/llm-verifier simulate --root examples/computation \
  --script examples/computation/simulation.json --out .work/computation-simulation.json
```

There are **no runtime Python dependencies**. Builds pin `setuptools==75.8.0` and
`wheel==0.45.1`; installation may download those build tools. With repository
access, Git-pinned installation is also possible once the reviewed commit is
published internally:

```sh
# TEMPLATE: use the reviewed full 40-character commit, not a branch/tag.
python3 -m pip install 'llm-verifier @ git+https://github.com/Layr-Labs/llm-verifier.git@FULL_40_CHARACTER_COMMIT'
```

Simulation never calls a model or executes submitted Python. Its scripted
provider and execution results are visibly marked, and it writes a
`simulation_only: true, publishable: false` envelope. It cannot write a
Yukon-compatible top-level score; ordinary finalization rejects any simulated
phase. Injecting a custom provider, runner or OpenAI transport also marks
its artifacts as simulation. Production workflow inputs cannot select simulation.

## Create a challenge

```sh
llm-verifier init my-challenge --name my-challenge --model OPENAI_MODEL
```

`init` writes a form: the rules, configuration and starting submission, with a
`TODO:` marker wherever the value depends on the problem. Replace every marker,
then run `llm-verifier check --root my-challenge`, which refuses any starter
placeholder that remains and names where it is. The [authoring guide](docs/authoring.md)
covers proof files, deterministic scores, checker tests and the optional pinned
Yukon/Actions caller.
For agents, start with the shared [challenge-authoring skill](skills/llm-verifier-challenge/SKILL.md).
The [atomic decision contract](docs/atomic-claims.md) also applies without references.
Solvers can cite exact accepted prior results through the
[automatic reference workflow](docs/prior-results.md), with no owner import setup.

Before uploading a candidate package, run the credential-free check on its exact subtree:

```sh
llm-verifier preflight --root /path/to/challenge --submission submission
```

This checks package limits, manifest semantics and provider response-schema
construction without executing code or calling a model. Organizers can opt into
the narrow [CurveSmash manifest adapter](docs/preprocessing.md); ordinary challenge
requirements remain unchanged when it is omitted.

## Independent phases

Keep protected rules/configuration outside `submission/`; keep all `.work/`
outputs outside candidate-editable paths. For a configured challenge root:

```sh
llm-verifier intake --root /path/to/challenge --out .work/intake.json
llm-verifier prepare-runtime --intake .work/intake.json
llm-verifier experiments --intake .work/intake.json --out .work/initial.json
```

These commands need no model credential. `prepare-runtime` explicitly pulls the
organizer's digest-pinned image when the selected phase can execute work; execution
uses `--pull=never`. An empty initial plan needs no Docker service, even when
checkers are registered for potential follow-up. Docker execution is
Linux/amd64 through `/var/run/docker.sock`; there is no host execution fallback.

The organizer must replace the examples' `SELECT_OPENAI_MODEL` placeholder with
an explicit accessible native OpenAI model ID in `provider.model` (no `openai/`
prefix) and authorize any paid use. The placeholder works only for offline
validation/simulation. A separate review process/job receives `OPENAI_API_KEY`
in its environment:

```sh
# Credentialed REVIEW job; never execute candidates in this job.
llm-verifier review-initial --intake .work/intake.json \
  --initial .work/initial.json --out .work/review.json
```

If `.data.decision.kind` is `followup`, transfer artifacts to a fresh,
credential-free execution job, then review in a separate credentialed job:

```sh
# Credential-free execution job (fresh worker):
llm-verifier prepare-runtime --phase followup --intake .work/intake.json
llm-verifier followup --intake .work/intake.json --initial .work/initial.json \
  --initial-review .work/review.json --out .work/followup.json
# Separate credentialed REVIEW job:
llm-verifier review-final --intake .work/intake.json --initial .work/initial.json \
  --initial-review .work/review.json --followup .work/followup.json \
  --out .work/final-review.json
```

Finalization has no model credential and executes no candidate code:

```sh
# Direct initial judgment (including proof-only):
llm-verifier finalize --intake .work/intake.json --initial .work/initial.json \
  --initial-review .work/review.json --score .work/score.json --dossier .work/dossier.json \
  --results .work/accepted-results.json
# If and only if a follow-up was requested, add:
# --followup .work/followup.json --final-review .work/final-review.json
```

There is deliberately no combined execute-with-secrets command. Artifacts can be
passed to library functions in `config`, `artifacts`, and `pipeline` instead of
using the CLI. Every consuming phase validates config, snapshot, implementation
and previous-stage bindings. Set `LLMV_EXPECTED_INTAKE` or `--expected-intake` to
the independently trusted intake digest when transferring artifacts; the hosted
workflow always does this. Hashes detect changes and cross-evaluation replay;
**they do not authenticate candidate-supplied artifacts**. Only organizer-produced
artifacts from the current trusted run may be consumed.

## Challenge configuration and examples

- [Proof-only lemma](examples/proof): accepts a relevant two-variable lemma with
  `scores: null`. It reserves one genuine full-target objective to fit Yukon's
  1–8 objective contract, but awards no artificial score to the lemma.
- [Scored proof](examples/scored-proof): a complete challenge that `init` and a filled
  form produce. The score comes from the claim, with both bounds, the four rules
  sections and a participant README.
- [Computation-backed identity](examples/computation): the same implementation
  checks submitted finite experiments and permits one bounded follow-up batch.
- [Toy truncated-hash collision](examples/collision): a protected checker verifies
  the declared pair before numeric scoring; a separate lemma needs no witness.
  [Checker registry and score gate](docs/checkers.md) explain the exact guarantees.
- [Configuration and input dialect](docs/configuration.md), with reference
  [config](docs/config.schema.json) and [submission](docs/submission.schema.json)
  schemas. Python validators also enforce cross-field and budget constraints.
- [Artifact, provider and execution contracts](docs/contracts.md).
- [Reusable workflow and caller template](docs/workflow.md).

The example manifests use one-track schema v2. Their `setupCommand` and
`benchmarkCommand` are phase descriptors: intake and finalization. **Local
`yukon run` is unsupported.** Yukon invokes setup immediately before
benchmark, without a pause for execution/review. Setup creates a new intake
identity, so precomputing review artifacts does not make `yukon run` work.
Use the independent `llm-verifier` phases above directly for local evaluation.
The computation manifest's finalization descriptor expects its demonstrated
follow-up files; for a direct judgment omit those flags using the CLI. The hosted
reusable workflow orchestrates all phases and handles both branches automatically.

## Yukon output and failure semantics

`YUKON_EVALUATION` supplies exact `policies`, boolean `fields.scores` and
`fields.validity`, plus optional `admission`. The dispatch input
`yukon_evaluation` is passed through unchanged. Unsupported policy selectors and
non-null admission policies fail explicitly. Only requested result fields are
written. Valid unscored work has an explicit `scores: null`; a scored tuple has
one finite number per objective. Score-only review still checks legitimate
claims; an invalid score-only assessment fails execution instead of replacing
retained validity. See the contract document for details.

A completed invalid judgment exits **0**, with `valid: false` and a reason when
validity was requested. Malformed data, unavailable Docker, provider errors,
insufficient infrastructure budgets or artifact mismatches exit **2**. The CLI
clears its requested stage output before work; finalization clears stale score
dossier, accepted-result and receipt files **before reading any artifact**, and writes the score only
after complete chain validation. Failed experiments remain explicit evidence;
they never automatically become acceptance. When required score checks are
configured, missing or failed checks forbid every non-null tuple; unscored
judgments remain subject to `allow_unscored`. Missing/failed follow-up stages
cannot be finalized.

## Verification and scope

```sh
PYTHONPATH=src python3 -m unittest discover -v
# Explicitly prepare the pinned image first; no model calls are involved:
llm-verifier intake --root examples/computation --out .work/docker-intake.json
llm-verifier prepare-runtime --intake .work/docker-intake.json
LLMV_DOCKER_INTEGRATION=1 PYTHONPATH=src python3 -m unittest tests.test_runner.DockerIntegration tests.test_checks.CheckerDockerIntegration -v
# If actionlint is installed:
actionlint .github/workflows/verify.yml
```

The default suite skips the separately gated real Docker tests. See
[validation record](docs/validation.md) for tests actually run. Scope is Python
standard-library programs, one initial reviewer plus at most one final reviewer,
and one optional batch of experiments/checks. Sage, Lean, native-language toolchains,
Full HashSmash support and its reviewer committee, challenge-specific
admission dialects, formal/benchmark certification, package registry distribution
and a hosted service are deferred. Public release, hosted deployment and license
selection remain outside the internal source publication scope.

Accepted-submission references require explicit, original-candidate
[submission admission](docs/submission-admission.md). Missing/null defaults off.
The existing owner API updates future candidates per Pareto track without changing
the pinned verifier or starting a reorg; retries retain the original policy.
