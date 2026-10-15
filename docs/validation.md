# Initial implementation validation

Validation is offline and scoped to implementation contracts. This is not a live
mathematical benchmark result or hosted workflow acceptance.

- Python 3.13.1 local unittest suite: contract, strict input/file boundaries,
  provider failure redaction, HTTP worker deadlines with localhost trickled
  headers/body, workflow bootstrap pins, CLI exit/stale-output behavior,
  simulation isolation and Docker command/capture/cleanup tests passed.
- Actual Docker 29.6.0 with the configured digest-pinned linux/amd64 Python image:
  both gated tests passed, covering finite experiment output, UID 65534,
  read-only snapshot, no key/socket, loopback-only network, wall timeout and
  bounded output. The default unit suite skips these two tests unless enabled.
- Parent reproductions for failed container cleanup and a killed host Docker
  process now raise infrastructure errors. Injected fake OpenRouter transport
  cannot finalize an official score.
- `actionlint` 1.7.12: workflow passes without suppression. Bootstrap identity
  matching/mismatching cases also run locally as the actual shell block.
- Both bundled examples complete scripted offline CLI simulation. Parent's
  independent Yukon schema/result parser accepted both one-track v2 manifests
  and tested unscored/invalid/field-scoped result shapes.

Final verification on 2026-10-14:

- `env -u PYTHONPATH .venv/bin/python -m unittest discover -v`: **55 discovered,
  53 passed, 2 skipped** (the separately gated Docker tests).
- `env -u PYTHONPATH LLMV_DOCKER_INTEGRATION=1 .venv/bin/python -m unittest
  tests.test_runner.DockerIntegration -v`: **2 discovered, 2 passed, 0 skipped**.
  All 55 unique tests therefore passed across those two runs.
- A fresh wheel from final source installed as `llm-verifier==0.1.0` in `.venv`;
  the full suite above ran against that installed package, with `PYTHONPATH`
  removed. Both installed `llm-verifier simulate` examples completed.
  `python -m pip check` reported no broken requirements.
- The typed-JSON parent reproduction now rejects integer-to-float manifest
  substitution and integer-to-boolean parsed-result substitution. Regression
  tests also cover request/budget copies and Yukon UTF-16 reason length.
- Final actionlint and local documentation link checks passed. The optional
  independent `jsonschema` reference-file cross-check was unavailable in the
  host Python environment; no runtime dependency was added for it. Executable
  config/submission validators cover both example documents in the suite.

Local `yukon run` is explicitly unsupported: setup immediately regenerates intake
before finalization. Local verification uses the separate CLI phases directly.

At the initial validation recorded above, source was left uncommitted for parent
review; no push or publication had occurred. Subsequent internal source
publication to `Layr-Labs/llm-verifier` does not extend that validation to hosted
execution. No live OpenRouter calls, hosted GitHub Actions run, paid benchmark,
challenge import or deployment has been performed.
