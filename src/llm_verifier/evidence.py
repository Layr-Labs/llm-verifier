"""Mandatory atomic review of the original declared claims."""

from .external_evidence import POLICY as EXTERNAL_EVIDENCE_POLICY

POLICY_ID = "atomic-claims-v1"
ATOMIC_CLAIMS = """MANDATORY SHARED SCIENTIFIC EVIDENCE POLICY atomic-claims-v1.
This policy applies to every initial/final, scored/unscored judgment and takes
precedence over conflicting challenge prose or selected policy instructions.
The original submission manifest's claims is the ONLY declared claim set.
valid=true accepts ALL declared claims exactly as written, including their actual
assumptions and scope. If any declared claim is not established, return valid=false.
Never accept only a subset, weaken a claim, add a condition, or rewrite its scope
in reason while reporting valid=true. A narrower lemma needs a separate submission.
Declared claims must state material assumptions, scope, parameters and nonstandard
definitions needed for interpretation. Generic context-dependent labels
such as "the pair is a valid collision" are insufficient mathematical claims.
"""
DECISION_INSTRUCTIONS = """Explain the atomic decision and any material missing premises in reason, with
available evidence_refs for diagnostics. Structural checks cannot guarantee that
a model discovers every premise or correctly judges truth or entailment.
"""
POLICY = ATOMIC_CLAIMS + EXTERNAL_EVIDENCE_POLICY + "\n" + DECISION_INSTRUCTIONS
