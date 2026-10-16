# Challenge

Edit `rules.md`, `verifier.json`, and the example in `submission/` directly.
Participants edit only `submission/`. Store each proof once and reference it from
its claim's `evidence` list.

Run `llm-verifier check --root .` for static validation. For custom checkers, add
`checker-tests.json` and run `llm-verifier test-checkers --root .` after preparing
the configured Docker image. These commands make no model calls.

To generate or refresh Yukon/Actions files, run `llm-verifier github --root .
--verifier-revision REVIEWED_COMMIT`. See the installed verifier's authoring guide
for the checker-test format, score sources, and evaluation commands.

The organizer selects a native OpenAI model ID in `verifier.json`. Supply
`OPENAI_API_KEY` only to the separate review process or benchmark workflow; keep
checker tests and candidate execution credential-free.
