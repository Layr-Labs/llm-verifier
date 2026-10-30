# Probabilistic escalation example

An always-selected, offline comparison of single and decomposed review. Both
judges inherit the configured provider. This example uses record-only mode.

```sh
llm-verifier check --root examples/escalation
llm-verifier simulate --root examples/escalation --script examples/escalation/simulation.json --out /tmp/escalation-simulation.json
```

See [configuration and semantics](../../docs/judges.md#probabilistic-escalation-opt-in).
