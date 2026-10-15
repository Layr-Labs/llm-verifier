# Author a challenge

Install this verifier revision, then create a six-file working example:

```sh
llm-verifier init my-challenge --name my-challenge --model PROVIDER/MODEL
llm-verifier check --root my-challenge
```

Edit `rules.md`, `verifier.json`, and `submission/` directly. Rules contain the
scientific specification; configuration selects objectives, a reviewer and any
checkers. Keep the example proof in one file. There is no external definition,
controller receipt, copied runner, generated skill, or fixture generator.

`check` validates the config, candidate snapshot, evidence references, checker
bundles and known-answer cases. It executes no code or model. Unrelated authoring
files and caches outside `submission/` are allowed. Keep protected config, rules
and checker files outside participant-editable paths. Repository access controls
and the organizer's trusted checkout provide authority; this command is not a
repository attestation or a restriction on an authoring agent's permissions.

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
block acceptance; diagnostic inputs cannot replace the declared witness.

The verifier binds the source to the complete immutable submission snapshot and
supplies a `claim:<hash>` ID to the reviewer. The model returns `scores: null` and
accepts the exact ID only when the claim meets the rules. The dossier derives the
numeric tuple from those acceptances and retains exact records. Acceptance of a
lemma or an ID from another snapshot awards no score. Every required checker must
still pass. Partial objective acceptance leaves the whole tuple unscored.

A maximizing proof claim can declare `"score": "positive_infinity"`. If accepted,
its exact record is retained with `unsupported_positive_infinity` projection and
null numeric scores. Yukon does not natively order this endpoint. Finite rationals
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
contains working cases. Keep framework protocol tests upstream; challenge tests
should cover its scientific or computational behavior.

## Yukon and GitHub Actions

Once the verifier revision is published and accessible to the caller:

```sh
llm-verifier github --root my-challenge --verifier-revision FULL_40_CHARACTER_COMMIT
```

This writes `benchmark.json` and `.github/workflows/benchmark.yml`. Rerun it after
changing objectives or policies; `check` detects a stale generated manifest.
Both workflow and source use the same supplied pin. Generated JSON is formatted
for review. Configure reusable-workflow access and the checkout/model credentials
as described in [workflow.md](workflow.md). The caller uses the existing separated
execution/review jobs. For local evaluation use the [independent phases](../README.md#independent-phases);
local `yukon run` does not orchestrate them.
