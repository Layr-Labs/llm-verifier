# Configuration and submission contract (v1)

`verifier.json` and its root-relative `rules_path` are organizer-owned. The caller
chooses the protected config path, submission directory and output paths.
Candidates can edit only submission content. Configuration objects reject unknown
keys; candidates cannot choose the provider, model, prompt, runtime, commands,
limits or output locations.

## Organizer config

Required fields:

| Field | Meaning |
| --- | --- |
| `version` | Integer `1` |
| `id` | Unique bounded ASCII challenge identifier |
| `rules_path` | Bounded relative POSIX path to UTF-8 rules, outside submission |
| `review_standard` | Exact label required in every judgment |
| `objectives` | 0–8 ordered objects: `name`, `direction` (`+`/`-`), `unit`, `semantics`; optional `score_source` and exact `score_minimum` ([authoring](authoring.md#score-sources)) |
| `allow_unscored` | Whether valid partial work can have `scores: null` |
| `policies` | Default `scoring` and `validity` JSON selectors |
| `supported_policies` | For each field, list of `{selector, instructions}` records |
| `provider` | Explicit `model`; optional settings below |
| `limits` | Optional organizer reductions/overrides within hard caps |
| `runtime` | Optional `{image, platform}`; official Python digest, linux/amd64 |
| `checkers` | Optional protected registry; see [checks and numeric score gate](checkers.md) |

Selectors are compared as exact canonical JSON, including `null`. There is no
implicit version upgrade. Both selected policies must be supported even for a
one-field reorg. The full rules and selected policy instructions enter the
effective-config digest. `YUKON_EVALUATION` is a closed object:

```json
{"policies":{"scoring":{"version":"finite-prefix-v1"},"validity":{"version":"scoped-claims-v1"}},"fields":{"scores":true,"validity":false},"admission":null}
```

At least one field must be true. Missing evaluation uses protected defaults and
requests both fields. Omitted/null admission means no additional admission
policy. Every non-null admission value, including `{}`, is unsupported and
fails before any execution/model call. Future admission dialects need an explicit
version and implementation; no opaque policy is silently ignored.

Locally, zero objectives are permitted only with `allow_unscored: true`. Yukon
requires 1–8 objectives, so do not publish a zero-objective manifest. The proof
example reserves a real full-target objective, documents that the lemma does
not earn it, and emits `null` rather than a fabricated number.

## Budgets

All limits are integers, including CPU and seconds. Defaults and hard maxima:

| Limit | Default | Hard maximum |
| --- | ---: | ---: |
| Files / per-file bytes / total submission bytes | 32 / 65,536 / 262,144 | 128 / 262,144 / 1,048,576 |
| Initial runs / follow-up runs | 3 / 3 | 12 / 8 |
| Per-run / initial-round / follow-up-round / total seconds | 20 / 60 / 60 / 60 | 120 / 600 / 600 / 600 |
| Stdout / stderr bytes per run | 65,536 each | 262,144 each |
| Total reserved output bytes | 393,216 | 4,194,304 |
| Memory MiB / CPUs / processes / tmpfs MiB | 256 / 1 / 32 / 16 | 1024 / 4 / 128 / 64 |
| Complete serialized prompt bytes | 2,097,152 | 8,388,608 |
| Total review seconds / HTTP attempts across both reviews | 180 / 4 | 600 / 6 |

Candidate experiments and organizer checks share all run/time/output budgets.
The registry source union independently uses the same file/count/byte caps as the
candidate snapshot; see [checker bundle limits](checkers.md).

Full run slots and both stream capacities are **reserved**, even if a run ends
quickly or prints little. For example, one initial 20-second run leaves two
follow-up slots under the default total 60-second budget, despite the follow-up
count ceiling of three. Initial/follow-up counts and round/whole-evaluation time
and output reservations must all fit. Timeout does not refund its slot. Docker
preflight fits inside each run's allotted time; cleanup has a separate maximum
three-second allowance per run. The round wall timer includes that allowance.
Explicit image pulling is a separately bounded 180-second setup operation.

Oversized files, entries, prompts or dossiers fail rather than being silently
truncated. Directory entries (including empty directories) are capped at four
times `max_files`; path depth is at most 12. Failed runs retain bounded partial
streams with `complete: false` when capture was interrupted. Whole artifacts are
bounded at 16 MiB; JSON nesting is at most 32 and traversal is bounded.

## Provider configuration

OpenRouter Chat Completions is the only production adapter, at the fixed API
endpoint. There is no automatic model or endpoint fallback. Required `model` is
an explicit `organization/model` identifier; sample placeholders must be replaced.
Defaults: `max_tokens: 8192`, `timeout_seconds: 60`, `max_attempts: 2`,
`max_response_bytes: 262144`, `response_format: "json_schema"`, `zdr: true`,
`data_collection: "deny"`, `upstream: null`. See the reference schema for caps.

Routing always sets `allow_fallbacks: false` and `require_parameters: true`.
Optional `upstream` restricts routing to that single OpenRouter provider slug;
otherwise OpenRouter chooses the initial compatible provider. No fallback list is
sent. Retention flags express routing requests, not an account-wide retention
guarantee. The organizer controls those flags and must select a compatible model.

`json_schema` sends closed per-entrypoint and per-checker variants derived from the validated
input schemas. Common native strict providers require every object property to
be required. An initial review needing request variants with optional properties
fails **before the HTTP call**; the organizer must explicitly select
`response_format: "json_object"` for those declarations. The latter includes the
same full schema in review material and applies identical local validation.
There is no silent downgrade, response repair or repeated semantic prompting.
Final/disabled-follow-up schemas omit all unused request variants. Provider
support for particular schema keywords still varies; unsupported requests fail
as infrastructure, never fall back to another model or fabricate a judgment.

## Submission

Every bounded regular UTF-8 file in the submission tree is snapshotted and shown
to the judge. Files are inert data until a declared Python entrypoint is executed
inside Docker. `submission.json` requires `version: 1`, `title`, `claims`
(nonempty list of bounded strings), and `experiments` (possibly empty). It may
also declare `checks: [{check, inputs}]`, one payload per registered checker ID;
see [protected checks](checkers.md). All other fields are rejected.
Each experiment has exactly:

```json
{
  "id": "prefix",
  "source": "check.py",
  "purpose": "Check every prefix through N",
  "input_schema": {
    "type": "object",
    "properties": {"n": {"type": "integer", "minimum": 0, "maximum": 100000}},
    "required": ["n"],
    "additionalProperties": false
  },
  "initial_inputs": [{"n": 10}]
}
```

There are at most 16 declarations, with unique IDs. A declaration can have zero
initial inputs and be available for follow-up. `source` must be a snapshotted
`.py` file. Initial inputs and judge-selected inputs use the **same** validator.

The intentionally restricted JSON Schema input dialect supports:

- `object`: explicit `properties`, unique `required` subset and
  `additionalProperties: false`; at most 32 properties.
- `integer` / `number`: required finite `minimum` and `maximum`, within ±1e100;
  booleans do not count as integers/numbers; integer bounds must be integers.
- `string`: required `maxLength` (≤4096), optional `minLength` and string `enum`
  (1–64 unique values within the declared length range).
- `array`: required `items`, `maxItems` (≤128), optional `minItems`.
- `boolean` / `null`: only their `type` field.

The root must be an object. Schema depth is at most 8 and canonical schema/input
size at most 16 KiB. References (including local `$ref`), remote fetching,
conditionals, unions, regexes, defaults, coercion, extra keywords and additional
properties are rejected. Property names are bounded strings. The reference
JSON schemas describe structural shapes; executable validators additionally
check this dialect, limits, source references and semantic bindings.
