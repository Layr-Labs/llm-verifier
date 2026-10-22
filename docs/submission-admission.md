# Submission admission

Accepted-submission references are opt-in for each consuming Pareto benchmark
(track). The verifier owns this versioned policy inside Yukon's existing public
`submissionAdmission` JSON. It is not part of pinned `verifier.json`, and no
separate Yukon feature switch is required.

```json
{"version": 1, "submissionReferencesEnabled": true}
```

Only those two fields are accepted. `version` must be integer `1` (not `true`,
`1.0` or a string) and `submissionReferencesEnabled` must be a JSON boolean.
Unknown versions, fields or types, including `{}`, fail explicitly with
`unsupported_admission_policy`, even for reference-free work. See the
[structural schema](submission-admission.schema.json); executable validation also
checks the integer representation.

Missing/null admission and explicit version-1 `false` disable references. Normal
reference-free baseline/evaluation work remains allowed. A nonempty original
`prior_results` declaration while disabled fails with
`submission_references_disabled` before any reference resolution, experiment or
model review. This is an admission/configuration refusal (CLI exit 2), not a
mathematical `valid: false`, an unavailable reference or evidence of falsity.
Authenticated empty resolution still runs and yields the publication receipt;
valid accepted exports and invalid-judgment receipts remain supported.

## Initial authoring and live updates

Generated `benchmark.json` tracks start with:

```json
"submissionAdmission": {"version": 1, "submissionReferencesEnabled": false}
```

`llm-verifier github` preserves and validates an existing single track's
`submissionAdmission`, including explicit `null`; only a new/missing field gets
the initial false default. `check` validates a non-null admission's structure but
does not enforce reference consumption or contact the resolver. A reference-using
submission can therefore be authored before admission is enabled. Neither command
uses local benchmark settings as authorization for intake.

For an imported Pareto benchmark, its owner can update the existing API without
changing the verifier pin or starting a reorg:

```text
PATCH /api/benchmarks/CONSUMER_BENCHMARK_ID
Authorization: Bearer OWNER_ACCOUNT_API_KEY
Content-Type: application/json
```

```json
{"submissionAdmission": {"version": 1, "submissionReferencesEnabled": true}}
```

Use `false` to disable future reference-using candidates. A `null` field resets
future admission to the disabled default; omitting `submissionAdmission` from
PATCH leaves it unchanged. The API accepts generic JSON; the verifier enforces
this dialect. Ownership of the benchmark is required. Grouped challenge tracks
have separate benchmark IDs and are patched independently; this is not a shared
challenge setting. Public benchmark detail exposes the policy. Reimporting a
manifest does not replace an already imported benchmark's live admission value.
See the [consumer update example](../examples/prior-results/consumer/admission-update.json).

## Original candidate policy and phase snapshots

Yukon snapshots admission on the original candidate and passes it as
`YUKON_EVALUATION.admission` with the selected scoring/validity policies and fields.
Native transport omits admission for a null/missing stored policy. The verifier
preserves the evaluation it receives in `effective.evaluation` and intake bindings;
it does not invent a null field or turn an omitted/null value into an enabled one.
Explicit null is also accepted at local intake and retained as supplied.

Admission is checked before a nonempty resolver call and again in `verify_intake`
for every consumed phase envelope. All later phases use the original intake
snapshot. They never reread current environment variables, local benchmark JSON,
or a live challenge setting to authorize references. A supplied resolver response
cannot override disabled admission.

Changing live admission affects new candidates only. An originally enabled
candidate remains enabled across retries or rejudging after a live disable; an
originally disabled candidate remains disabled after a live enable. Score-only
and validity-only reorgs retain the original admission too. A refused candidate
must be submitted as a new candidate after enabling the policy; a retry/reorg does
not upgrade it. Old missing-policy records stay disabled—there is no migration or
compatibility exception. There is no live post-intake admission gate.

Local intake/simulation receives the same evaluation through `--evaluation` or
`YUKON_EVALUATION`; it does not read `benchmark.json` as a policy fallback. The
[consumer evaluation example](../examples/prior-results/consumer/evaluation.json)
shows an explicit enabled evaluation. Enabled references still require trusted
platform resolution; simulation cannot publish official scores, exports or
receipts. Local `yukon run` orchestration remains unsupported.

## Evidence and access

Admission only permits a consumer to request references. Existing source access,
original submission ordering and current acceptance checks still apply. A private
or denied source can return `prior_forbidden` independently of admission refusal;
PR13 unavailable support is likewise a separate mathematical-evidence condition.
A source benchmark need not enable reference consumption to export accepted work.

Original manifest claims remain the entire atomic claim set. Successful reapproval
keeps immutable submission IDs; unavailable declarations retain their original
bytes, while only accepted support enters export ancestry. Admission does not
supply a current score or checker pass, alter those semantics, or create a theorem
catalog.
