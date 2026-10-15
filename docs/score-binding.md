# Protected objective binding (0.3.0, binding version 1)

The optional `score_binding` on an objective adds deterministic value and exact
main-claim acceptance checks to the existing [required-check gate](checkers.md):

```json
{"name":"coefficient","direction":"+","unit":"gates per input bit","semantics":"Accepted unconditional bound for the pinned family/model.","score_binding":{"version":1,"check":"main-claim","field":"score_record"}}
```

The checker must be registered with `required_for_score:true`; `field` must be a
required object property in its output schema. It returns one of these closed
semantic records:

```json
{"kind":"finite","claim_id":"claim:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","numerator":"31","denominator":"10"}
```

```json
{"kind":"terminal","claim_id":"claim:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","terminal":"positive_infinity"}
```

The displayed hash is illustrative. The protected checker must derive the ID
from the complete canonical main-claim declaration, including its family,
circuit model, quantifiers, coefficient/endpoint and proof identity. Do not
accept an arbitrary candidate-selected ID. Candidate proof files and declared
proof identity must agree, which the reviewer must explicitly assess if the
checker only receives declared inputs. All candidate bytes, canonical inputs,
protected source and reports remain bound to the current intake.

The checker should state that `passed` verifies its scoped encoding/arithmetic
predicate, never the universal proof. A syntactically valid main claim can still
be mathematically false. The reviewer accepts the exact identifier only after
reviewing the whole main claim against protected rules. No prose parsing or
inferred main-claim acceptance is used.

## Checker schema and protected prompt boundary

Use a bounded closed record object with required `kind` and `claim_id`, plus
optional `numerator`, `denominator` and `terminal`. The existing restricted schema
dialect has no union, so the binding layer enforces the exact two variants.
For example:

```json
{"type":"object","properties":{"kind":{"type":"string","enum":["finite","terminal"],"maxLength":8},"claim_id":{"type":"string","maxLength":70},"numerator":{"type":"string","maxLength":256},"denominator":{"type":"string","maxLength":256},"terminal":{"type":"string","enum":["positive_infinity"],"maxLength":17}},"required":["kind","claim_id"],"additionalProperties":false}
```

Only semantic-validated records enter the protected `score_gate.bindings`:
claim IDs have the fixed `claim:[a-f0-9]{64}` grammar; rationals use canonical
digit strings; kinds and terminal labels are fixed enums. Arbitrary output
text, echoed inputs and scientific prose remain user evidence. The original
protected-source exclusion from model prompts is unchanged.

## Finite values and exact acceptance

A finite record requires a canonical signed numerator, positive denominator,
reduced fraction and at most 256 characters per integer string. Zero is `0/1`;
leading zeroes, negative zero, non-reduced fractions and zero/negative
denominators are rejected. This is an explicit representation resource bound,
not a mathematical ceiling or scientific eligibility threshold. Any tighter
challenge domain, such as nonnegative coefficients, belongs to its checker.

Every non-null tuple requires all ordinary required checks, a consistent passing
record for each bound objective and `valid:true`. The record's exact `claim_id`
must be a separate item in `accepted_claims`. An accepted auxiliary lemma alone
cannot score. The model's score must equal Python's deterministic binary64
projection of the exact fraction; equality has no tolerance. Integer/float
spellings of the same numeric value are equivalent here, while canonical checker
input and artifact identity still distinguish their JSON types. Mismatches are
rejected at review consumption and finalization, never rewritten.

The exact rational is retained in protected checker evidence and dossier
`score_records`. Yukon receives the raw coefficient as a finite JSON number,
which is a rounded transport representation, not an exact rational. Distinct
fractions may tie in binary64; native Yukon comparison cannot recover their
exact ordering. No normalization or baseline constant is applied. The supported
range includes `1/10^255` and a 256-digit numerator; overflow/nonzero underflow
are rejected rather than promoted to an endpoint.

## Terminal records and ordinary unscored work

A terminal record is allowed only for a maximizing objective. Any eligible
terminal declaration forbids a numeric tuple, even if the model chooses zero,
a finite corollary, or a huge sentinel. With `valid:true`, all required checks
satisfied, exact `claim_id` acceptance and `scores:null`, the dossier derives:

```json
{"objective":"coefficient","record":{"kind":"terminal","claim_id":"claim:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","terminal":"positive_infinity"},"check":"main-claim","passing_runs":["run:initial:0"],"projection":{"status":"unsupported","reason":"yukon_positive_infinity_unsupported"}}
```

This is the semantic positive-infinity endpoint. Its null Yukon tuple is an
explicitly unsupported projection, not a null scientific score or an ordinary
lemma. Yukon does not natively rank/display this endpoint. Manual review and
promotion must inspect the typed dossier. With multiple objectives a terminal
record requires the **whole** `scores` tuple to be null; partial numeric tuples
are not supported. `allow_unscored` must permit the null tuple under the existing
validity rules.

A relevant lemma may omit checker declarations and receive `valid:true,
scores:null`. Without explicit accepted terminal claim identity its dossier has
no terminal record. Failed/invalid/conflicting records also cannot produce a
semantic score. An invalid numeric judgment is rejected for bound objectives.

## Retry, provenance and compatibility

Only successful schema-valid `passed:true` reports for the submission's exact
canonical declared inputs qualify. Judge-only/different-input passes cannot
substitute. Matching retries may recover a failed initial run. Different passing
records for the same canonical input fail closed instead of selecting the best
one. Different-input diagnostics cannot change an already established binding.

Existing configurations without `score_binding` retain their previous scoring
behavior. Config/submission wire versions stay `1`, binding records have version
`1` in config, and package version is `0.3.0`. Source-bound phase artifacts must
restart at intake after upgrade. The new dossier `score_records` field is an
empty list when no semantic records are accepted. No new runtime dependency,
model phase, platform schema, admission dialect or formal proof certification
is introduced.

## Scientific-proof attachment gap

The CircuitBreaker pilot exposed an input-size constraint: a complete proof bound
inside canonical checker inputs must fit the existing **16 KiB aggregate** input
limit. Splitting a proof into strings does not bypass that cap; individual strings
also have the existing 4096-character schema limit. The bounded pilot can carry
short proofs and precise provenance, but does not provide a general long-proof
attachment interface. A future protected attachment-digest/input binding or
carefully configurable aggregate budget needs separate design and tests. This
release does not silently lift limits or give checkers access to candidate files.
