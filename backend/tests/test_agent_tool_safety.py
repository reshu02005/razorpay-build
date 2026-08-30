"""
The safety property that lets a language model near a payment system at all:
nothing the model can call moves money, and nothing it can say sets an amount.

Every other control in RecoverAI is a check on a decision the agent made. This
file tests the shape of the agent's *reach* -- what it is even capable of
attempting. That distinction matters because guardrails can be misconfigured,
while a tool that does not exist cannot be invoked by any prompt, jailbreak or
hallucination.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.tools import TERMINAL_TOOL, ToolRegistry
from app.config import Settings
from app.db.models import Payment
from app.domain.enums import ToolCapability
from app.domain.schemas import AgentRecoveryPlan

#: The tool surface as reviewed. Six read-only lookups plus one proposal tool.
EXPECTED_TOOLS = {
    "get_payment_details",
    "get_customer_history",
    "classify_failure_code",
    "score_recovery_propensity",
    "get_recovery_policy",
    "check_recovery_eligibility",
    "submit_recovery_plan",
}

#: A plan that satisfies every field constraint, used as the baseline the
#: rejection tests deviate from by exactly one field.
VALID_PLAN = {
    "failure_category": "gateway_error",
    "confidence": 0.82,
    "strategy": "retry_same_method",
    "rationale": "The acquirer returned a gateway-side fault, so the same rails "
    "should clear on a retry once the transient condition passes.",
    "customer_message": "Sorry - a temporary glitch stopped your payment. "
    "Please try again; nothing was charged.",
}


@pytest.fixture()
def registry(db, settings: Settings, failed_payment: Payment) -> ToolRegistry:
    return ToolRegistry(db, settings, failed_payment)


def test_no_tool_offered_to_the_model_can_move_money(registry: ToolRegistry) -> None:
    """
    The central invariant. ``ToolCapability.FINANCIAL`` exists in the enum for one
    reason -- so that its absence from this list is checkable rather than asserted
    in a comment. Order creation lives in the service layer, behind a human
    approval, and is unreachable from the tool loop.
    """
    financial = [s.name for s in registry.specs() if s.capability is ToolCapability.FINANCIAL]
    assert financial == []


def test_only_the_terminal_tool_may_write_anything(registry: ToolRegistry) -> None:
    """
    Everything except the proposal tool is a pure lookup.

    A write-capable tool in the middle of the loop would let a partially-reasoned
    intermediate state be persisted, which is how an agent ends up committing to a
    plan it later argued itself out of.
    """
    writers = {s.name for s in registry.specs() if s.capability is not ToolCapability.READ_ONLY}
    assert writers == {TERMINAL_TOOL}
    assert registry.get(TERMINAL_TOOL).capability is ToolCapability.WRITE_PROPOSAL


def test_the_tool_surface_is_exactly_the_seven_reviewed_tools(registry: ToolRegistry) -> None:
    """
    This test is *designed* to fail when someone adds a tool.

    That is the point of it, not a maintenance cost. Expanding what an autonomous
    financial agent can do should require deleting a line from a test named after
    the review that approved the current surface -- a deliberate act with a diff
    an approver can see -- rather than happening as a side effect of a feature
    branch.
    """
    assert {spec.name for spec in registry.specs()} == EXPECTED_TOOLS


def test_a_plan_cannot_carry_an_amount() -> None:
    """
    The schema-level half of "the AI cannot set the amount".

    ``AgentRecoveryPlan`` forbids extra keys, so a model that invents
    ``amount_paise`` fails validation instead of having the field quietly dropped.
    Silent dropping would be the dangerous outcome: the run would look successful
    while the model believed it had changed the charge, and nobody would ever see
    the attempt in a log.
    """
    with pytest.raises(ValidationError):
        AgentRecoveryPlan(**{**VALID_PLAN, "amount_paise": 1})


def test_a_plan_cannot_smuggle_an_unknown_instruction() -> None:
    """Same guarantee, generalised: any unmodelled key is a hard validation error."""
    with pytest.raises(ValidationError):
        AgentRecoveryPlan(**{**VALID_PLAN, "payout_account": "acc_attacker"})


def test_the_baseline_plan_is_actually_valid() -> None:
    """
    Guards the two tests above from passing for the wrong reason: if the baseline
    itself were invalid, ``pytest.raises`` would succeed no matter what the extra
    field did.
    """
    plan = AgentRecoveryPlan(**VALID_PLAN)
    assert plan.strategy.value == "retry_same_method"


def test_a_tool_that_raises_returns_an_error_object_to_the_model(
    registry: ToolRegistry,
) -> None:
    """
    A tool exception must reach the model as data, not as a traceback that ends
    the run.

    Calling with an argument the tool does not accept raises inside the registry,
    which is the closest reproduction of the real failure mode: a model
    hallucinating a parameter name. The loop has to survive that and let the model
    correct itself, otherwise one bad guess degrades the whole analysis.
    """
    result = registry.call(
        "classify_failure_code", {"parameter_the_model_invented": "nonsense"}
    )
    assert isinstance(result, dict)
    assert "error" in result


def test_submitting_a_plan_with_an_invalid_enum_is_refused_not_raised(
    registry: ToolRegistry,
) -> None:
    """
    A hallucinated strategy is the most likely single failure of the LLM path.

    It must come back as ``accepted=False`` -- a message the model can read and
    retry against -- rather than an exception, and it must certainly not be
    coerced into some nearby valid strategy. Silent coercion would mean the
    approval screen showed a plan the model never proposed.
    """
    result = registry.call(
        TERMINAL_TOOL,
        {**VALID_PLAN, "strategy": "wire_the_money_somewhere_else"},
    )
    assert result.get("accepted") is False
