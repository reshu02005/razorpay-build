"""
The guardrail layer: the limits that decide what the AI is allowed to do.

RecoverAI's claim is "AI decides. Guardrails control. Razorpay executes. Audit
trail proves." This package is the second clause, and it is deliberately the
least clever code in the project: thirteen small pure functions in ``rules`` and
one aggregator in ``engine``. Nothing here calls a model, a database or a
network. A control on money should be readable end to end in an afternoon by
someone who does not trust it yet.

The split is worth stating explicitly:

*   ``rules`` -- what may be wrong with a proposed recovery. Each rule is a pure
    ``(GuardrailContext) -> GuardrailEvaluation`` function, with every fact it
    needs precomputed by the service layer, so the whole policy is exhaustively
    unit-testable without a database.
*   ``engine`` -- how thirteen independent verdicts become one decision:
    most-restrictive-wins, every rule always evaluated, plus the frozen policy
    snapshot that keeps old decisions meaningful after the limits change.

Typical use::

    from app.policy import GuardrailContext, PolicyEngine

    verdict = PolicyEngine(settings).evaluate(ctx)
    if verdict.decision is GuardrailDecision.DENY:
        ...

Re-exported here so callers import from the package rather than reaching into
its modules: the two-module split is an implementation detail, and pinning
callers to it would make the split expensive to revisit.
"""

from __future__ import annotations

from app.policy.engine import GuardrailVerdict, PolicyEngine
from app.policy.rules import (
    NOT_APPLICABLE_REASON,
    RULE_COUNT,
    RULES,
    GuardrailContext,
    GuardrailEvaluation,
    Rule,
    RuleFn,
)

__all__ = [
    "NOT_APPLICABLE_REASON",
    "RULES",
    "RULE_COUNT",
    "GuardrailContext",
    "GuardrailEvaluation",
    "GuardrailVerdict",
    "PolicyEngine",
    "Rule",
    "RuleFn",
]
