# Committee-reviewed standalone lemma

This example uses the shared verifier's math committee plus a challenge-owned
scope persona. The submission proves only the two-variable lemma; it earns no
full-target score. Committee configuration and persona files are organizer-owned.

`verifier.json` selects `judges/committee.json`, which imports the local catalog
`judges/personas.json` and adds its scope reviewer. The bundled reviewers, defender
and adjudicator remain provided by the installed verifier. All members inherit
the explicitly selected model from `provider`; the placeholder supports offline
simulation only.

```sh
llm-verifier check --root examples/committee
llm-verifier simulate --root examples/committee \
  --script examples/committee/simulation.json --out .work/committee.json
```

The script supplies a scope objection, defense and dismissal. It never calls a
model or publishes a score. See [judge configuration](../../docs/judges.md) for
custom profiles, shared budgets, live phase commands and semantic limits.
