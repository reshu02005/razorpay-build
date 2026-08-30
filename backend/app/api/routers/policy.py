"""
The guardrail configuration, exposed read-only.

There is no PUT and no PATCH here, and that is a design decision rather than an
unfinished feature. If policy could be edited through the same API the agent's
flow uses, the guardrails would be reachable from the automated path -- and a
limit that an automated system can raise is not a limit. Changing policy means
changing configuration and restarting, which leaves a trace outside the running
process.

The handler assembles ``PolicyOut`` from ``Settings`` plus the engine's static
rule catalogue. That is field mapping, not decision-making: no threshold is
compared against anything here.

Every route declares ``response_model=`` and ``summary=`` so ``/docs`` reads as
the API reference for this service.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import PolicyEngineDep, SettingsDep
from app.domain.enums import FailureCategory
from app.domain.schemas import PolicyOut

router = APIRouter(prefix="/api/policy", tags=["policy"])


@router.get(
    "",
    response_model=PolicyOut,
    summary="Read the active guardrail policy",
    description="The exact limits in force right now, plus the catalogue of all 13 rules. The "
    "frontend renders this page directly from the response, so the documented policy and the "
    "enforced policy cannot drift apart.",
)
def get_policy(settings: SettingsDep, engine: PolicyEngineDep) -> PolicyOut:
    """Return the guardrail limits and rule catalogue currently in force."""
    return PolicyOut(
        max_recovery_attempts=settings.max_recovery_attempts,
        recovery_cooldown_seconds=settings.recovery_cooldown_seconds,
        high_value_review_threshold_paise=settings.high_value_review_threshold_paise,
        max_recovery_amount_paise=settings.max_recovery_amount_paise,
        daily_recovery_budget_paise=settings.daily_recovery_budget_paise,
        max_cases_per_customer_per_day=settings.max_cases_per_customer_per_day,
        min_propensity_score=settings.min_propensity_score,
        max_payment_age_hours=settings.max_payment_age_hours,
        require_human_approval=settings.require_human_approval,
        auto_approve_enabled=settings.auto_approve_enabled,
        auto_approve_max_paise=settings.auto_approve_max_paise,
        auto_approve_min_propensity=settings.auto_approve_min_propensity,
        recovery_link_ttl_minutes=settings.recovery_link_ttl_minutes,
        # Derived from the enum rather than restated as a list here: the
        # non-recoverable set is a property of the taxonomy, and duplicating it
        # in the API layer would let the two copies disagree after someone adds
        # a category.
        non_recoverable_categories=[c for c in FailureCategory if not c.is_recoverable],
        rules=engine.rule_catalogue(),
    )
