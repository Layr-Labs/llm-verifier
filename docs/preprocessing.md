# Protected manifest preprocessing

A challenge can select one optional, versioned manifest adapter in its protected
`verifier.json`:

```json
"manifest_preprocessor": "curvesmash-v1"
```

Only the organizer's configuration enables it. Omit the field to disable it;
unknown names, `null`, code paths and candidate-supplied settings are rejected.
The adapter is ordinary reviewed code shipped inside the pinned verifier package.
There is no plugin discovery, candidate-code execution, script loading, file
filter or second hook stage. New adapters require a reviewed verifier revision.

## CurveSmash v1 rules

| Input | Transformation | Preserved constraint |
|---|---|---|
| Valid unreduced claim score, such as `130815246/1000000` | Reduce exactly to `65407623/500000`; zero becomes `0/1` | Original value, canonical integer-string syntax, positive denominator, 256-character limits before reduction, representability and exact objective bounds |
| Finite integer enum without one or both bounds | Add missing `minimum`/`maximum` from the enum | Membership, supplied bounds, distinct integer values, enum count and numeric caps |
| Finite string enum without `maxLength` | Add the longest enumerated string's length | Membership, supplied length restrictions, enum count and string-size cap |

Enum repair follows nested object properties and array items in experiment input
schemas. It never replaces supplied bounds or turns an enum into a continuous
range. Initial inputs, required fields, experiment sources, statements, evidence
paths, checks and prior references are unchanged. Ordinary validation runs on the
result and still rejects contradictory bounds, invalid enums and unsupported
keywords. Protected checker schemas and objective bounds are not repaired.
CurveSmash's existing exploratory policy, additional rules files and inert
submission selectors are supported by the same verifier revision.

Integers with no bounds or finite enum remain errors: an intended range cannot
be inferred. Binary caches, other non-UTF-8 files, symlinks, paths outside the
candidate tree and excessive packages still fail before the adapter. Packaging
must exclude generated caches; the verifier does not ignore submitted files.

Optional experiment arguments retain their omission semantics. For this batch,
the organizer can separately select the existing setting:

```json
"response_format": "json_object"
```

Place that field inside `provider`, retaining the other provider settings.
This opts out of provider-enforced output shape while keeping the same local
decision and experiment-input validation. There is no automatic fallback and
the adapter never marks optional inputs required or fills their defaults. Strict
mode with requestable optional fields fails during preflight/intake. Unused
follow-up schemas are omitted when `max_followups` is zero.

## Snapshot and audit

The flow is original file snapshot → optional manifest adapter → normal
validation → execution/review. Path, UTF-8 and resource checks run on the original
package first. The effective package must satisfy the same limits after repair.
Original source files are never edited.

Intake keeps exact original records in `data.files`, the effective parsed
manifest in `data.manifest`, and, only when enabled, `data.preprocessing`:

- `adapter`: the immutable rules identifier, `curvesmash-v1`.
- `original_sha256` / `effective_sha256`: exact manifest file hashes.
- `changes`: deterministic operations with a component-array `path`, `op`,
  `after`, and `before` for replacements. Added bounds use `op: "add"`.

The pinned verifier implementation hash covers the adapter's source, while the
configuration hash binds its selection. Every phase reconstructs the effective
manifest and audit from the original bytes and compares them with the intake.
Editing and resealing the manifest or audit cannot bypass these checks.

Candidate execution receives the effective `submission.json` and otherwise
identical source records. Review receives that same manifest, the original
manifest record, and the audit. Run provenance hashes the effective execution
bundle; submission identity continues to hash the original package. Both initial
and follow-up plans use the effective declarations. A no-op adapter preserves
all file bytes and records an empty change list.

Accepted-result exports retain the existing `atomic-submission-v1` format, exact
original manifest bytes, original source identity and whole-submission verdict.
The exported `policy_id` preserves the origin acceptance standard; exploratory
acceptance remains `plausible_not_refuted` when reused. Existing
intake/dossier/verifier hashes bind the audit. Imported accepted claim
fractions are checked through an equivalent reduced copy without rewriting the
original record or granting new acceptance. Reuse does not require the consumer
to enable CurveSmash preprocessing. Historical full replay still needs the
origin's pinned verifier/configuration/dossier; no migration or re-signing is
performed. Older verifier revisions can reject unreduced accepted claims, so
consumers of these new exports need a revision with this parsing support.

## Local checks and adoption

```sh
llm-verifier preflight --root challenge --config verifier.json \
  --submission submissions/selected-candidate
```

Preflight is credential-free, performs no network requests or execution, and
writes no files. It prints the original package/configuration hashes and audit.
It validates the selected policy/admission shape and manifest semantics, including
initial/final response-schema construction; it does not resolve prior results or
prove the claims. `check` applies the same manifest gates to the starter subtree,
while intake and hosted `workflow-prepare` enforce them before creating an
execution plan. Errors identify the offending field or file.

To adopt this for CurveSmash, publish and pin the reviewed verifier revision in
its installer and reusable workflow, enable `manifest_preprocessor` in the protected track configuration, select JSON mode explicitly for the optional
input cases, and run preflight on the exact selected packages. Keep the enabling
configuration version scoped to the intended evaluation batch. Removing the
setting for future evaluations restores ordinary requirements; preserve the old
pin and dossiers to reproduce that batch. These changes alone neither re-dispatch
submissions nor turn evaluation failures into acceptance.
