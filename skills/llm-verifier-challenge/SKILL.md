---
name: llm-verifier-challenge
description: Author challenges with llm-verifier, including rules, baseline submissions, score sources and protected checkers. Use when creating, rebuilding or changing a challenge, rather than solving an existing challenge.
---

# Author an llm-verifier challenge

Read the [authoring guide](../../docs/authoring.md) from the same verifier revision
as the CLI. If this skill is installed separately, obtain `docs/authoring.md` from
`Layr-Labs/llm-verifier` at the selected revision. Use that guide for schemas and format details.

Use the task's specification, challenge name, reviewer model and verifier revision.
Resolve ambiguities affecting scope, eligibility or score meaning before encoding them.

- For a new directory, run `llm-verifier init DIRECTORY --name NAME --model OPENAI_MODEL`.
  Replace the toy rules, objective and example submission together.
- Edit ordinary files. Keep scientific rules in `rules.md`, store each proof once,
  and reference its files from the claim. Use `score_source` for each scoring objective.
- Minimize review surface: add code, tests and prose only for a concrete challenge
  requirement. Reuse the shared runner and keep generic protocol tests upstream.
  Custom checkers need compact, independently determined positive and negative cases.
- Run `llm-verifier check --root DIRECTORY`. For custom checkers, prepare the configured
  pinned Docker image and run `llm-verifier test-checkers --root DIRECTORY` without
  model credentials.
- When hosted integration is needed, run `llm-verifier github --root DIRECTORY --verifier-revision FULL_COMMIT` using the same verifier pin, then rerun `check`.

Report the challenge files, validation results and remaining qualification briefly.
Static and checker validation do not establish mathematical acceptance. Report missing
framework capabilities explicitly.
