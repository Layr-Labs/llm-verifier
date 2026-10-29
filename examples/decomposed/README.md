# Offline decomposed-review example

This selects `builtin:math-decomposed-v1` for the same scoped lemma as the proof
example. The scripted planner covers both source files, and the region worker
returns a separate coverage report for each assignment. A region raises a blocking
finding, the defender responds from the unchanged evidence, and the adjudicator
dismisses it. These scripted outputs exercise plumbing, not model accuracy.

From the repository root after installation:

```sh
llm-verifier check --root examples/decomposed
llm-verifier simulate --root examples/decomposed --script examples/decomposed/simulation.json --out /tmp/decomposed-simulation.json
```

Inspect `region_plan`, `region_coverage`, and the per-region provenance in the
simulation dossier. The plan uses one-based inclusive source lines. Changing the
source requires updating the scripted plan. Missing, reordered or extra worker
reports fail validation. No provider credentials, network retrieval or candidate
code execution are used by this example, and simulations cannot publish a score.
