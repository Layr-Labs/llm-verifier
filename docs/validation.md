# Atomic implementation validation

Local validation on 2026-10-16 used fresh master
`f1b9cd862e0b034e89267b88e301b583305c7289`, preserving current editable authoring,
checker CI and exact objective bounds. Source is staged and uncommitted for review.
There are no verifier migrations or compatibility adapters.

- A locally built 0.6.0 wheel was installed in a new virtual environment. With
  `PYTHONPATH` removed and real Docker gates enabled, all **149 tests passed**,
  with zero skips. Five tests use the existing digest-pinned Docker image; no
  image pull was needed. `pip check` passed. All 30 package source/template files
  match the local source bytes.
- Atomic tests cover rejection of a declared set containing an unsupported claim,
  accepted unscored lemmas, rejection of subset/condition catalogs, minimal
  initial/final prompts, exact original manifest bytes, source/phase tampering,
  simulation publication restrictions, receipt cleanup and output aliases.
- Current score/check tests cover finite rationals, exact lower/upper bounds,
  positive infinity, no model score override, missing/conflicting/current canonical
  checks, no partial score records and no inherited prior score or witness.
  Fresh-master intake/authoring bounds and checker CI are retained.
- Actual local Yukon capture/storage and signed-OIDC resolver tests emitted
  current atomic source snapshots for finite, unscored and infinity sources,
  plus the same finite submission after a fresh accepting judgment. The installed
  verifier consumed those exact response bytes through worker decoding, intake,
  review, full-chain export and publication receipts. Before/after reacceptance
  preserves the submission selector, original source digest and manifest bytes
  while changing evaluation/artifact provenance.
- Bounded source-graph checks reject direct cycles and self/ancestor imports while
  preserving acyclic shared ancestors and later rejudgment timestamps. Native
  original ordering, retention/access/reorg transactions and publication races
  are platform responsibilities, exercised by the paired Yukon implementation.
- Both checked-in workflows pass actionlint. Current authoring/init/check/github
  behavior and scripted examples remain covered by the installed suite.

Build used locally available setuptools 82.0.1 with `--no-index --no-deps
--no-build-isolation`, avoiding network package calls. Hosted builds retain the
existing declared build requirements. No model request, hosted workflow dispatch,
publication, deployment or live platform mutation was performed. Scripted model
responses test plumbing and invariants; they do not establish mathematical truth,
complete dependency discovery, self-contained wording or correct entailment.

## Direct OpenAI migration (2026-10-16)

Provider-only validation from published master
`f1b9cd862e0b034e89267b88e301b583305c7289`:

- Python 3.13.1 source suite: 124 discovered, 119 passed and five gated Docker
  skips. A fresh wheel installation, with `PYTHONPATH` removed and
  `LLMV_DOCKER_INTEGRATION=1`, passed all 124 tests with no skips.
- Mocked direct Chat Completions requests check the fixed OpenAI endpoint and
  headers, `max_completion_tokens`, `store: false`, explicit response formats,
  native model IDs, credential selection, strict response/refusal handling,
  redaction, retries, aggregate deadlines and nonpublishable injected transports.
  Existing localhost trickle tests exercise the killable HTTP worker.
- Real Docker tests use the existing pinned image and verify that
  `OPENAI_API_KEY` never enters candidate or checker containers, alongside the
  existing network, filesystem, output and timeout boundaries.
- Wheel and source distribution build successfully. Installed runtime/template
  bytes match source; `pip check`, all four offline example branches, and
  installed `init`/`check`/`github` smoke checks pass.
- `actionlint` 1.7.12 accepts all three implementation workflows and both generated
  callers. An independent JSON Schema validator accepts all four source configs
  and rejects 36 obsolete/invalid provider variants. Local README/docs links and
  `git diff --check` pass.

These tests use mocked model responses. No live or paid OpenAI request, hosted
workflow dispatch, model qualification or challenge rollout is part of this
validation. The earlier validation records above describe their historical
implementation. The provider-only record does not by itself establish atomic integration.


## Combined atomic acceptance and direct OpenAI (2026-10-16)

An isolated checkout reconstructed the exact atomic-final staged tree
`3fadb57f94a798b26281a5f8684be11cdd96239f` on base
`f1b9cd862e0b034e89267b88e301b583305c7289`, then applied the reviewed provider-only
patch for tree `41cc7f493da13191080286d64317bfe5629e0696`.

- The installed combined 0.6.0 wheel passed all **160 tests**, including all five
  gated Docker tests with zero skips. The pinned image was already installed;
  no image pull was performed. API credentials and `PYTHONPATH` were removed.
- All 30 installed package source/template files match final source bytes;
  `pip check` passes. Actionlint accepts the three implementation workflows and
  both packaged callers without suppression.
- Three reference schemas and 29 documents pass independent JSON Schema checks,
  including 12 generated initial/final schemas with exactly the five atomic
  judgment fields. All 54 obsolete or invalid provider-config variants fail.
- `tests.verify_yukon_snapshot` consumes the exact atomic-final platform sources
  and Yukon snapshots through installed intake, review, export and receipts.
  Finite, reaccepted finite, unscored and infinity cases pass. Reacceptance retains
  submission ID, source digest and exact manifest bytes while provenance changes.
- Integration preserves atomic judgment, prior-result and score semantics. The
  only runtime conflict retained atomic evidence-policy normalization while
  removing obsolete routing settings. Atomic-only fixtures now use native model
  IDs; the prior-result examples use the same guarded placeholder as other examples.

The combined runtime SHA-256 is
`98933b51fdcf1d2044dbef75283e967065418d935f9abc4803fcc6cfc85ebf2d`.
The source, full-index patch, installed wheel, input checksums and validation
reports are frozen in the combined integration thread handoff. Original atomic
and provider artifacts remain unchanged. This is local validation with mocked
model responses; it includes no live model call, commit, push or publication.

## Long hosted review repair (2026-10-17)

EC2 validation runs 161 source tests, with five gated Docker skips; all 37
provider tests pass. Background coverage includes a real private-worker HTTP
fixture (creation, same-ID polling, deletion), exact native request bytes,
store=false, no input truncation, schema/effort preservation, transient poll
errors, hard poll/time bounds, cancellation/cleanup, safe response IDs,
refusal/tool/truncation rejection, usage normalization and exact mode-specific
endpoint provenance. Existing killable-worker and credential-placement
regressions remain green. Hosted background execution awaits explicit approval.

## Challenge-owned judge committees (2026-10-22)

Implemented locally from master `6bdefc130ec7215697eccf4339853343f9282920`.
A fresh 0.7.0 wheel installation passed all **257 tests**, with zero skips,
including 24 committee tests and the five existing real Docker checks. Model
credentials and `PYTHONPATH` were removed for the installed suite; no image pull
was required. All 44 installed runtime/resource files match the source and wheel.

Committee coverage includes protected local catalogs and immutable prompt snapshots,
profile composition and model overrides, independent reviewer inputs, exact
blocking-finding defense/resolution coverage, rejected unresolved acceptance,
forged/reordered/missing stages, shared request/token/retry/wall budgets, the single
follow-up batch, simulation publication restrictions, exact score/check gates,
prior-result lineage and unavailable support, and protected workflow output paths.
The legacy normalized-configuration digest regression remains unchanged and passes.

Independent JSON Schema validation passed six reference schemas, seven example
configs, bundled/local catalogs and selection, and 14 generated strict stage
schemas with scripted reports. All three hosted workflows and both generated
callers pass actionlint. Installed CLI discovery, check and committee simulation
pass; `pip check` and `git diff --check` pass.

This establishes local implementation and mechanical validation. No live model
call, hosted committee evaluation, publication or deployment is part of this run.

## HashSmash preset publication validation (2026-10-22)

The final 0.7.0 package adds `builtin:hashsmash-v1`, retaining HashSmash's six
role IDs and adapting its review priorities to the shared verifier protocol.
Three new reusable personas cover evaluability, cryptanalysis and adversarial
experiment assessment. The catalog contains ten personas and three profiles.
The guide documents inherited model settings, per-role overrides and the limits
of the role mapping; HashSmash-specific paired lane policy is not imported.

The fresh installed suite passes **259 tests, zero skips**, including 26 committee
tests and five real Docker checks. The two added cases cover the exact six-role
mapping, inherited/overridden provider settings, immutable intake, and a full
blocking-finding defense/adjudication through final dossier validation. The
installed catalog, protected configuration check and offline HashSmash simulation
also pass. All 47 installed runtime/resource files match the final source and wheel.
Six reference schemas, all three bundled profiles, their 26 generated stage schemas
and the documented selectors validate; all three production workflows pass actionlint.

Calibration remains deferred. No live model request or challenge rollout is part
of this publication validation.
