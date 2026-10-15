# Approved-definition challenge scaffolding (0.3.0)

The generic `init`, `context` and `check` commands adapt the builder process from
`Layr-Labs/yukon-template@72a6b96b4b5385f199a5a29ecdd742aeb45ad55f`
(`.agents/skills/build-challenge/SKILL.md`, `AGENTS.md`, `docs/authoring.md`).
They implement the llm-verifier recipe, not stock AutoYukon initialization, the
Moonshot runtime, or its accept/revision/reject protocol. No model is selected
implicitly and no credentials are read during scaffolding or public conformance.

## Standalone controller

Start from [the complete toy collision definition](../examples/collision.definition.json).
It contains the protected checker bundle and target, rules, baseline submission,
an unscored lemma, scripted orchestration cases and real checker known answers.
The toy problem does not certify a full hash challenge or a mathematical theorem.
Replace its content and expected results with your approved challenge definition.
Preserve source authorship and provenance when importing a scientific baseline.

```sh
llm-verifier init --definition /controller/approved-definition.json \
  --output /workspace/new-challenge \
  --verifier-repository Layr-Labs/llm-verifier \
  --verifier-revision FULL_40_CHARACTER_REVIEWED_COMMIT \
  --model EXPLICIT/ACCESSIBLE_MODEL
```

Both workflow/source pins use the supplied immutable commit. `init` accepts only
a new directory and a new external controller receipt (default:
`/workspace/new-challenge.receipt.json`; override with `--receipt`). It preserves
all supplied UTF-8 file bytes, validates the config and scripted cases in a
staging directory, and produces:

- Rules, protected checker sources/data, baseline, proof and case files from the definition.
- `verifier.json`, with the explicit model and normalized bounded configuration.
- A schema-v2 one-track `benchmark.json` with manual promotion and only `submission` editable by participants.
- A pinned `.github/workflows/benchmark.yml` caller and separate public checker workflow.
- `.agents/skills/build-challenge/SKILL.md`, `AUTHORING.md`, `public-checks.json` and the public checker runner.
- `yukon-scaffold.json`, recording the definition digest, selected pins/model, file inventory and builder edit map.

The external receipt binds the definition, selected generator options and exact
inventory. Keep it on the trusted controller channel, outside the exported tree.
Hashes detect unauthorized changes relative to that receipt; they are not
signatures and do not authenticate a candidate-supplied receipt. Do not publish
private definitions, receipts, credentials or qualification cases by accident.
All data in the definition's `files` map is explicitly public export material.

## Definition v1

The closed top-level object has:

| Field | Meaning |
| --- | --- |
| `version` | Integer `1` |
| `name`, `description` | Stable ASCII ID and bounded track description |
| `verifier` | Existing protected [configuration](configuration.md), including explicit objective semantics, rules path and any checker registry/bindings |
| `files` | Object of root-relative safe paths to exact UTF-8 text; at most 128 files, 256 KiB per file, 4 MiB canonical map |
| `edit_paths` | Explicit builder-editable file paths from `files`; rules and generated files cannot be included |
| `conformance` | 1–32 independently labeled scripted cases described below |
| `checker_cases` | Optional 0–64 real protected-checker known-answer cases |

`files` must include the configured rules path, `submission/submission.json`,
all registered checker bundle files and all conformance case files. It cannot
replace generated files or insert content under `.github`, `.agents`, `.yukon`
or `tools`. Runtime candidate/checker/input limits may be lower than definition
limits and still apply. Config and submission wire versions remain `1`.

A scripted case is:

```json
{"id":"lemma","submission_path":"lemma","script_path":"lemma-simulation.json","expected":{"valid":true,"scores":null}}
```

The script uses the existing `initial_results`, `initial_decision`,
`followup_results`, `final_decision` simulation contract. Optional
`expected.score_records` asserts the exact derived semantic-record list in the
dossier. Simulation never executes a checker or calls a model. An expected
judgment is a test oracle for plumbing, not scientific acceptance.

A real checker case is:

```json
{"id":"pair","check":"collision","inputs":{"left":"m3","right":"m17"},"expected":{"passed":true,"target_bits":8,"digest_hex":"bd","total_bytes":5}}
```

Its declared inputs are validated against the registry. Its exact expected
output may include deliberately schema-invalid negative results. Public checker
CI fails explicitly if checkers are registered but no cases are provided. Select
positive, negative, boundary and malformed-input cases independently; successful
public known answers do not qualify a judge or establish mathematical truth.

## Managed builder and completion

The standalone controller supplies an approved definition, trusted receipt,
role/authority and finish point. A managed builder starts with:

```sh
llm-verifier context --root /workspace/new-challenge --task build \
  --definition /controller/approved-definition.json \
  --receipt /controller/new-challenge.receipt.json
llm-verifier check --root /workspace/new-challenge \
  --definition /controller/approved-definition.json \
  --receipt /controller/new-challenge.receipt.json
```

`context` verifies inventory and protected bytes, returning read/inspect/edit
paths. `check` additionally validates the candidate snapshot and scripted cases;
it returns observed hashes for all inventory files. Both managed `context` and
`check` enforce the complete export inventory: unknown files, empty directories,
workflows, instruction files, caches and symlinks are rejected. Only root `.git`
metadata (a regular pointer file or directory) is skipped without reading or
traversing it; a symlink `.git` is rejected. Keep authoring receipts, virtual
environments and evaluation outputs outside the export when running these
checks. Hosted evaluation may create `.yukon` artifacts later; those are not part
of a finalized authoring export. Neither command executes
candidate/checker code or calls a model. `--task validate` returns no edits.
Participant `--task solve` reads the configured rules path and permits only the
submission subtree, without access to private controller inputs.

Managed builders edit only the returned paths. They do not regenerate rules,
workflow, instructions or inventory. The controller reviews the returned edits,
synchronizes them into its approved external definition, and reruns `init` into
a fresh directory and receipt to finalize a new inventory. This explicit
regeneration replaces a separate `seal` command. Qualification is always
`not_performed`; regeneration does not imply scientific or hosted acceptance.

## Public checker CI and live integration

`python3 tools/public_conformance.py --root .` executes protected checker cases
using the configured pinned Docker image, direct JSON stdin and only the selected
bundle mounted at `/snapshot/files`. It has bounded output/time, no network,
nonroot/read-only execution and verified cleanup. Prepare the pinned image
explicitly before execution; the generated credential-free public workflow does
this. It needs no model secret, verifier repository checkout or reusable-workflow
access. It is a public test runner, not a replacement for production evaluation.

The separate benchmark caller needs reusable-workflow access from the verifier
repository and, where necessary, `VERIFIER_CHECKOUT_TOKEN` for read-only source
checkout. `OPENROUTER_API_KEY` belongs only in authorized review jobs/processes.
Scaffolding does not change settings or arrange credentials. A generated workflow
is not proof of access, hosted execution, import, publication or qualification.
Use the [separate phases](../README.md#independent-phases) for local dogfooding;
local `yukon run` still does not orchestrate them.
