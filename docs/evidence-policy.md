# Atomic claims and scientific evidence

Every initial/final and scored/unscored review uses the mandatory
`atomic-claims-v1` policy. Configuration may omit the field (it defaults to this
policy); another current policy value is rejected. Challenge prose cannot relax it.

`submission.json`'s `claims` is the single authoritative claim set. `valid: true`
accepts every declared claim exactly as written. If one claim is not established,
the entire set is rejected. A judge cannot accept a subset, add conditions, weaken
a statement or rewrite its scope in `reason` while accepting the submission.
A useful narrower lemma can be a separate submission and receive separate credit.
Incidental claims, conjectures and findings in proof or reason prose are not
separately accepted or reusable.

Declared claims must state their material assumptions, scope, parameters and
nonstandard definitions needed for interpretation and reuse. Generic labels such
as “the pair is a valid collision” are insufficient. For example, state the exact
pair, hash, truncation width and domain separator. Standard mathematical notation
and familiar results do not require first-principles definitions. Supporting proof
belongs in proof files and need not be copied into the manifest. Self-containment
and mathematical correctness are LLM review obligations, not schema guarantees.

Familiar standard mathematical results may be invoked with their precise variant,
hypotheses and applicability checked. For an unfamiliar, uncertain or nonstandard
material external premise, the needed supporting argument must be reproduced and
validated in supplied evidence. A citation, URL, paper title or quoted theorem
alone does not suffice. This version does not retrieve PDFs or websites.

An authenticated accepted prior submission supplies only its original declared
claims, parsed from exact original manifest bytes. The downstream judge assesses
whether the needed result follows under their actual assumptions and scope. It
must decline ambiguous reinterpretation or missing definitions. It cannot import
meaning from current challenge rules, prior reason prose or incidental proof
assertions. Current evidence must independently reproduce and validate enough
context for applicability; missing context cannot be invented. The original proof
need not be rejudged merely to use an unambiguous accepted claim.

The judgment has exactly `valid`, `scores`, `reason`, `evidence_refs` and
`review_standard`. Reason explains acceptance or the missing premise; known
source/run/prior references provide diagnostics. There is no exported theorem
catalog, duplicate accepted-claims list, dependency/hypothesis mapping, coverage
report or per-objective support table. Structural validation does not prove that
a model discovers every missing premise or judges truth and entailment correctly.

For bound objectives the model returns `scores: null`; the verifier derives scores
only from current source/check gates after atomic validity. Prior acceptance
cannot supply a score, witness pass or current objective entitlement. See the
[score rule](authoring.md#score-availability) and [reference wire](prior-results.md).
