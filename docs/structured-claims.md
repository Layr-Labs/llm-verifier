# Exact structured claim data

Protected organizer configuration may opt in with `"structured_claims": "atomic-v1"`.
This requires the default `atomic-claims-v1` acceptance policy. Omission preserves
the existing normalized config, review instructions, five-field judgment schema,
score bindings, dossier shape and accepted-result format. Unsupported values,
including `false` and `null`, fail configuration validation.

With opt-in, a claim object may contain `structured` alongside `statement`,
`evidence` and its optional `score`. Without `structured`, object claims still
require `score`; ordinary string claims remain supported. For example:

```json
{
  "statement": "The declared algorithm satisfies this bound under the stated assumptions.",
  "score": {"numerator": "1", "denominator": "1"},
  "evidence": ["proof.md"],
  "structured": {
    "version": "example-bound-v1",
    "bound": {"coefficient": "1", "exponent": "1/2"}
  }
}
```

`structured` is a nonempty JSON object, bounded to 16 KiB of canonical JSON and
subject to the existing JSON complexity, submission and prompt limits. It is part
of the original declared claim, not an incidental hint. Only an explicit objective
`score_source` selects a numeric score. Other structured claims are still reviewed
and retained, whether or not they have a `score` field.

The verifier preserves every structured key and value. It does not infer, repair,
normalize or replace them from prose, filenames, hints, other claims, model replies
or prior scores. Protected rules define their interpretation, assumptions and
relationship to the statement and evidence. Domain-specific expression validation,
resource completeness and comparison remain the consuming challenge's responsibility.
No domain comparator or numerical-expression parser is added by this feature.

## Atomic review and acknowledgment

The actual review payload contains both the exact submission and ordered
`structured_claims` records for every original claim carrying a structured object:

```text
{claim: index, statement, structured, source_sha256,
 evidence: {relativePath: snapshotFileSha256}}
```

The protected system metadata contains `structured_claims: "atomic-v1"` and
`structured_claims_sha256: digest(records)`. Candidate-controlled structured content
appears only as untrusted user evidence. A mandatory instruction requires review
of the entire exact set and its correspondence to the statements and supporting
evidence. Every initial or final judgment must explicitly return the same
`structured_claims_sha256` in addition to the five existing judgment fields.
Missing, malformed, mismatched, reordered or subset acknowledgments fail review
validation. A follow-up request carries no judgment; its eventual final judgment
must acknowledge the same original structured set.

`valid: true` still accepts **all original claims jointly**, including every
structured object. The digest acknowledgment cannot select a subset and is
required on rejections too. An opted-in submission with no structured claims
acknowledges `digest([])`; no record is inferred from its prose. Acknowledgment and
source binding do not establish mathematical truth: that remains model review.

## Provenance and extraction

Each record uses exactly the existing objective-source formula:

```python
source_sha256 = digest({"source": {"claim": index}, "files": digest(intake["data"]["files"])})
```

`digest` is `llm_verifier.jsonutil.digest`: SHA-256 of sorted, compact ASCII JSON.
The complete immutable file snapshot includes the original manifest and every
auxiliary file. Even a change to a nonobjective claim or auxiliary file changes
source identity. Existing protected preprocessing can normalize a separate score
field, but leaves the exact structured object and original file records intact.

For review fixtures and inspection:

```python
from llm_verifier.structured_claims import records
from llm_verifier.jsonutil import digest

claim_records = records(intake["data"])
acknowledgment = digest(claim_records)
```

This pure helper assumes validated intake data and **does not establish acceptance**.
For downstream accepted evidence, use the complete trusted dossier:

```python
from llm_verifier.pipeline import accepted_structured_claims

claim_records = accepted_structured_claims(dossier)
```

The extractor reconstructs the entire phase chain with the retained finalization
time, verifies exact prompt/schema/source/provenance bindings and the review
acknowledgment, then compares the complete rebuilt dossier. It rejects simulations,
rejections, missing opt-in and altered or incomplete records. Opted-in dossiers
include `data.accepted_structured_claims`: the exact records on acceptance, `[]` on
rejection. Existing source bundles retain the original manifest without introducing
a second judge-authored claim catalog. The intake and dossier envelope hashes are
available separately for a consumer's envelope.

Hashes provide integrity, not authentication. As with finalization and accepted
result export, consume only organizer-produced artifacts from the trusted run
channel, never candidate-uploaded reports. HTTP-mocked tests exercise binding and
orchestration; their synthetic decisions do not qualify a live mathematical judge.

Historical artifacts remain bound to their original verifier implementation.
Replay them with that exact retained source; implementation mismatch still fails.
No old dossier, source, policy, prompt or timestamp is rewritten by this feature.
A challenge must wait for a reviewed published verifier revision before updating
its hosted workflow pin; a local patch is not a publication SHA.
