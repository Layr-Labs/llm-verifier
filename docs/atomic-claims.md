# Atomic acceptance of declared claims

The original `submission.json` manifest's `claims` is the only declared claim set.
`valid: true` accepts every claim exactly as written, including its actual
assumptions and scope. If any claim is unsupported, return `valid: false`.
The judge cannot accept a subset, weaken a statement or add conditions in `reason`
while reporting acceptance. A narrower lemma needs a separate submission.

Claims must state their material assumptions, scope, parameters and nonstandard
definitions. Supporting arguments belong in proof files. This is a mandatory
instruction in every initial/final and scored/unscored review, with precedence
over conflicting challenge prose or selected policies; no configuration is needed.
The existing [external-evidence rule](external-evidence.md) remains mandatory,
including its familiar-standard exception. Mathematical adequacy remains an LLM
assessment, not a guarantee supplied by the response schema.

The judgment has exactly `valid`, `scores`, `reason`, `evidence_refs` and
`review_standard`. Acceptance and rejection use that same shape. Reason explains
the decision or missing premise; known source/run references supply diagnostics.
There is no second accepted-claims list, assumptions list or unresolved-obligations
list that can contradict the verdict or redefine the original claims.

For bound objectives the model returns `scores: null`. Atomic acceptance, all
current score sources and every required canonical checker must permit the whole
tuple before deterministic projection. An unavailable tuple stays unscored only
when allowed; rejection yields no bound score records. Exact rational bounds and
check semantics remain binding. A complete positive-infinity endpoint retains its
exact records and null numeric projection even when `allow_unscored: false`.
See [score availability](authoring.md#score-availability).
