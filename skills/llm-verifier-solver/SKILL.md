---
name: llm-verifier-solver
description: Prepare and critique submissions to an existing llm-verifier challenge, including structural checks, declared computations and independent review. Use when solving or revising a candidate submission.
---

# Solve an llm-verifier challenge

Read the [solver guide](../../docs/solving.md) from the same verifier revision as
the challenge's CLI. Follow its preparation, independent critique and reporting
workflow against the complete final submission and protected challenge context.

Use the generated participant README or AGENTS pointer to locate the pinned
guide. If this skill was copied alone, obtain `docs/solving.md` from that exact
verifier revision; do not substitute a moving branch or invent a pin.

Keep advisory subagent critique distinct from configured-provider evaluation,
and keep execution separate from credentials. Report any unavailable check or
review explicitly. Challenge creation belongs to `llm-verifier-challenge`.
