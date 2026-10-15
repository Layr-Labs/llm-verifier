# Protected checkers and required numeric scores (0.2.0)

An organizer can register bounded Python checks under the protected challenge
root. A solver selects initial checks by ID and JSON data; the judge can select
registered checks in its existing single follow-up batch. Every declared check
runs initially. This adds no phase, model loop, dependency or external service.
Configurations and submissions without these optional fields keep their prior
behavior. Config/submission wire versions remain `1`; package version is `0.2.0`.
Artifacts are bound to every installed verifier source file and package version,
so artifacts from 0.1 or a different source revision must restart at intake.

## Registry and declarations

The optional organizer `checkers` array contains at most 16 unique IDs. Each entry
has exactly these fields (see `examples/collision/verifier.json`):

| Field | Contract |
| --- | --- |
| `id`, `description` | Stable ASCII identifier and bounded protected description |
| `source` | Root-relative `.py` entrypoint included in `files` |
| `files` | Explicit list of distinct protected root-relative source/data files |
| `input_schema` | Bounded closed object in the existing restricted JSON Schema dialect |
| `output_schema` | Same dialect, closed object; required checks must require `passed` with `type: boolean` |
| `required_for_score` | Literal boolean: whether this check gates every non-null score tuple |

Source and target parameters must be organizer-owned, outside candidate editable
paths. All files use the existing no-follow, hardlink/special-file rejection,
bounded UTF-8 reader. Python encoding cookies must specify UTF-8. No directory
walk of the protected root occurs. The union of declared registry files is bounded
by `max_files`, `max_file_bytes` and `max_submission_bytes` **once across the whole
registry**, independently of the candidate snapshot's same caps. Sharing one
protected file between checkers stores it once. Do not declare credentials,
`globalenv`, `hostfile`, or unrelated files in a bundle. No such file is read
implicitly; the organizer's explicit file list is the authority.

A submission may add:

```json
"checks": [{"check": "collision", "inputs": {"left": "m3", "right": "m17"}}]
```

There is at most **one canonical declared input per checker ID**, including
optional checkers. Duplicates are rejected, not selected by order. Unknown IDs,
source overrides, commands and extra fields are rejected. Inputs are inert JSON
objects validated before execution, with canonical size at most 16 KiB and the
existing depth, string, numeric and collection limits. They are not path lookups,
code, environment references, target overrides or dynamic imports. A trusted
checker should use fixed protected target files and treat any input text as data.
Optional tools may return any bounded closed object matching their output schema;
they need not define `passed`.

Initial ordering is candidate experiments in declaration order, followed by
checks in declaration order. All declared inputs must fit aggregate budgets;
there is no silent omission, prioritization or refund of failed run slots.

The judge's follow-up request is:

```json
{"check":"collision","inputs":{"left":"m3","right":"m17"},"reason":"Retry the same declared witness after a timeout."}
```

Candidate requests still use `{experiment, inputs, reason}`. The keys are mutually
exclusive; identical ID spellings in the two namespaces cannot substitute for
each other. A candidate file named like a protected entrypoint is still candidate
source, never the organizer source. Follow-ups can use registered optional or
undeclared checkers and different inputs, but those are scoped evidence only.
`json_schema` uses separate closed variants for each checker/experiment. Optional
input properties require explicit `json_object` when the initial review exposes
those variants, just as for existing experiments. No silent schema downgrade
occurs. Final and follow-up-disabled schemas contain no unused variants.

## Execution, binding and gate

Each check uses a fresh invocation of the unchanged DockerRunner. Its mount
contains **only that checker's selected protected bundle**, with the fixed
entrypoint and `/snapshot/files/<root-relative path>` layout. It never contains
candidate files, another checker's unrelated files, model-generated source, host
secrets, or a Docker socket. Standard library imports are available; Python `-I`
does not automatically import sibling files. An organizer can explicitly load
its own declared helper at a fixed snapshot path; candidate dynamic imports are
not available. The existing no-network, nonroot, read-only, bounded tmpfs and
resource policies and verified container cleanup apply.

Candidate and checker runs share initial/follow-up slot ceilings and reserved
whole-evaluation time/output limits. Initial runtime preparation follows the actual initial plan: a no-run lemma
needs no Docker even with a registry. A declared checker-only plan prepares the
pinned image. The conditional follow-up job uses `prepare-runtime --phase
followup` on its fresh worker, including for judge-only registered checkers.
Execution uses the installed image with `--pull=never`; only the explicit
`prepare-runtime` setup command pulls. Setup is credential-free in the workflow.

Effective configuration includes the exact checker file snapshots. Its intake
hash binds registry metadata, schemas, source bundles, targets, rules and policy.
Each run also records its namespace/requester, descriptor hash, whole selected
bundle hash, entrypoint hash, canonical input hash, config hash, and phase ID.
Plans, sources, typed inputs, raw stdout/stderr, parsed results and derived schema
validation are checked again whenever evidence is consumed. Source edits after
intake do not affect its immutable snapshot. Hashes provide integrity on the
trusted organizer artifact channel, not signatures: candidate-uploaded or fully
attacker-recomputed reports are never authoritative evidence.

For **every** checker marked `required_for_score`, numeric scores require:

1. A single submission-declared canonical input payload.
2. A completed zero-exit run of that registered protected checker for exactly
   that payload, under the current intake and validated phase chain.
3. Output matching the checker's schema with literal boolean `passed: true`.

Canonical JSON binds types: `1`, `1.0` and `true` cannot replace each other.
Different-input passes, judge-only undeclared payloads, candidate stdout, missing
runs, false/malformed output, numeric `passed: 1`, timeout, output overflow,
runtime errors, stale/replayed/tampered artifacts and budget omission never
satisfy this gate. A same-input follow-up retry may satisfy it. A successful
canonical initial pass remains valid even if an unrelated diagnostic fails.
A missing/failed required check does not force a lemma to supply witness data:
`scores: null` remains allowed under existing `allow_unscored` validity rules.

The judge receives protected descriptors/schemas, a derived `score_gate` containing
only IDs, hashes and static status categories, and instructions to return allowed
outputs. Raw checker output—including echoed hostile input—stays in the user
message as evidence, never system instructions. All candidate files and captured
run evidence remain in that message without truncation. Protected checker source
bytes remain in intake/dossier for audit; they are **not sent to the model**.

Schema-invalid checker results retain their exact raw/parsed output and a derived
`check_validation` (`valid`, `invalid_output` or `execution_failed`); they do not
abort execution or suppress unscored review. Review artifact validation and
finalization independently enforce the gate before Yukon field projection. A
non-null tuple is forbidden even in `valid: false` judgments or validity-only
projections. Model output is never rewritten and scores are never invented.
The dossier records the derived gate; Yukon output fields are unchanged.

## What a pass does not establish

The gate permits scoring; it does **not** compute, validate or authorize arbitrary
LLM objective values. Protected challenge rules must bind each objective to the
checked canonical witness and explain permitted units/interpretation. The judge
must review that relationship. Objective equality is not a generic deterministic
feature of this registry. Probability, expected cost, algorithmic complexity,
optimality and claims beyond the checked predicate require separate evidence or
proof. A lying/mistaken protected checker is outside this integrity guarantee;
organizers must review their checker implementation and target selection.

The collision example is explicitly a **toy eight-bit truncation of SHA-256**.
It verifies only a concrete pair and its total UTF-8 bytes. It does not implement
full HashSmash, admission or its reviewer committee. Every non-null Yukon
admission policy remains unsupported. Local `yukon run` remains unsupported;
use the separated phase CLI or existing six-job hosted orchestration.

## Offline example

```sh
llm-verifier simulate --root examples/collision \
  --script examples/collision/simulation.json --out .work/collision.json
llm-verifier simulate --root examples/collision --submission lemma \
  --script examples/collision/unscored-simulation.json --out .work/collision-lemma.json
```

The first script declares `m3`/`m17` (toy digest `bd`, total size 5), then requests a
failing identical-message diagnostic in follow-up. Its original canonical pair
still satisfies the score gate. The second supplies a pigeonhole existence lemma
without a concrete witness and returns null scores. Simulation uses scripted
execution/provider results and is visibly non-publishable; it does not execute a
checker or call a model. The gated Docker tests execute this exact checker and
verify isolation, actual outputs, timeout and malformed-result behavior.

For real credential-free checker execution with the pinned image already present:

```sh
llm-verifier intake --root examples/collision --out .work/collision-intake.json
llm-verifier experiments --intake .work/collision-intake.json --out .work/collision-initial.json
```

No model call is needed for those phases. The complete phase sequence is in the
README; do not run review commands without separately authorizing model use.
