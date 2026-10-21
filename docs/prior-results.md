# Automatic references to accepted submissions

Solvers cite immutable submission IDs using existing Yukon identity and access:

```json
"prior_results": [{"submission_id": "SOURCE_SUBMISSION_ID"}]
```

The manifest accepts up to 16 unique references. There are no result or claim IDs,
owner registrations, copied artifacts, source allowlists, reference credentials or
per-reference actions. A prior lemma may be valid without a score. Prior scores
and checker witnesses cannot replace current scores/checks.

`GET /api/submissions/:id/accepted-results` returns discovery through existing
visibility checks. `llm-verifier inspect-results --discovery FILE|-` displays the
original manifest bytes and atomic verdict. Discovery is informational; it never
authorizes verifier intake. Read the [scientific policy](evidence-policy.md) before
using a prior claim: incidental prose is not an accepted result, and missing
original definitions must not inherit meanings from the current challenge.

## Consumer admission

References require the original consumer's versioned
`submissionAdmission: {"version": 1, "submissionReferencesEnabled": true}` policy,
carried in `YUKON_EVALUATION.admission`. Missing/null admission disables references.
The verifier refuses nonempty declarations with `submission_references_disabled`
before resolution and rechecks its immutable intake snapshot in later phases.
Empty authenticated resolution, receipts and accepted exports still work.

The existing owner PATCH updates admission separately for each Pareto track and
requires no reorg or verifier config change. Retries and reorgs retain the original
candidate policy; there is no live post-intake admission gate. Read
[submission admission](submission-admission.md) for the strict dialect, generated
manifest behavior, public API examples and new-candidate requirement after refusal.

## Immutable wire

There is one representation, `atomic-submission-v1`. The existing endpoint,
workflow input and artifact names remain. Earlier partial-claim bundles and
legacy dossiers are rejected; no migration or adaptation exists in the verifier.

The export has `version: 1`, original `policy_id`, `simulation: false`, `source`,
`judgment: {valid: true, reason}`, `ancestors: [{submission_id}]` and `sha256`.
The [schema](accepted-results.schema.json) records the exact fields. Source holds
verifier evaluation/intake/review/dossier provenance, `bindings`,
`submission_sha256` and `manifest: {text, size, sha256}`. **The UTF-8 bytes encoded
by `source.manifest.text` are the original `submission.json` bytes**, including
whitespace. Its parsed `claims` is the only accepted set; no judge rewrites it.
Other proof files remain in the original dossier. The reusable surface is limited
to unambiguous declared claims.

`source.submission_sha256` equals `bindings.submission`: the canonical digest of
the entire original candidate file snapshot, including the original manifest.
This is independent of policy, model and verifier implementation. It identifies
source content, not a result selector. Other evaluation IDs and digests provide
provenance and integrity. An attacker can recompute hashes; acceptance authority
comes from the protected workflow and platform retention, never candidate files.

The resolver response is exactly:

```text
{version: 1, snapshot_id, consumer,
 references: [{submission_id, bundle_sha256} OR
              {submission_id, unavailable: "rejected" | "review_required" | "dependency_unavailable"}],
 sources: [{submission_id, benchmark_id, challenge_id, evaluation_id,
            validity_result_id, verifier_evaluation_id, accepted: true,
            submitted_at, completed_at,
            bundle: {format: "atomic-submission-v1", sha256, bytes, text}}],
 ancestors: [{submission_id, accepted: true}]}
```

`consumer` retains submission/benchmark/challenge/evaluation/job IDs. Bundle text
is the original exported artifact, not regenerated JSON; outer hashes bind its
UTF-8 bytes. Intake validates exact requested sources, byte pins, original
manifest records, source identity and transitive acceptance attestations.
The two reference forms are a disjoint, exact partition of the original declared
IDs, with at most 16 entries in total. Only accepted entries have source bundles,
`prior:<submission_id>` evidence references and ancestry. Unavailable entries
provide a bounded status notice, no accepted theorem or claim that a theorem is
false: `rejected` is a completed rejection, `review_required` is completed work
awaiting required review, and `dependency_unavailable` means a completed source's
accepted support is blocked by its dependencies. Hidden/missing sources and
pending/failed execution are not mathematical unavailability notices.

The current judge may establish the entire original claim set from independent
current evidence and available accepted sources, or reject. It cannot rely on an
unavailable source's old acceptance or use its negation as a premise. All available
accepted declared sources conservatively contribute lineage; unavailable sources
contribute none. The original manifest retains every declared reference. Export
reconstructs accepted-only ancestry from the authenticated snapshot; it does not
treat every original declaration as proof dependence. An accepted source closure
cannot contain an ID the same snapshot marks unavailable.

Submission IDs govern ancestry. Yukon checks original immutable submission order,
self/forward/cyclic dependencies and visibility; judgment timestamps do not order
references. Python also rejects self/cyclic source ancestry. A later judgment of
an old submission must not turn its original dependency into a forward reference.

Bounds: 256 KiB per source bundle, 1 MiB aggregate imported bundle text, 2 MiB
resolver response, 16 sources and 128 ancestors. Full review prompts must fit the
configured prompt budget; evidence is never silently truncated. Oversize export
fails closed. The fixed 15-second credential-isolated worker bounds transport and
retries only the temporary dispatch/run-discovery race. Credentials do not enter
candidate execution, snapshots or model evidence.

## Retention and publication

No-reorg model/policy/verifier updates preserve retained mathematical acceptance.
Score-only reorg preserves the selected validity origin. Fresh validity reorg
reviews the same original declared set atomically. Successful reacceptance keeps
the submission reference usable; rejection makes the entire set unavailable.
Original policy/verifier identifiers in a supported atomic bundle are provenance,
not comparisons against today's judge.

Within a dependency-ordered Pareto reorg, sources included in that reorg use its
completed provisional judgments. Pending prerequisite executions wait; missing,
failed or cancelled execution remains infrastructure/batch failure. There is no
fallback to an old acceptance for a pending or newly rejected source. A completed
machine-valid result that still needs human review is unavailable support, not
automatically human-accepted. Sources outside the reorg use retained published
acceptance subject to the existing checks. This companion does not change the
native scalar lifecycle.

Intake freezes exact response and original source bytes. Later phases never
refetch or replace the evaluated claims. Finalization emits
`{version: 1, snapshot_id}`, including invalid judgments and empty reference sets.
Yukon rechecks access and immutable identity for every declared ID at publication,
and accepted support/closure for newly judged accepting validity. An unavailable
status need not stay negative for a consumer that proved its claims independently.
No new permission, owner flag or credential is involved.

A preserved/no-new-job consumer keeps its historical selected verdict and lineage.
It receives neither a new acceptance nor filtered dependencies. If that historical
closure is revoked, the consumer becomes unavailable for reuse just like an
untouched consumer outside the batch; its historical verdict does not establish
current reuse eligibility. Scores are not automatically rewritten and required
human review is not bypassed. Native transactions own ordering, provisional
selection, final support/access fences and coherent publication.

Only a verified non-simulation, valid full dossier can export a source bundle.
Finalization clears all stale outputs before work and removes them on failure.
Simulation cannot emit an official score, accepted bundle or publication receipt.
The installed-package cross-language check uses actual resolver responses produced
by local Yukon route tests; model and transport boundaries remain mocked offline.
