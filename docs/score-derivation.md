# Retained-result score derivation

`score-derivation-v1` is an opt-in representation review of the **same submission's authenticated retained full claims**. It produces a structured payload for a protected challenge adapter. It never produces a new scientific validity verdict, an accepted-results bundle, or a human approval. Ordinary initial/final review, score-only validity gates, and other-submission accepted-result reuse remain unchanged.

Use this mode when a new scoring representation can be extracted from the full declaration that was already reviewed. A stronger representation than an old lossy numeric projection can be legitimate; a stronger scientific claim than the accepted declaration is outside this mode. Original `atomic-claims-v1` and `exploratory-v1` qualifications remain distinct. The model receives all original declarations and source files, with their original policy, and checks representation support rather than re-proving the submission.

## Dispatch and selection

The destination scoring policy explicitly contains `"retained_result":"yukon-retained-result-v1"`. On an eligible historical score-only job, Yukon supplies `evaluation.retained_result`. The fields must be exactly `{"scores":true,"validity":false}`. The ordinary verifier parser deliberately rejects this additional descriptor; protected caller routing must choose the derivation workflow explicitly.

The closed retained descriptor is:

```typescript
type Evidence = {
  result_id: string; evaluation_id: string; policy_version_id: string;
  source_ref: string; job_id: string; commit_sha: string;
};
type RetainedResult = {
  version: "yukon-retained-result-v1";
  benchmark_id: string; submission_id: string;
  candidate_key: string; packet_id: string;
  score: Evidence;
  validity: Evidence & {valid: boolean};
  destination: {
    job_id: string; evaluation_id: string; packet_id: string; source_ref: string;
  };
};
```

IDs are lowercase hyphenated UUIDs. Candidate keys are `digest:` or `archive:` followed by 64 lowercase hexadecimal characters; this platform identity is **not** the verifier's file-snapshot digest. Source refs and commit strings are nonempty Unicode scalar strings of at most 1024 UTF-16 code units, preserved exactly without trimming. Lone surrogates are rejected. Historical GitHub transport additionally requires that the actual run head equal the retained validity's commit.

Yukon authenticates the original selected packet and separate score/validity evidence, binds the destination, and fences stale selections at dispatch, completion, and publication. A protected organizer plan maps that selection to original artifact bytes. Neither a candidate-supplied descriptor nor a self-computed hash authenticates a result. The final challenge score output echoes the complete issued descriptor as top-level `derived_from`, beside `scores` and optional `metrics`, without `valid`.

Baselines and submissions needing new validity do not have this descriptor. The caller may route an explicitly requested ordinary both-fields evaluation to the ordinary workflow. Missing identity on an expected derivation, stale identity, missing artifacts, or unsupported input never triggers an automatic paid validity review.

## Protected configuration and plan

Keep `derivation.json` and its plan outside the candidate subtree. They are part of the protected organizer source, not candidate input. For example:

```json
{
  "version": 1,
  "mode": "score-derivation-v1",
  "id": "coefficient-transcription",
  "plan_path": "derivation-plan.json",
  "instructions": "Extract the exact coefficient from the retained full declaration. Do not infer an unstated rate or constant.",
  "payload_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["coefficient"],
    "properties": {"coefficient": {"type": "string", "maxLength": 64}}
  },
  "provider": {
    "model": "SELECT_OPENAI_MODEL",
    "max_completion_tokens": 1024,
    "max_attempts": 1,
    "timeout_seconds": 60
  },
  "limits": {
    "total_review_tokens": 1024,
    "total_provider_requests": 1,
    "total_review_seconds": 60
  }
}
```

Choose an explicit available model before real review. Existing provider settings, including reasoning effort and background requests, are supported. This mode uses one reviewer and its bounded retries. It has no planner, committee, automatic escalation, experiments, checker execution, or tools. The total token budget reserves `max_completion_tokens` for each attempted generation; it does not refund cheap responses. Malformed output is a failure, not an automatic repair call.

Optional limits are `max_files`, `max_file_bytes`, `max_submission_bytes`, `max_prompt_bytes`, `total_review_seconds`, `total_provider_requests`, and `total_review_tokens`. Defaults are 32 files, 256 KiB per file, 1 MiB per submission, 2 MiB prompt, 180 seconds, three requests, and 32768 reserved completion tokens. Existing verifier hard limits apply.

The bounded payload-schema dialect requires closed objects with every property required, finite arrays (at most 128 items), bounded strings (at most 16384 characters), bounded numeric values, booleans, null, scalar constants/enums, `anyOf`/`oneOf`, and acyclic local `#/$defs/NAME` references. The expanded schema is limited to 128 KiB, 4096 nodes and depth 16; payloads are limited to 64 KiB. Remote or recursive references are rejected. Only the documented exact rational/digest patterns in `derivation_schema.PATTERNS` are accepted, avoiding arbitrary regex execution. Root payloads are objects. Hoist `$defs` to the document root when embedding a domain schema. `oneOf` is sent to the provider as `anyOf` and its exclusivity is checked locally. Domain meaning and arithmetic invariants remain the protected adapter's responsibility.

The plan is:

```text
{
  version: "score-derivation-plan-v1",
  config_sha256: RAW_SHA256_OF_DERIVATION_JSON,
  entries: [{
    origin: RETAINED_RESULT_WITHOUT_DESTINATION,
    action: "derive" | "not_applicable",
    reason: ORGANIZER_EXPLANATION,
    source_files_sha256: CANONICAL_VERIFIER_FILE_SNAPSHOT_DIGEST,
    artifacts: {intake: REF, accepted_results: REF} | null
  }]
}
```

Each `REF` has exactly `repository`, `run_id`, `run_attempt`, `artifact_id`, `artifact_name`, `archive_sha256`, `path`, and `file_sha256`. IDs/attempts are positive decimal strings; digests are lowercase SHA256. The archive hash covers raw ZIP bytes and file hash covers raw file bytes. `path` is the exact safe file path inside the ZIP. The plan digest in an intake also covers **raw file bytes**, while the entry, normalized configuration, and file snapshot use the verifier's canonical JSON digest. There is one entry per benchmark/submission; the complete original descriptor must match, not only the candidate key.

Source artifacts belong to the retained **validity** evidence. A later score job can have a different head or produce only a derivation; that does not make it the original accepted-claim source. The downloader checks the pinned successful run/attempt, actual head, artifact ID/name/run, and ZIP/file digests. There is no lookup of the latest artifact and no execution of old code or wheels. Metadata or network failures stop the run. Downloads have bounded sizes/time and no model fallback.

For retained `valid:false`, action must be `not_applicable`, artifacts must be null, and no provider is constructed. For retained `valid:true`, original intake and accepted-results are required even for a deliberately unscored `not_applicable` entry. The source reader validates the accepted bundle, exact original manifest and every file hash, intake envelope, original policy and source bindings, and equality of old/current candidate file snapshots. It does not call current `verify_intake` on old implementation bytes or reinterpret an old review. Historical preprocessing that changed the parsed manifest is explicitly unsupported by this first import format.

## API and output

```python
from llm_verifier.derivation import prepare, review, verify_derivation, source_material

intake = prepare(root, "derivation.json", "submission", evaluation,
                 context, archive_root)
result = review(intake)
data = verify_derivation(result, expected_intake=intake["sha256"])
original = source_material(data["intake"])
```

`original` has `intake`, `accepted_results`, and `manifest`; all are null for retained-invalid records. The original file records preserve raw UTF-8 bytes. The current intake uses stage `score-derivation-intake`; the result uses stage `score-derivation` with `data={intake,decision,provenance}`. The decision is:

```text
{status: "derived" | "unsupported" | "not_applicable",
 payload: ORGANIZER_SCHEMA_OBJECT | null,
 reason: TEXT,
 source_refs: [{source, sha256, start_line, end_line, quote}]}
```

Only `derived` carries a payload. It must include a claim citation. `claim:N` cites the string claim or an object claim's exact `statement` string; `file:PATH` cites the exact original file text. SHA256 covers full UTF-8 source text. Lines are 1-based, inclusive, split only at LF; joining a selected slice preserves CR. The quote must equal that slice exactly. Files can clarify an accepted declaration but cannot silently enlarge it. Citation validation checks provenance, not mathematical entailment.

A protected adapter checks its own domain invariants, projects numeric scores, and emits truthful original plus derivation provenance. It must validate `simulation` and current hosted artifact/run identities before publication. `unsupported` is an explicit representation outcome: an organizer policy may retain an old projection or withhold scores. Infrastructure, authentication, schema, or provider failure is not `unsupported` and must not be silently converted into one.

## Hosted execution and receipt-only capture

Call the separate pinned `.github/workflows/derive-score.yml` from a protected caller. Inputs mirror the ordinary workflow's repository/revision/root/config/submission/score-path/environment fields, with required `yukon_evaluation` and optional `yukon_accepted_results`. Outputs are `helper_base64`, `helper_sha256`, `package_artifact`, `intake_artifact`, `intake_sha256`, `derivation_artifact`, `review_run_id`, `review_run_attempt`, and optional `receipt_artifact`.

The prepare job verifies current checkouts, builds the pinned wheel once, fetches the protected historical ZIPs, and seals the intake. The review job receives only verified current-run artifacts and the model key; it has no checkout, original-artifact credential, OIDC authority, or candidate execution step. Artifact names are `llmv-derivation-package`, `llmv-derivation-intake`, and `verifier-score-derivation` (containing `derivation.json`). The challenge owns final score projection/delivery; this shared workflow does not write a Yukon score.

If the caller declares Yukon's existing accepted-results context, prepare performs authenticated `resolve([])` solely to bind the current run. The consumer must equal the retained descriptor's benchmark/submission and **destination** job/evaluation. The sealed snapshot must contain no references, sources, or ancestors. It emits only `accepted-results-receipt.json`, with `{"version":1,"snapshot_id":"..."}`, in artifact `verifier-accepted-results`. There is no `accepted-results.json`, new anchor, or new accepted scientific claim. This preserves existing capture/run-binding hooks without changing self-import rules.

CLI commands are `derivation-fetch`, `derivation-prepare`, `derivation-workflow-prepare`, `derive-score`, `derivation-check`, and `simulate-derivation`. `derivation-check --require-publishable` rejects simulation and local/unasserted contexts; the domain adapter still checks actual hosted delivery identities.

## Offline harness

`derivation-fetch` is the only historical network download command. To test offline, provide already verified ZIPs named `ARTIFACT_ID.zip` under `--archives`, prepare the intake, then use `simulate-derivation --intake intake.json --script decision.json --out simulation.json`. It uses the real source/schema/citation validation but returns an explicitly unpublishable simulation wrapper. It cannot export an accepted result or official score. Injected providers are always simulation, even if their metadata claims to be OpenAI.

Run the generic source, transport, receipt, hosted and failure cases without Docker, credentials, or paid calls:

```sh
PYTHONPATH=src python3 -m unittest tests.test_derivation tests.test_derivation_boundaries
```

Fixtures construct synthetic original accepted artifacts at the real HTTP test boundary. They cover changed candidate files and selections, distinct original score/validity origins, original policy preservation, missing context, null receipts, exact citations, bounded schema unions, transport hashes/redirects, failure diagnostics, reserved budgets, simulation, and ordinary baseline policy compatibility. The full ordinary verifier suite remains required.
