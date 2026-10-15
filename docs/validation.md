# Implementation validation

Validation is offline and scoped to implementation contracts. This is not a live
mathematical benchmark result or hosted workflow acceptance.

- Python 3.13.1 local unittest suite: contract, strict input/file boundaries,
  provider failure redaction, HTTP worker deadlines with localhost trickled
  headers/body, workflow bootstrap pins, CLI exit/stale-output behavior,
  simulation isolation and Docker command/capture/cleanup tests passed.
- Actual Docker 29.6.0 with the configured digest-pinned linux/amd64 Python image:
  both gated tests passed, covering finite experiment output, UID 65534,
  read-only snapshot, no key/socket, loopback-only network, wall timeout and
  bounded output. The default unit suite skips these two tests unless enabled.
- Parent reproductions for failed container cleanup and a killed host Docker
  process now raise infrastructure errors. Injected fake OpenRouter transport
  cannot finalize an official score.
- `actionlint` 1.7.12: workflow passes without suppression. Bootstrap identity
  matching/mismatching cases also run locally as the actual shell block.
- Both bundled examples complete scripted offline CLI simulation. Parent's
  independent Yukon schema/result parser accepted both one-track v2 manifests
  and tested unscored/invalid/field-scoped result shapes.

Final verification on 2026-10-14:

- `env -u PYTHONPATH .venv/bin/python -m unittest discover -v`: **55 discovered,
  53 passed, 2 skipped** (the separately gated Docker tests).
- `env -u PYTHONPATH LLMV_DOCKER_INTEGRATION=1 .venv/bin/python -m unittest
  tests.test_runner.DockerIntegration -v`: **2 discovered, 2 passed, 0 skipped**.
  All 55 unique tests therefore passed across those two runs.
- A fresh wheel from final source installed as `llm-verifier==0.1.0` in `.venv`;
  the full suite above ran against that installed package, with `PYTHONPATH`
  removed. Both installed `llm-verifier simulate` examples completed.
  `python -m pip check` reported no broken requirements.
- The typed-JSON parent reproduction now rejects integer-to-float manifest
  substitution and integer-to-boolean parsed-result substitution. Regression
  tests also cover request/budget copies and Yukon UTF-16 reason length.
- Final actionlint and local documentation link checks passed. The optional
  independent `jsonschema` reference-file cross-check was unavailable in the
  host Python environment; no runtime dependency was added for it. Executable
  config/submission validators cover both example documents in the suite.

Local `yukon run` is explicitly unsupported: setup immediately regenerates intake
before finalization. Local verification uses the separate CLI phases directly.

At the initial validation recorded above, source was left uncommitted for parent
review; no push or publication had occurred. Subsequent internal source
publication to `Layr-Labs/llm-verifier` does not extend that validation to hosted
execution. No live OpenRouter calls, hosted GitHub Actions run, paid benchmark,
challenge import or deployment has been performed.

## Protected checker registry / 0.2.0 (2026-10-15)

Implemented and verified in a new isolated worktree starting at published commit
`d204edd7f7a2a88c18c519421700ff8ff6a784b9`. At this validation, source was left
uncommitted for review; any subsequent draft PR publication does not extend this
validation to hosted execution.
The original 55 tests are retained. A fresh 0.2.0 wheel was built using the
existing pinned build requirements, installed in a new virtual environment, and
all following Python suites used that installed package with `PYTHONPATH` unset:

- Default discovery: **87 tests, 82 passed, 5 Docker-gated skips**.
- Explicit real Docker suite: **5 tests, 5 passed, 0 skipped**, using the installed
  pinned image with no pull. All 87 unique repository tests passed across the two
  runs. This includes the original two Docker tests and three new checker tests.
- New tests cover literal boolean/schema gating; missing declarations and
  unscored validity policy; exact-input retries versus different/undeclared
  cherry-picked passes; typed numeric bindings; invalid and hidden-score
  judgments; stale final score removal; candidate ID/source collisions; bundle,
  requester, source, input, output and replay tampering; explicit aggregate file
  and run budgets; no-follow paths; prompt/evidence separation; native strict
  schema variants and optional input behavior; complete mocked CLI phases;
  phase-aware runtime preparation; and both collision simulation branches.
- Real Docker checks confirm the toy pair and diagnostic outputs, selected-bundle
  isolation, no candidate import/host credentials/socket/network, read-only
  source, nonroot UID, fresh tmpfs between runs, timeout and schema-invalid
  output retained as unscored evidence. Model responses remain mocked.
- `actionlint` 1.7.12 accepts the updated workflow; local documentation links,
  `pip check`, and exact installed/source runtime byte comparison pass.
- Existing proof/computation and new scored/unscored collision simulations pass
  through the installed module CLI. Simulation cannot publish an official score.

The independent review's no-work runtime finding was corrected: default initial
preparation uses the actual initial plan, while the conditional follow-up job
explicitly selects `--phase followup` on its fresh worker. Thus an unscored lemma
with no runs needs no Docker despite a registered required checker.

During this validation, no live model call, hosted workflow dispatch, challenge
import, paid benchmark, publication or change to original owner worktrees was
performed.
The gate verifies a scoped predicate, not arbitrary objective values or broader
mathematical claims. Local `yukon run` and every non-null admission policy remain
unsupported. See [protected checks](checkers.md) for the exact limits.

## Approved-definition scaffolding and score binding / 0.3.0 (2026-10-15)

Started from upstream master `5b67e8d51c3bd1f9c245c3f0978fe8f9bf2b1c8d`.
The unchanged baseline passed all 87 unique tests (82 offline plus five separately
gated real Docker tests). New tests cover exact accepted-claim identity, numeric
mismatches, malformed/reduced rationals, binary64 precision limits, terminal
classification, mixed-objective null tuples, conflicting canonical retries,
invalid/hidden-score judgments, and stale-output cleanup.

Generator tests use fresh temporary directories and cover round trips, exact
source/rule preservation, immutable pins/model selection, trusted external
receipt and protected-file validation, forged inventories, source path/symlink
boundaries, no-overwrite behavior, configured rules paths, bounded public checker
capture and subprocess CLI init/context/check. The generated collision starter
completed its two scripted branches and its two real Docker public checker
cases without model credentials. Public conformance is not mathematical
qualification. Source and generated workflows pass actionlint.

CircuitBreaker dogfooding found missing required simulation fields and missing
source evidence references in authored fixtures; the generator rejected both
before exporting a challenge. Its scientific-content owner corrected those
cases. Long scientific proof payloads still share the existing aggregate 16 KiB
input bound; chunking does not bypass it. Exact rational and terminal semantic
records have separate documented Yukon transport limitations. No platform schema
or repository access settings were changed by this work.
