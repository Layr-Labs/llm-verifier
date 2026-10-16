# Atomic lemma reuse

Submit the lemma example first. After platform acceptance, discover its original
manifest through `GET /api/submissions/:id/accepted-results`. Add
`"prior_results": [{"submission_id": "A_SUBMISSION_ID"}]` to the consumer manifest.
The downstream judge assesses the application under the original declared scope.
All declared claims must be established; incidental proof/reason prose supplies no
extra reusable result. These examples perform no registration or live call.
See [automatic references](../../docs/prior-results.md).
