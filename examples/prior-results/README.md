# Atomic lemma reuse

Submit the lemma example first. After platform acceptance, discover its original
manifest through `GET /api/submissions/:id/accepted-results`. Add
`"prior_results": [{"submission_id": "A_SUBMISSION_ID"}]` to the consumer manifest.
The downstream judge assesses the application under the original declared scope.
All declared claims must be established; incidental proof/reason prose supplies no
extra reusable result. These examples perform no registration or live call.
See [automatic references](../../docs/prior-results.md).

References are disabled unless the original consumer candidate has explicit
version-1 enabled admission. For an imported consumer Pareto track, its owner sends
[consumer/admission-update.json](consumer/admission-update.json) to the existing
`PATCH /api/benchmarks/:id` API before creating a new candidate. Grouped tracks
are updated independently. Missing/null means disabled; retries and reorgs cannot
change a candidate's original policy. This example performs no API call.

[consumer/evaluation.json](consumer/evaluation.json) shows the local intake
evaluation carrying the same admission (still requiring authenticated platform
resolution). The consumer here is a local unscored example with zero objectives;
a hosted Yukon benchmark additionally needs its required objective. Do not add an
admission flag to `verifier.json`. See
[submission admission](../../docs/submission-admission.md).
