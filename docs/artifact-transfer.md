# Verified same-run artifact transfers

`verify.yml` downloads artifacts with `scripts/artifact_transfer.py`. This is a
standalone Python standard-library program, available before installing the wheel.
It does not import verifier or candidate code. A failed transfer stops that job
before its verifier phase; retrying transport never repeats a review or experiment.

Only the trusted prepare checkout emits `helper_base64` and `helper_sha256`.
Consumers use the static bootstrap step in `verify.yml`: strict base64 decoding,
an 87,384-character encoded cap, a 65,536-byte decoded cap, and SHA256 equality
before exclusive creation of `.llmv-artifact-transfer/transfer.py`. Inputs enter
through environment variables, never shell interpolation or `eval`. The output
is well below GitHub's 1 MiB per-job limit, including UTF-16 size accounting.
`python3 -I transfer.py install-action` creates a fixed Node launcher using only
Node built-ins. The helper source itself includes that launcher and action metadata.

The local `uses: ./.llmv-artifact-transfer` action receives the existing runner
runtime credential. Only its Python child receives `ACTIONS_RUNTIME_TOKEN` and
`ACTIONS_RESULTS_URL`; the child environment excludes model keys, checkout keys,
`GITHUB_TOKEN`, `PYTHONPATH`, and candidate environment settings. Nothing writes
credentials to outputs, receipts, files, or later steps. Credentialed review jobs
still perform no checkout. The existing caller permissions and supplied-secret
contract are unchanged. REST with `GITHUB_TOKEN` would require `actions: read`,
which legacy callers do not grant and reusable workflows cannot elevate.

## Transfer input and outputs

`LLMV_ARTIFACTS` is a JSON array, supplied only from trusted producer job outputs:

```json
[
  {
    "artifact": {
      "id": "12930800270",
      "name": "llmv-package",
      "sha256": "214b909133bfe302dde3a6483cf41294839cc35ce8f80a5308309fc9f1d888d0",
      "run_id": "40947958680",
      "run_attempt": "1"
    },
    "path": "wheel"
  }
]
```

All descriptor values are strings. `sha256` is the **ZIP archive digest** from the
pinned `upload-artifact` output; a `sha256:` prefix is also accepted. `path` is a
fresh single-component directory under `RUNNER_TEMP`. Existing destinations,
including dangling symlinks, are rejected. No name-only or newest-artifact lookup
is supported. An invalid or missing descriptor fails before any download.

Additive reusable-workflow outputs expose:

- `helper_base64`, `helper_sha256`: trusted bootstrap source and its digest.
- `package_artifact`, `intake_artifact`, `dossier_artifact`: the JSON descriptors
  above, with names `llmv-package`, `llmv-intake`, and `verifier-dossier`.
- `intake_sha256`: the existing intake **envelope** digest, neither a file nor
  archive digest.
- `review_kind`: `judgment` or `followup` from the initial review.
- `review_run_id`, `review_run_attempt`: the selected review's producer identity
  (final review for follow-up, otherwise initial review).
- `initial_review_run_attempt`, `final_review_run_attempt`: individual review
  producer attempts; the final value is empty when that job was skipped.

Existing callers need not consume these outputs. A downstream deterministic
metrics job can copy the static bootstrap step, then transfer package, intake,
and dossier using these descriptors. Optional follow-up descriptors are consumed
only when the original workflow's follow-up condition is true.

After all transfers succeed, `RUNNER_TEMP/llmv-transfer-receipt.json` contains:

```text
{version: 1, run_id, delivery_attempt,
 artifacts: [{artifact: <unchanged descriptor>, path,
              files: {relative_member_name: extracted_file_sha256}}]}
```

This receipt records observed bytes; it does not authenticate arbitrary caller
input or authorize recovery/publication. Consumers must still validate source,
configuration, intake, and phase lineage with their existing verifier contracts.
A delivery rerun preserves the descriptors' original producer attempts and records
its own `GITHUB_RUN_ATTEMPT` separately. The descriptor run ID must equal the current
numeric run; a producer attempt cannot exceed the consumer's attempt. The helper
is not a cross-run artifact recovery API.

## Transport and extraction contract

The implementation follows the runtime protocol in the exact previous
`download-artifact` pin, `018cc2cf5baa6db3ef3c5f8a56943fffe632ef53`:

1. Derive backend run/job IDs from the runner-provided `Actions.Results` scope.
   This parses context only; the GitHub service authenticates the token.
2. `ListArtifacts` with a fixed `id_filter`. Require exactly one result, the exact
   ID/name/backend run, a producer backend job, and a bounded archive size.
   Check the API digest when present against the required producer archive digest.
3. Request `GetSignedArtifactURL` using that exact returned producer backend
   run/job/name. The runtime signing API does not accept an artifact ID.
4. Download to a temporary archive, restarting/truncating all partial bytes and
   hashing anew after a retry. Require metadata size equality and archive SHA256
   equality before opening the ZIP for extraction.
5. Re-list that exact ID and require unchanged metadata before use. This detects
   deletion/replacement while resolving the name/job-addressed signed URL.
6. Validate all archive members, extract regular files into a fresh private staging
   directory, and rename it into the previously absent destination.

Both snake_case and protobuf camelCase response fields are accepted; ambiguous
aliases are rejected. The checked-in protocol fixture was serialized with the
exact pinned action's protobuf codec, using real observed archive ID/name/size/hash
and synthetic backend UUIDs. It is not a captured runtime-service response.

Authenticated requests go only to the runner's HTTPS
`results-receiver.actions.githubusercontent.com` service and never follow
redirects. Blob requests carry no Authorization header. They allow at most three
HTTPS redirects, restricted to `*.blob.core.windows.net` and
`*.actions.githubusercontent.com`, rejecting credentials, nonstandard ports,
fragments, control characters, and malformed URLs before URL normalization.
Errors/logs contain fixed codes and validated artifact IDs, never response bodies,
exception text, runtime tokens, signed URLs, or query parameters.

Each operation has at most four attempts, with 1/2/4-second exponential waits plus
at most 250 ms jitter. Retried failures include resets, connection failures,
timeouts, temporary DNS failures, truncated archives, and HTTP 408/429/500/502/503/504.
Other HTTP errors (including 401/403/404/410), TLS validation failures, wrong
identity, corrupt archives, or unsafe paths fail immediately. Signed URL requests
are refreshed during a download retry, always for the same producer.

All requests share one 180-second monotonic and OS wall-clock deadline across the
entire action invocation, including DNS, headers, body reads, backoff, hashing,
and extraction. Individual requests have a maximum 15-second socket timeout. The
launcher also kills its Python child at 190 seconds; the workflow step limit is
four minutes. A successful earlier archive may remain when a later transfer fails,
but no partial destination or completed receipt is published for a failed archive.
Downstream phase steps cannot run after transfer failure.

Limits per archive: 256 MiB compressed, 512 MiB expanded, and 10,000 members. Only
stored/deflated ZIP members are accepted. Absolute/traversing paths, backslashes,
control characters, drive prefixes, duplicate names, file/directory collisions,
symlinks, special files, and encrypted members fail. Executable modes are not
restored. Member CRC/size and extracted-file SHA256 are checked during extraction.

## Offline validation and remaining hosted qualification

```bash
PYTHONPATH=src python3 -m unittest tests.test_artifact_transfer tests.test_cli_workflow
```

Tests mock all network I/O and include reset-then-success, exhaustion, permanent
failure, streamed-body reset, shared deadlines and a real OS-timer interruption,
strict redirects and credential redaction, immutable identity, archive corruption,
ZIP safety, bounded bootstrap/CRLF rejection, and the exact generated Node launcher.
A read-only replay also verified a real existing package ZIP's archive digest and
safe extraction without installing or executing its wheel.

The wrapper remains GitHub.com/Ubuntu-specific. It intentionally fails closed if
GitHub changes its runtime protocol, service hosts, or backend run identity across
attempts. Numeric same-run provenance does not relax the backend-run equality check.
`test-artifact-transfer.yml` adds a push/PR hosted smoke: upload a tiny known
fixture, then bootstrap and download it in a separate consumer with no checkout
and `permissions: {}`, checking exact bytes, file hash, and transfer receipt.
This CI must pass before downstream rollout. No live dispatch or rerun was
performed by the implementing worker; actual runner injection,
service responses, and retained-artifact behavior across attempts still need an
explicitly authorized hosted qualification. Pin/source upgrades and platform
recovery remain separate changes.

Protocol references:

- [Pinned action runtime download implementation](https://github.com/actions/download-artifact/blob/018cc2cf5baa6db3ef3c5f8a56943fffe632ef53/dist/index.js)
- [Pinned uploader outputs](https://github.com/actions/upload-artifact/blob/b7c566a772e6b6bfb58ed0dc250532a479d7789f/action.yml)
- [Runner injection into JavaScript actions](https://github.com/actions/runner/blob/main/src/Runner.Worker/Handlers/NodeScriptActionHandler.cs)
- [Reusable workflow permission limits](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations)
