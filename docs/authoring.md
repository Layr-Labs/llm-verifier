# Author a challenge

Install this verifier revision, then create a challenge form:

```sh
llm-verifier init my-challenge --name my-challenge --model OPENAI_MODEL
```

Replace `OPENAI_MODEL` with an explicitly selected native OpenAI model ID; there
is no default. Review processes alone need `OPENAI_API_KEY`.

`init` writes the files with `TODO` markers wherever the value depends on the
problem, and prints where they are. Replace every marker, then run:

```sh
llm-verifier check --root my-challenge
```

`check` and `github` refuse the challenge while any of the starter's own placeholders
remains, and name where it is: a file and line, or a field in a JSON file. Your own
notes and participants' text may say TODO; only the starter's placeholders count. For a complete
worked example of a scored proof, see [examples/scored-proof](../examples/scored-proof). Rules contain the
scientific specification; configuration selects objectives, a reviewer and any
checkers. Keep the starting proof in one file. There is no external definition,
controller receipt, copied runner, generated skill, or fixture generator.

`check` also validates the config, candidate snapshot, evidence references, checker
bundles and known-answer cases. It executes no code or model. Unrelated authoring
files and caches outside `submission/` are allowed. It validates prior-result
selector syntax locally; authenticated resolution occurs at evaluation intake,
so ordinary editing and `check` need no platform credentials. Keep protected config, rules
and checker files outside participant-editable paths. Repository access controls
and the organizer's trusted checkout provide authority; this command is not a
repository attestation or a restriction on an authoring agent's permissions.

## What init fills in and what you fill in

`init` fills in the values that have one known answer:

- The review label: `review_standard` becomes `<challenge id>-review-v1`. Raise the
  version when the review rules change.
- Reviewer settings for `gpt-6-astra`: CircuitBreaker's reviewer finished a hosted
  review of its long baseline proof
  ([run](https://github.com/Layr-Labs/circuitbreaker-llm-verifier/actions/runs/39051956398))
  with `"reasoning_effort": "high"`, `"max_completion_tokens": 32768`,
  `"timeout_seconds": 1200`, `"background": true`, `"max_attempts": 1` and
  `"total_review_seconds": 2400`, so `init` writes these for that model. For another
  model, choose and qualify the settings yourself; see
  [provider configuration](configuration.md#provider-configuration). Background mode
  keeps temporary response state at OpenAI for about ten minutes.

You fill in each `TODO`:

- Rules: keep the four sections (problem and model, score, what does not count, and
  submission and review). Write known invalid arguments under "What does not count"
  as rules, not as worked cases.
- Score name: a descriptive snake_case name for the scored quantity, such as
  `gate_coefficient`, not a bare symbol such as `c`.
- Score bounds: an exact `score_minimum` and `score_maximum` whenever the rules fix
  them, for example a known optimum, or a lower limit below which every claim is
  trivially true. Delete a bound line only when the score has no limit in that
  direction. Bounds need a `score_source`; see [score sources](#score-sources).
- Partial results: `allow_unscored` is `false` when every submission must claim a
  score, and `true` only when the rules accept unscored lemmas.
- README: what participants need: the problem, how to submit and how review works.
  Keep the generated preparation checklist and guide pointer in README and AGENTS.
  Give solver-launching agents that pointer in their initial context; an upstream
  skill alone does not ensure discovery. Add challenge-specific instructions
  around the marked blocks. Root `AGENTS.md` discovery depends on the agent client.
  For other launchers, supply the pointer in the instruction surface they actually
  load (for example BB's `.bb/AGENTS.md` or the initial task context); the generator
  does not create or replace these other policy files.
- The starting submission: its exact claim, score and complete proof.

Then pin the verifier: pass the newest commit on `master` to `github`, which writes
the same pin to both workflow callers.

## Score sources

Each objective can select one source. If any objective uses a source, all must.
For a proof-backed objective, select a zero-based entry in `submission.claims`:

```json
"score_source": {"claim": 0}
```

That claim has a statement, exact score, and paths relative to `submission/`:

```json
{
  "statement": "For every positive integer n, 1 + ... + n >= n²/2.",
  "score": {"numerator": "1", "denominator": "2"},
  "evidence": ["proof.md"]
}
```

An objective may add inclusive protected `score_minimum` and `score_maximum`
bounds, for example:

```json
"score_source": {"claim": 0},
"score_minimum": {"numerator": "0", "denominator": "1"}
```

Each bound is a reduced finite rational in the same format as a claim score;
when both are set, the minimum must not exceed the maximum. Out-of-range proof
claims are rejected by `check`, intake and artifact validation, even if a model
would accept them. Comparisons use exact fractions before float projection.
An out-of-range checker result cannot authorize a score. Bounds require
`score_source`; omitting them preserves signed scores for other challenges.
Positive infinity exceeds every finite minimum and maximum, so a finite maximum
excludes it. Unscored string claims remain allowed.

Ordinary unscored claims remain strings. Integers in a rational are canonical
strings of at most 256 characters; the fraction must be reduced with a positive
denominator. Challenge rules decide which coefficients and theorems are eligible.
Proofs use the existing submission/file limits, not the 16 KiB checker-input limit.
Do not copy proof text into the manifest or split it into tool-input chunks.

For a computational objective, select a required numeric output field:

```json
"score_source": {"check": "collision", "field": "total_bytes"}
```

The checker must be `required_for_score`; only passing, schema-valid results for
the submission's exact declared inputs qualify. Conflicting canonical retries
make numeric scoring unavailable; diagnostic inputs cannot replace the declared witness.

### Score availability

Every declared claim must be established exactly as written for `valid: true`.
For bound objectives the model returns `scores: null`. The verifier derives the
complete tuple only when validity is true, all current sources are ready and
every required canonical checker passes. Source digests bind the complete current
snapshot; they are provenance, not claim-acceptance selectors. There is no partial
objective acceptance, partial score record export or inheritance from prior results.

If a bound source is absent, checker outputs conflict, a checker result is outside
objective bounds or a required check is missing/failed, scores and score records
are unavailable. Valid work may remain unscored when `allow_unscored: true`;
otherwise a valid judgment without complete permitted records fails validation.
This is separate from mathematical rejection. Structured claim values outside
protected bounds are rejected earlier by `check`, intake and artifact validation,
as described above. They do not reach this unscored branch.

Every review follows [atomic-claims-v1](evidence-policy.md). Declared claims must
include material assumptions, scope, parameters and nonstandard definitions for
interpretation and reuse. Proof files contain the supporting argument. A narrower
lemma belongs in a separate submission. [Prior submissions](prior-results.md)
supply only their exact declared claims, never current scores or checker passes.

A maximizing proof claim can declare `"score": "positive_infinity"`. If accepted,
its exact record is retained with `unsupported_positive_infinity` projection and
null numeric scores. This complete exact endpoint is permitted even with
`allow_unscored: false`; no finite tuple is fabricated. Yukon does not natively order this endpoint. Finite rationals
project to binary64 and may tie despite different exact values. Scientific review
and any manual promotion must account for those platform limits.

## Custom checker tests

Add ordinary checker files and register them in `verifier.json` as described in
[checkers.md](checkers.md). Put cases in `checker-tests.json`, with at least one
case per registered checker:

```json
[
  {
    "id": "identical-messages",
    "check": "collision",
    "inputs": {"left": "m3", "right": "m3"},
    "expected": {"passed": false}
  }
]
```

`expected` can select meaningful output fields; the complete output must also pass
the production schema. Use independently determined positive and negative cases.
Run `llm-verifier check --root .`, explicitly prepare the digest-pinned image from
`verifier.json`, then run `llm-verifier test-checkers --root .` without model
credentials. Tests use the production Docker runner, limits, snapshot isolation
and cleanup. No image is pulled implicitly. The repository's collision example
contains working cases. Run self-tests when authoring or changing a checker;
the dispatched benchmark runs the submission's checks, not the known-answer
corpus. Keep framework protocol tests upstream; challenge tests should cover
its scientific or computational behavior.

## Yukon and GitHub Actions

Once the verifier revision is published and accessible to the caller:

```sh
llm-verifier github --root my-challenge --verifier-revision FULL_40_CHARACTER_COMMIT
```

This writes `benchmark.json`, `.github/workflows/benchmark.yml`, and a short
`.github/workflows/checker-tests.yml` caller for push/PR checker CI. Keep the
benchmark dispatch-only: Yukon dispatches evaluations explicitly, so a
`pull_request` benchmark trigger can evaluate the same submission twice.
Rerun the generator after changing objectives or policies; `check` detects a stale manifest.
Both workflow and source use the same supplied pin. Generated JSON is formatted
for review. Configure reusable-workflow access and the checkout/model credentials
as described in [workflow.md](workflow.md). The benchmark retains separated
execution/review jobs, declares the separate `yukon_accepted_results` input and
grants OIDC permission to its prepare job. No owner source registry, import
allowlist or reference credential is needed. Checker CI uses the shared
`test-checkers.yml`: checkout and wheel preparation occur in one job; structural
validation and production-runner checker cases run in a separate job without
supplied secrets. It needs reusable-workflow access and, for private verifier
checkout, a read-only `VERIFIER_CHECKOUT_TOKEN`, but no model credential.
For local evaluation use the [independent phases](../README.md#independent-phases);
local `yukon run` does not orchestrate them.

`init` also adds a short solver checklist to README and AGENTS, pointing to the
same-install guide and thin skill shipped in the wheel. It does not invent a
revision before one is selected. `github` refreshes these pointers to immutable
links for the supplied repository/commit. It appends the block if absent, replaces
only text between `llm-verifier:solver-guidance:begin` / `end` markers if present,
and preserves all surrounding author prose. Ambiguous or incomplete markers and
unsafe file targets fail rather than overwrite policy text. Other agent-policy
files are untouched. Existing challenges adopt the pointers by explicitly running
`github`; review its workflow/manifest changes as well. See the shared
[solver guide](solving.md) for the preparation and independent critique workflow.

## Public submission admission

Generated tracks initialize version-1 `submissionAdmission` with
`submissionReferencesEnabled: false`; regeneration preserves a valid existing
true/false/null value. `check` validates its shape without enforcing consumption
or resolving references. For imported Pareto tracks, use the existing benchmark
owner PATCH to change future candidates independently of pinned verifier config.
Reimport preserves live admission, and retries/reorgs keep the original candidate
policy. See [submission admission](submission-admission.md) for exact examples.
