# Review failures and retained evidence

A failed review remains an infrastructure failure. It does not produce a judgment,
score, or accepted result. Version 0.7.1 retains diagnostic evidence for these
failures without changing the successful review envelope or the legacy single
reviewer request.

The planner now receives `minItems: 1` and the selected committee `max_regions` as
`maxItems`, in preflight, execution, and replay. The bundled decomposed profile
selects eight regions; configured caps through the existing hard maximum of 16
remain supported. Coverage validation still checks every source line independently
of the wire schema.

## Diagnostic output

Both review CLI commands accept `--failure-out PATH`. The default is
`OUT.failure.json`, where `OUT` is the normal `--out` path. A successful review
removes a stale diagnostic file and writes the same successful output as before.
A caught review failure writes the diagnostic atomically, leaves the successful
output absent, and returns a nonzero exit code. All review input, successful
output, and failure paths must be distinct, including hardlink/symlink aliases;
symlink parents are rejected. Intake and parent checkpoints are verified before a
failure artifact can claim their identity.

The library `pipeline.review(..., failure_out=path)` enables the same diagnostic
capture. Its default remains no diagnostic file. Library callers own their output
paths; the CLI additionally guards against aliases of the input files.

A diagnostic uses the ordinary hash envelope with **stage `review_failure`** and
the original intake bindings and parent hashes. Its data contains:

- `phase`: `initial` or `final`;
- `error`: a fixed error category, without arbitrary provider exception text;
- `publishable: false` and an explicit `simulation` envelope flag;
- ordered `calls`, with member/stage/region identity, prompt/schema/provider
  configuration hashes, and provider `events`;
- `truncated`, indicating that the diagnostic capture budget was reached.

An event records the attempt number, elapsed provider-call time, HTTP status or
fixed error category when available, and a `response` projection. Raw assistant
text is captured **before strict response parsing and semantic validation**,
including invalid JSON and incomplete outputs. Safe response/model identifiers
and independently valid numeric token usage are retained even if other response
fields are invalid. An unparseable HTTP 200 envelope has no trustworthy field
boundaries, so arbitrary envelope text is omitted: `unparsed_body_omitted`, the
original `body_bytes`, and `redacted_body_sha256` record this explicitly. The hash
covers UTF-8 replacement decoding followed by credential redaction, not the
original wire bytes. Malformed background polls record the failed response rather
than the preceding queued response. This omission does not apply to invalid
assistant decision JSON inside a parseable provider envelope; that raw assistant
text is retained. Successful background responses retain the normalized assistant
output, and failed native responses cross the private worker pipe as the same
bounded projection. Background cleanup behavior is unchanged.

Only assistant text, safe identifiers, and numeric usage are retained from parsed
envelopes. Reasoning, tool arguments, arbitrary provider metadata, HTTP error
bodies, headers, and exception messages are excluded. The actual OpenAI credential
is redacted from captured text, including normally JSON-escaped characters. Raw response text never appears in CLI logs.
Each event retains at most 256 KiB of serialized text; the review retains at most
4 MiB of event capture before omitting further text while preserving bounded
metadata. At most 128 events are recorded, within the existing 64-request limit.
Truncation and omitted captures are explicit. Unavailable usage is left absent,
not estimated. This is diagnostic evidence, not a billing receipt.

The failure stage and shape are rejected by review verification, followup
execution, finalization, dossier replay, and result export. Changing its stage
label does not satisfy the successful-review schema. Simulation inputs and
injected providers continue to mark failures as simulation. Failure files are
uploaded separately as `llmv-initial-review-failure` or
`llmv-final-review-failure`; neither is a completed review checkpoint.

## Completed review recovery

Artifact delivery retries do not retry review generation. A downstream export or
delivery recovery must reuse the immutable original intake, experiment, review,
and verifier wheel identities. Export a completed dossier using its matching
wheel and preserve its original run/attempt identity; a later delivery attempt
has separate provenance. A new verifier release must not be substituted while
replaying an old intake. Diagnostic artifacts cannot replace missing completed
checkpoints or authorize a paid rerun. The consuming platform is responsible for
authorizing recovery and preventing stale or duplicate publication.
