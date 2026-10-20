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
| `objectives` | 0–8 ordered objects: `name`, `direction` (`+`/`-`), `unit`, `semantics`; optional `score_source`, inclusive `score_minimum` / `score_maximum` |
| `allow_unscored` | Whether an accepted entire declared set can remain unscored |
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
| Total review seconds / generation attempts across both reviews | 180 / 4 | 2400 / 6 |

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

Direct OpenAI Chat Completions is the only production adapter, at the fixed
`https://api.openai.com/v1/chat/completions` endpoint. The review process reads
`OPENAI_API_KEY` from its environment; no key or endpoint override is accepted in
configuration or CLI arguments. Required `model` is an explicit native OpenAI
model ID, without an `openai/` prefix. No model is chosen by default. Select and
qualify a model with access to Chat Completions, `max_completion_tokens`, and the
chosen response format. A dated model ID can pin the organizer's selected snapshot;
configured and returned IDs are recorded separately because aliases may resolve to
snapshots. Native fine-tuned IDs with colons are accepted syntactically, but their
schema capabilities still need qualification. `auto`, `default`, lists and
provider-prefixed IDs are rejected. The examples' `SELECT_OPENAI_MODEL` placeholder
is accepted for offline validation/simulation only; `init` and production review
reject it before a network call.

Defaults: `max_completion_tokens: 8192`, `timeout_seconds: 60`, `max_attempts: 2`,
`max_response_bytes: 262144`, `response_format: "json_schema"`. The completion token
cap includes visible output and reasoning tokens; it is not an input-token cap.

A single HTTP attempt may have at most 1200 seconds. Each review phase has a
separate hard wall bound of 1200 seconds across its attempts, even if the
configured aggregate budget is larger. Initial and final reviews together may
use at most 2400 seconds; the defaults remain unchanged. The killable worker
continues to bound DNS, TLS, writes, headers and body reads. For large proofs at
high reasoning effort, organizers should qualify a sufficient explicit timeout
(for example 1200 seconds with one attempt and a 2400-second aggregate budget).
Reasoning models can take several minutes; see the
[OpenAI long-running request guidance](https://developers.openai.com/api/docs/guides/background).
The default mode retains its fixed Chat Completions endpoint and request semantics;
background mode is an explicit organizer opt-in described below.

Exhausted retries report the last safe failure category: `provider_timeout`,
`provider_transport_failed`, `provider_rate_limited`, or `provider_server_error`.
A response-size violation reports `provider_response_limit` without retry.
`provider_attempt_budget` means no attempt was available, rather than hiding
an actual transport timeout. Provider error bodies, headers, credentials and
arbitrary exception text are never included in these diagnostics.

Optional `provider.reasoning_effort` accepts exactly `"none"`, `"minimal"`,
`"low"`, `"medium"`, `"high"`, `"xhigh"` or `"max"`; null and non-string values
are rejected. In the default mode it is sent unchanged as the top-level Chat Completions
`reasoning_effort` field and bound by the existing provider-config hash. When
omitted, no field or local default is added to the normalized config or request,
preserving their previous bytes. Supported efforts depend on the selected model;
qualify that model/effort pair. Unsupported requests fail as infrastructure with
no effort fallback or downgrade. [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)
and [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) support
`low`, `medium`, `high`, `xhigh` and `max`, but not `none` or `minimal`.
Omission does not guarantee high effort. The starter sets no effort; `init` writes
CircuitBreaker's reviewer settings only for `gpt-6-astra`, as the
[authoring guide](authoring.md#what-init-fills-in-and-what-you-fill-in) describes.

See the [OpenAI request reference](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)
and the local reference schema for bounds. Default-mode requests send `n: 1`,
`stream: false`, and `store: false`. It sends no tools, sampling overrides, routing
policy or attribution headers. `store: false` disables stored completions; it does
not establish an account-level zero-retention guarantee or disable abuse monitoring.
Organizers manage those controls with OpenAI separately; see
[OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data).

To migrate an older OpenRouter configuration, select the intended OpenAI model's
native ID, rename `max_tokens` to `max_completion_tokens`, and remove `upstream`,
`zdr` and `data_collection`. Those old fields are rejected, never silently ignored.
Provision `OPENAI_API_KEY` in the caller and refresh both verifier pins and the
caller workflow with `llm-verifier github`. Old credentials are not reused. Restart
intake under the new implementation; existing phase artifacts are source-bound.

`json_schema` sends closed per-entrypoint and per-checker variants derived from the validated
input schemas. [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
requires every object property to be required and every object to be closed. An
initial review needing request variants with optional properties
fails **before the HTTP call**; the organizer must explicitly select
`response_format: "json_object"` for those declarations. The latter includes the
same full schema in review material and applies identical local validation.
There is no silent downgrade, response repair or repeated semantic prompting.
Final/disabled-follow-up schemas omit all unused request variants. Model
support for particular schema keywords still varies; unsupported requests fail
as infrastructure, never fall back to another model or fabricate a judgment.

## Submission

Every bounded regular UTF-8 file in the submission tree is snapshotted and shown
to the judge. Files are inert data until a declared Python entrypoint is executed
inside Docker. `submission.json` requires `version: 1`, `title`, `claims`
(nonempty list of bounded strings or [structured scored claims](authoring.md#score-sources)),
and `experiments` (possibly empty). It may
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


Malformed HTTP 200 responses report a fixed parser category after
`provider_response_invalid_`: envelope JSON, envelope, choices, unfinished,
output token limit, message, decision JSON, metadata, or usage (underscores
replace spaces). These categories reveal neither response text nor credentials.
In particular, `provider_response_invalid_output_token_limit` means the single
choice ended with `finish_reason: "length"`. It is an infrastructure failure;
no partial decision is accepted and no semantic repair retry is performed.


An organizer may explicitly set provider.background=true to use the fixed
OpenAI Responses endpoint for a long reasoning review. The adapter preserves the
same messages, model/effort, output-token allowance, JSON schema and store=false.
Omission preserves the historical Chat Completions request bytes and endpoint.
The endpoint in sealed provenance must exactly match the selected mode.

Background mode creates one generation, polls only that validated response ID,
and accepts only a completed response with one completed assistant text message.
Refusals, tool output, failure, cancellation, incomplete output and invalid
usage fail without semantic repair. Reasoning items/summaries are not retained.
The normalized decision still passes all existing judgment/score validation.
Per-response bytes remain bounded by max_response_bytes. The killable worker
retains its total deadline; individual HTTP calls have at most 30 seconds.

The generation-attempt budget counts POSTs that start reviews. Each attempt adds
at most 120 GET polls, spaced by 10 seconds, and at most one cancellation plus one
DELETE (123 HTTP calls total including generation). Poll failures retry the same
generation, never trigger a fresh model judgment. Ten seconds of the attempt wall
budget are reserved for best-effort cancellation/deletion; the parent still
kills a stuck worker at the hard deadline. Cleanup failure cannot change an
already obtained judgment. No provider credential or raw error text is logged.

Current OpenAI background documentation allows store=false, while retaining
temporary response state for asynchronous execution/polling (roughly ten minutes).
This differs from a foreground request with no asynchronous state. Organizers
must select this mode explicitly and apply their account-level data controls:
https://developers.openai.com/api/docs/guides/background
