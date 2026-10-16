# Reusable workflow

`.github/workflows/verify.yml` is the sole implementation workflow. A protected
caller in each challenge repository dispatches it; candidates cannot edit callers,
verifier pins, config, rules, runtime or outputs.

The workflow has six jobs: prepare/intake, initial execution, initial review,
optional follow-up execution, optional final review, and deterministic finalization.
Only prepare checks out repositories and can use a private verifier checkout
token. It checks out the challenge at actual `github.sha`, and separately checks
out the verifier at an explicit full SHA, both with `persist-credentials: false`.
It builds a wheel once, then passes it and inert intake through same-run artifacts.
Execution jobs receive no supplied secrets or checkout tokens; Docker receives an
explicit minimal environment and no GitHub artifact credentials. Only the two
review steps receive the OpenAI secret, and neither executes candidates.

## Pins and availability

The caller's reusable-workflow `uses: ...@SHA` and the `verifier_revision` input
must identify the **same full commit**, in `verifier_repository`. Before any
checkout, a bootstrap step reads GitHub.com's `job.workflow_repository` and
`job.workflow_sha` via `toJSON(job)` in **step env**, then requires exact equality
with the supplied pins. Missing identity or a mismatch fails. `toJSON` avoids
stale actionlint property tables without suppressing lint categories. This relies
on GitHub.com called-workflow identity; GHES/other workflow engines without those
fields are not supported by this initial wrapper. The core CLI has no such
platform dependency.

After checkout, `workflow-prepare` independently checks the actual clean Git
revisions. Upgrading means reviewing a new verifier commit, updating **both** the
caller `uses` pin and implementation input together, and using the applicable
Yukon reorg process. A change to only one pin fails before source installation.

Checkout/upload/download actions use the exact known pins from the inspected
HashSmash workflow. No callee checkout is assumed, no Git submodules are used,
and no input expression is interpolated into shell source. Shell arguments arrive
through environment variables and are quoted. Bootstrap has no candidate imports.

## Caller template

Generate the caller and matching manifest with `llm-verifier github --root .
--verifier-revision FULL_COMMIT`. See [authoring](authoring.md#yukon-and-github-actions)
for the command and [the shared caller template](../src/llm_verifier/templates/benchmark.yml)
for its source. Configure repository access before running it.
The placeholders are not deployable commit hashes. Set the
protected `root_dir` to the imported leaf path (or `.`), and use config/submission/
score paths relative to that root. The examples' manifests refer to this filename.
Configure `OPENAI_API_KEY`, and `VERIFIER_CHECKOUT_TOKEN` only if a separate
private or internal verifier repository needs access; the caller's default token
may not have cross-repository access. For a private/internal reusable workflow,
its repository's **Actions → General → Access** setting must also allow the
caller repository. A checkout token does not grant reusable-workflow access;
`llm-verifier check` validates local files, not these GitHub settings.

The caller must declare string dispatch input `yukon_evaluation`; the wrapper
passes it to `YUKON_EVALUATION` at intake and snapshots the exact selected policies
and fields. No artifact or credential is sent by the candidate. Protect all
configuration and workflow paths in the Yukon manifest.

The wrapper supports one challenge evaluation per workflow run. Do not call it
multiple times in a matrix/run without adding organizer-controlled distinct
artifact namespaces and corresponding Yukon selection. The included independent
one-track examples can each be imported from their own root, after publication
and a real accepted hosted smoke test. No import has been performed here.

## Finalization and artifact layout

`finalize` uses `always()` plus explicit success conditions for mandatory jobs,
and requires both optional jobs only when initial review requested follow-up.
Downloads of optional artifacts use the same condition. A failed job cannot be
substituted by a previous successful review. The trusted intake digest is passed
to every consuming phase through `LLMV_EXPECTED_INTAKE`.

The only score upload is `benchmark-score`, containing exactly the manifest-relative
`score_path`, e.g. `.yukon/score.json`, including hidden directories. `root_dir` is
**not** prepended to that archive path. Finalization validates this path against
intake context, stages it in a fresh directory outside the candidate checkout and
uploads it once. The manifest sets `runner.scoreArtifactName` to `benchmark-score`.
The dossier is a separate artifact; phase files do not have the score filename.

The example `setupCommand` and `benchmarkCommand` are phase descriptors for
intake and finalization. **Local `yukon run` is unsupported in v0.2.** Yukon calls
setup immediately before benchmark without pausing for the intermediate phases;
setup generates a new intake UUID and invalidates any previously prepared review
chain. Running the separate phases beforehand does not fix that behavior. Use
`llm-verifier` phases directly for local evaluation. The computation example's
finalization descriptor expects its demonstration's follow-up files; direct
judgments omit those flags. There is no credential-bearing combined local
runner. Hosted workflow execution is complete for proof-only, direct judgment
and requested follow-up branches.

Official references checked for this implementation:

- [GitHub job context workflow identity](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#example-usage-of-job-context-workflow-identity)
- [OpenAI Chat Completions](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)

Yukon compatibility was checked against the supplied `pareto-mode.md` and
`github-actions-benchmark-author-guide.md` source checkout. Parent independently
ran the exact Yukon manifest/result parser on both example manifests and scoped
result shapes. An actual GitHub-hosted workflow and live model acceptance remain
unvalidated and require separately authorized validation. Internal repository
publication alone does not establish either, or authorize a workflow dispatch,
paid model call or challenge import.

## Protected registry (0.2)

The same six jobs carry candidate experiments and organizer checkers. Keep every
checker source/target bundle path outside editable submission paths. Runtime
preparation follows the actual initial plan, including checker-only plans. Empty
initial plans need no Docker. The conditional follow-up job passes `--phase
followup`, preparing judge-selectable entrypoints on its fresh worker. The workflow
rejects a scorePath that would overwrite any declared protected checker file.
[Checker source snapshots, shared budgets and the numeric score gate](checkers.md)
are validated through the existing intake/phase artifact channel. No checker
source executes in a credentialed review job.

## Automatic challenge checker CI

`llm-verifier github` also generates a small push/pull-request/workflow-dispatch
caller of `.github/workflows/test-checkers.yml`. Its workflow and source pin
must match exactly, as for the benchmark. It validates the challenge and runs
`checker-tests.json` using the production `test-checkers` command; it does not
call a model or run participant-declared experiments.

Only the preparation job checks out repositories. Both checkouts disable
credential persistence; the optional read-only `VERIFIER_CHECKOUT_TOKEN` is
used only for verifier checkout. The prepare job verifies exact clean revisions,
builds a wheel and archives the challenge's Git tree. A separate job with no
supplied secrets and empty GitHub-token permissions installs that wheel,
restores the source, explicitly prepares the pinned Docker image, and runs
checks with a process environment containing only PATH. There is no second
checker runner or model secret in this workflow.

Configure reusable-workflow access and cross-repository read credentials before
expecting a green check. A missing credential/access grant is an infrastructure
failure, not a skipped or successful checker run. Fork PRs do not receive caller
secrets; use an organizer-controlled branch for those changes without switching
to `pull_request_target`. The workflow supports one checker caller per run, with
one artifact namespace and a 15-minute execution-job limit.
