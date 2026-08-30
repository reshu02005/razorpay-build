"""
The guardrail engine: the layer that decides whether an AI proposal may become
a charge.

This is the most important file in the suite. The agent is allowed to be wrong;
the guardrails are not. Each of the thirteen rules gets two tests -- one showing
it stays out of the way on a clean case, one showing it fires on exactly the
condition it exists for -- because a rule that never fires and a rule that always
fires are both useless, and only testing both directions distinguishes a working
rule from either failure mode.

The remaining tests cover the properties of the engine itself rather than of any
single rule: aggregation is most-restrictive-wins, every rule reports on every
evaluation, and the policy snapshot that gets frozen into a case row can survive
the trip through the database.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.config import Settings
from app.db.models import Customer, Payment, utcnow
from app.domain.enums import FailureCategory, GuardrailDecision, RecoveryStrategy
from app.policy.engine import GuardrailVerdict, PolicyEngine
from app.policy.rules import NOT_APPLICABLE_REASON, RULE_COUNT, RULES, GuardrailEvaluation

ALLOW = GuardrailDecision.ALLOW
REQUIRE_APPROVAL = GuardrailDecision.REQUIRE_APPROVAL
DENY = GuardrailDecision.DENY

#: How many rules the engine is expected to run. Read from the rule table rather
#: than typed as a literal, so the count and the table cannot drift apart.
RULE_COUNT = len(RULES)


@pytest.fixture()
def policy_engine(settings: Settings) -> PolicyEngine:
    """Engine bound to the same Settings the context factory hands out."""
    return PolicyEngine(settings)


def evaluation_for(verdict: GuardrailVerdict, rule_id: str) -> GuardrailEvaluation:
    """
    Pull one rule's evaluation out of a verdict.

    Asserting on the named rule -- rather than on the aggregate decision -- is
    what makes these tests diagnostic. If two rules fire, the aggregate cannot
    tell you which one you actually exercised.
    """
    for evaluation in verdict.evaluations:
        if evaluation.rule_id == rule_id:
            return evaluation
    raise AssertionError(
        f"{rule_id} produced no evaluation; rules present: "
        f"{sorted(e.rule_id for e in verdict.evaluations)}"
    )


# ---------------------------------------------------------------------------
# Properties of the rule table itself
# ---------------------------------------------------------------------------


def test_the_documented_thirteen_rules_are_all_registered() -> None:
    """A rule dropped from the table is a guardrail that silently stops running."""
    assert RULE_COUNT == 13, [rule.rule_id for rule in RULES]


def test_rule_ids_are_unique() -> None:
    """
    Duplicate ids would make a verdict ambiguous.

    ``blocking_rules`` and the stored evaluations are keyed by ``rule_id``; if two
    rules shared one, the approval UI would show a reason belonging to a rule that
    did not fire, and this file's own ``evaluation_for`` lookup would silently
    test the wrong rule.
    """
    ids = [rule.rule_id for rule in RULES]
    assert len(set(ids)) == len(ids), ids


def test_every_rule_reports_even_when_nothing_is_wrong(policy_engine, policy_ctx) -> None:
    """
    A passing rule must still produce an evaluation.

    The approval screen renders the full checklist, and an auditor reading a case
    months later needs to see that a rule ran and passed -- not infer it from an
    absence. Short-circuiting on the first failure would break both.
    """
    verdict = policy_engine.evaluate(policy_ctx())
    assert len(verdict.evaluations) == RULE_COUNT


def test_a_clean_case_stops_only_at_the_human(policy_engine, policy_ctx) -> None:
    """
    The shipped configuration never auto-approves.

    This pins the baseline the rest of the file is written against: on a context
    that violates nothing, the only rule that withholds an ALLOW is the human
    approval rule. If a change to the defaults made some other rule fire on a
    clean case, every single-override test below would be testing two rules at
    once, and this test is what catches that.
    """
    verdict = policy_engine.evaluate(policy_ctx())
    assert verdict.decision is REQUIRE_APPROVAL
    assert verdict.blocking_rules == []
    assert verdict.approval_rules == ["R13_HUMAN_APPROVAL"]
    assert verdict.summary


# ---------------------------------------------------------------------------
# R1 -- maximum recovery attempts
# ---------------------------------------------------------------------------


def test_r1_allows_an_attempt_within_the_budget(policy_engine, policy_ctx, settings) -> None:
    verdict = policy_engine.evaluate(policy_ctx(attempt_number=settings.max_recovery_attempts))
    assert evaluation_for(verdict, "R1_MAX_ATTEMPTS").decision is ALLOW


def test_r1_denies_the_attempt_after_the_budget_is_spent(
    policy_engine, policy_ctx, settings
) -> None:
    """Without this the FAILED -> PROPOSED transition would be an infinite retry loop."""
    verdict = policy_engine.evaluate(policy_ctx(attempt_number=settings.max_recovery_attempts + 1))
    evaluation = evaluation_for(verdict, "R1_MAX_ATTEMPTS")
    assert evaluation.decision is DENY
    assert evaluation.passed is False
    assert verdict.decision is DENY


# ---------------------------------------------------------------------------
# R2 -- cooldown between attempts
# ---------------------------------------------------------------------------


def test_r2_allows_a_retry_once_the_cooldown_has_elapsed(
    policy_engine, policy_ctx, settings
) -> None:
    now = utcnow()
    verdict = policy_engine.evaluate(
        policy_ctx(
            now=now,
            last_attempt_at=now - timedelta(seconds=settings.recovery_cooldown_seconds + 1),
        )
    )
    assert evaluation_for(verdict, "R2_COOLDOWN").decision is ALLOW


def test_r2_denies_a_retry_fired_inside_the_cooldown(policy_engine, policy_ctx) -> None:
    """A double-clicked approve, or a retried request, must not become a retry storm."""
    now = utcnow()
    verdict = policy_engine.evaluate(
        policy_ctx(now=now, last_attempt_at=now - timedelta(seconds=5))
    )
    evaluation = evaluation_for(verdict, "R2_COOLDOWN")
    assert evaluation.decision is DENY
    assert evaluation.passed is False


# ---------------------------------------------------------------------------
# R3 -- recoverable failure category
# ---------------------------------------------------------------------------


def test_r3_allows_a_recoverable_category(policy_engine, policy_ctx) -> None:
    verdict = policy_engine.evaluate(
        policy_ctx(failure_category=FailureCategory.GATEWAY_ERROR)
    )
    assert evaluation_for(verdict, "R3_RECOVERABLE_CATEGORY").decision is ALLOW


@pytest.mark.parametrize(
    "category", [FailureCategory.RISK_BLOCKED, FailureCategory.UNKNOWN]
)
def test_r3_denies_a_category_that_must_never_be_re_presented(
    policy_engine, policy_ctx, category
) -> None:
    """
    Re-presenting a risk-blocked instrument helps push a stolen card through, and
    re-presenting an unclassified failure means acting on a reason nobody
    understood. Both are denied at the policy layer even if the planner asked for
    a money-moving strategy anyway.
    """
    verdict = policy_engine.evaluate(policy_ctx(failure_category=category))
    evaluation = evaluation_for(verdict, "R3_RECOVERABLE_CATEGORY")
    assert evaluation.decision is DENY
    assert evaluation.passed is False


# ---------------------------------------------------------------------------
# R4 -- absolute amount ceiling
# ---------------------------------------------------------------------------


def test_r4_allows_an_amount_under_the_ceiling(policy_engine, policy_ctx, settings) -> None:
    verdict = policy_engine.evaluate(policy_ctx())
    assert evaluation_for(verdict, "R4_AMOUNT_CEILING").decision is ALLOW
    assert policy_ctx().amount_paise < settings.max_recovery_amount_paise


def test_r4_denies_an_amount_over_the_ceiling(
    policy_engine, policy_ctx, settings, failed_payment: Payment
) -> None:
    """
    The blast-radius cap on a single recovery.

    The *payment's* amount is raised rather than the context's, so the recovery
    amount still equals the original and R9 stays satisfied -- otherwise this test
    would pass for the wrong reason.
    """
    failed_payment.amount_paise = settings.max_recovery_amount_paise + 1
    verdict = policy_engine.evaluate(policy_ctx())
    assert evaluation_for(verdict, "R4_AMOUNT_CEILING").decision is DENY
    assert evaluation_for(verdict, "R9_AMOUNT_INTEGRITY").decision is ALLOW
    assert verdict.decision is DENY


# ---------------------------------------------------------------------------
# R5 -- high-value review
# ---------------------------------------------------------------------------


def test_r5_allows_an_everyday_amount(policy_engine, policy_ctx, settings) -> None:
    verdict = policy_engine.evaluate(policy_ctx())
    assert policy_ctx().amount_paise < settings.high_value_review_threshold_paise
    assert evaluation_for(verdict, "R5_HIGH_VALUE_REVIEW").decision is ALLOW


def test_r5_forces_review_at_the_high_value_threshold(
    policy_engine, policy_ctx, settings, failed_payment: Payment
) -> None:
    """
    Escalates rather than denies: a large recovery is legitimate, it just may not
    happen unattended. The threshold is inclusive, so the boundary value itself is
    the interesting input.
    """
    failed_payment.amount_paise = settings.high_value_review_threshold_paise
    verdict = policy_engine.evaluate(policy_ctx())
    evaluation = evaluation_for(verdict, "R5_HIGH_VALUE_REVIEW")
    assert evaluation.decision is REQUIRE_APPROVAL
    assert evaluation.passed is False
    assert "R5_HIGH_VALUE_REVIEW" in verdict.approval_rules


# ---------------------------------------------------------------------------
# R6 -- no duplicate open order
# ---------------------------------------------------------------------------


def test_r6_allows_when_no_order_is_open(policy_engine, policy_ctx) -> None:
    verdict = policy_engine.evaluate(policy_ctx(open_attempt_exists=False))
    assert evaluation_for(verdict, "R6_DUPLICATE_ORDER").decision is ALLOW


def test_r6_denies_a_second_order_while_one_is_still_open(policy_engine, policy_ctx) -> None:
    """Two live payment links for one failure is how a customer gets charged twice."""
    verdict = policy_engine.evaluate(policy_ctx(open_attempt_exists=True))
    evaluation = evaluation_for(verdict, "R6_DUPLICATE_ORDER")
    assert evaluation.decision is DENY
    assert evaluation.passed is False


# ---------------------------------------------------------------------------
# R7 -- daily recovery budget
# ---------------------------------------------------------------------------


def test_r7_allows_a_recovery_that_fits_in_the_remaining_budget(
    policy_engine, policy_ctx, settings
) -> None:
    ctx = policy_ctx()
    verdict = policy_engine.evaluate(
        policy_ctx(daily_recovery_total_paise=settings.daily_recovery_budget_paise - ctx.amount_paise)
    )
    assert evaluation_for(verdict, "R7_DAILY_BUDGET").decision is ALLOW


def test_r7_denies_the_recovery_that_would_cross_the_daily_budget(
    policy_engine, policy_ctx, settings
) -> None:
    """
    The system-wide blast radius. The rule adds the pending amount to today's
    total, so the test spends the budget down to one paise short of the amount:
    the cap must be enforced on the *projected* total, not the current one.
    """
    ctx = policy_ctx()
    verdict = policy_engine.evaluate(
        policy_ctx(
            daily_recovery_total_paise=settings.daily_recovery_budget_paise
            - ctx.amount_paise
            + 1
        )
    )
    evaluation = evaluation_for(verdict, "R7_DAILY_BUDGET")
    assert evaluation.decision is DENY
    assert evaluation.passed is False


# ---------------------------------------------------------------------------
# R8 -- per-customer velocity
# ---------------------------------------------------------------------------


def test_r8_allows_a_customer_under_the_daily_case_cap(
    policy_engine, policy_ctx, settings
) -> None:
    verdict = policy_engine.evaluate(
        policy_ctx(customer_cases_today=settings.max_cases_per_customer_per_day - 1)
    )
    assert evaluation_for(verdict, "R8_CUSTOMER_VELOCITY").decision is ALLOW


def test_r8_denies_chasing_the_same_customer_once_more_that_day(
    policy_engine, policy_ctx, settings
) -> None:
    """One unlucky customer having a bad day must not be pursued indefinitely."""
    verdict = policy_engine.evaluate(
        policy_ctx(customer_cases_today=settings.max_cases_per_customer_per_day)
    )
    evaluation = evaluation_for(verdict, "R8_CUSTOMER_VELOCITY")
    assert evaluation.decision is DENY
    assert evaluation.passed is False


# ---------------------------------------------------------------------------
# R9 -- amount integrity (the "the AI cannot change the amount" guarantee)
# ---------------------------------------------------------------------------


def test_r9_allows_the_exact_original_amount(
    policy_engine, policy_ctx, failed_payment: Payment
) -> None:
    verdict = policy_engine.evaluate(policy_ctx(amount_paise=failed_payment.amount_paise))
    assert evaluation_for(verdict, "R9_AMOUNT_INTEGRITY").decision is ALLOW


@pytest.mark.parametrize("delta", [1, -1], ids=["one_paise_more", "one_paise_less"])
def test_r9_denies_a_recovery_that_differs_by_a_single_paise(
    policy_engine, policy_ctx, failed_payment: Payment, delta: int
) -> None:
    """
    The last line of the "the AI cannot set the amount" guarantee.

    The first line is structural: ``AgentRecoveryPlan`` has no amount field and
    forbids extra keys, so a model cannot even express one. This rule is the
    defence against the *other* way a wrong amount arrives -- a service-layer bug
    that computes the recovery amount instead of copying it. A tolerance of even
    one paise here would make that class of bug invisible, so the test asserts on
    the smallest possible divergence in both directions.
    """
    verdict = policy_engine.evaluate(
        policy_ctx(amount_paise=failed_payment.amount_paise + delta)
    )
    evaluation = evaluation_for(verdict, "R9_AMOUNT_INTEGRITY")
    assert evaluation.decision is DENY
    assert evaluation.passed is False
    assert verdict.decision is DENY
    assert "R9_AMOUNT_INTEGRITY" in verdict.blocking_rules


# ---------------------------------------------------------------------------
# R10 -- minimum success likelihood
# ---------------------------------------------------------------------------


def test_r10_allows_a_score_on_the_floor(policy_engine, policy_ctx, settings) -> None:
    verdict = policy_engine.evaluate(policy_ctx(propensity_score=settings.min_propensity_score))
    assert evaluation_for(verdict, "R10_PROPENSITY_FLOOR").decision is ALLOW


def test_r10_denies_a_recovery_the_model_expects_to_fail(policy_engine, policy_ctx) -> None:
    """
    A hopeless retry is not free: it costs a gateway call and, more importantly,
    it puts a failed payment prompt back in front of a customer who has already
    had one bad experience.
    """
    verdict = policy_engine.evaluate(policy_ctx(propensity_score=0.01))
    evaluation = evaluation_for(verdict, "R10_PROPENSITY_FLOOR")
    assert evaluation.decision is DENY
    assert evaluation.passed is False


# ---------------------------------------------------------------------------
# R11 -- payment freshness
# ---------------------------------------------------------------------------


def test_r11_allows_a_payment_inside_the_freshness_window(
    policy_engine, policy_ctx, settings, failed_payment: Payment
) -> None:
    failed_payment.created_at = utcnow() - timedelta(hours=settings.max_payment_age_hours - 1)
    verdict = policy_engine.evaluate(policy_ctx())
    assert evaluation_for(verdict, "R11_PAYMENT_FRESHNESS").decision is ALLOW


def test_r11_denies_a_stale_payment(
    policy_engine, policy_ctx, settings, failed_payment: Payment
) -> None:
    """A week-old cart is a marketing problem, not a payments problem."""
    failed_payment.created_at = utcnow() - timedelta(hours=settings.max_payment_age_hours + 1)
    verdict = policy_engine.evaluate(policy_ctx())
    evaluation = evaluation_for(verdict, "R11_PAYMENT_FRESHNESS")
    assert evaluation.decision is DENY
    assert evaluation.passed is False


# ---------------------------------------------------------------------------
# R12 -- customer risk flag
# ---------------------------------------------------------------------------


def test_r12_allows_an_unflagged_customer(policy_engine, policy_ctx, customer: Customer) -> None:
    assert customer.risk_flagged is False
    verdict = policy_engine.evaluate(policy_ctx())
    assert evaluation_for(verdict, "R12_CUSTOMER_RISK_FLAG").decision is ALLOW


def test_r12_denies_a_customer_the_merchant_has_flagged(
    policy_engine, policy_ctx, customer: Customer
) -> None:
    """
    The merchant's own risk process outranks the agent's recommendation. This is
    the rule that lets a fraud team stop automated recovery for one customer
    without touching the policy configuration.
    """
    customer.risk_flagged = True
    verdict = policy_engine.evaluate(policy_ctx())
    evaluation = evaluation_for(verdict, "R12_CUSTOMER_RISK_FLAG")
    assert evaluation.decision is DENY
    assert evaluation.passed is False


# ---------------------------------------------------------------------------
# R13 -- explicit human approval
# ---------------------------------------------------------------------------


def test_r13_requires_a_human_under_the_shipped_configuration(
    policy_engine, policy_ctx, settings
) -> None:
    """
    The default configuration has no autonomous lane at all: every rupee is
    approved by a person. This test is what stops that default being weakened by
    accident.
    """
    assert settings.require_human_approval is True
    assert settings.auto_approve_enabled is False
    verdict = policy_engine.evaluate(policy_ctx())
    evaluation = evaluation_for(verdict, "R13_HUMAN_APPROVAL")
    assert evaluation.decision is REQUIRE_APPROVAL
    assert evaluation.passed is False


def test_r13_allows_only_when_the_whole_auto_approve_lane_qualifies(
    policy_ctx, settings, failed_payment: Payment
) -> None:
    """
    Graduated autonomy, off by default.

    Every condition of the lane is satisfied here; the tests above already prove
    that the shipped configuration -- which fails the first two conditions --
    still demands a human.
    """
    lane = settings.model_copy(
        update={
            "require_human_approval": False,
            "auto_approve_enabled": True,
            "auto_approve_max_paise": failed_payment.amount_paise,  # inclusive boundary
            "auto_approve_min_propensity": 0.50,
        }
    )
    verdict = PolicyEngine(lane).evaluate(policy_ctx(settings=lane, propensity_score=0.90))
    assert evaluation_for(verdict, "R13_HUMAN_APPROVAL").decision is ALLOW
    assert verdict.decision is ALLOW
    assert verdict.approval_rules == []


# ---------------------------------------------------------------------------
# Engine behaviour
# ---------------------------------------------------------------------------


def test_one_deny_outranks_every_allow_and_every_approval(policy_engine, policy_ctx) -> None:
    """
    Most-restrictive-wins is what makes the engine safe to extend: adding a rule
    can only ever make the system more conservative. A verdict that averaged, or
    that took the last rule's answer, would let a new rule accidentally unblock
    something.
    """
    verdict = policy_engine.evaluate(policy_ctx(open_attempt_exists=True))

    allowed = [e.rule_id for e in verdict.evaluations if e.decision is ALLOW]
    assert len(allowed) == RULE_COUNT - 2  # everything except R6 (deny) and R13 (approval)
    assert verdict.decision is DENY
    assert verdict.blocking_rules == ["R6_DUPLICATE_ORDER"]
    assert "R13_HUMAN_APPROVAL" in verdict.approval_rules


@pytest.mark.parametrize(
    "strategy",
    [s for s in RecoveryStrategy if not s.moves_money],
    ids=lambda s: s.value,
)
def test_a_strategy_that_moves_no_money_clears_every_rule(
    policy_engine, policy_ctx, settings, failed_payment: Payment, strategy
) -> None:
    """
    The short circuit, tested with everything wrong at once.

    "Escalate this to a human" and "do nothing" are not charges, so refusing them
    on the grounds of a spent budget or a stale payment would be nonsense -- the
    case would be stuck with no legal onward transition. Every rule must return
    ALLOW here even though this context violates the attempt budget, the cooldown,
    the ceiling, the daily budget, the velocity cap, the propensity floor, the
    risk flag and amount integrity simultaneously.
    """
    now = utcnow()
    failed_payment.created_at = now - timedelta(hours=settings.max_payment_age_hours + 1)
    verdict = policy_engine.evaluate(
        policy_ctx(
            strategy=strategy,
            failure_category=FailureCategory.RISK_BLOCKED,
            attempt_number=settings.max_recovery_attempts + 5,
            now=now,
            last_attempt_at=now,
            open_attempt_exists=True,
            daily_recovery_total_paise=settings.daily_recovery_budget_paise,
            customer_cases_today=settings.max_cases_per_customer_per_day + 1,
            propensity_score=0.0,
            amount_paise=failed_payment.amount_paise + 100,
        )
    )

    assert verdict.decision is ALLOW
    assert verdict.blocking_rules == []
    assert verdict.approval_rules == []
    assert len(verdict.evaluations) == RULE_COUNT
    assert all(e.decision is ALLOW and e.passed for e in verdict.evaluations)


def test_policy_snapshot_survives_a_json_round_trip(policy_engine) -> None:
    """
    The snapshot is written into a JSON column on every case so that a later
    change to the configuration cannot silently rewrite the meaning of an old
    decision. If it contained a Path, an Enum member or a Decimal, the insert
    would fail at the moment a case is created -- that is, in production, on the
    money path, and not here.
    """
    snapshot = policy_engine.policy_snapshot()
    assert snapshot, "an empty snapshot records nothing about the policy in force"
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_rule_catalogue_describes_every_registered_rule(policy_engine) -> None:
    """
    The catalogue is what ``GET /api/policy`` renders. A rule missing from it is a
    limit the merchant is being held to but cannot read.
    """
    catalogue = policy_engine.rule_catalogue()
    assert {entry["rule_id"] for entry in catalogue} == {rule.rule_id for rule in RULES}
    assert all(entry["name"] and entry["description"] for entry in catalogue)


def test_a_non_money_strategy_marks_every_rule_not_applicable(policy_ctx) -> None:
    """
    A strategy that creates no payment attempt must not render as thirteen
    passing checks.

    The engine short-circuits to ALLOW, which is correct -- no rule objected. But
    ALLOW plus ``passed=True`` is indistinguishable on the wire from "we examined
    this and cleared it", and for a fraud case that reading is actively wrong: the
    rules were never consulted. ``applicable=False`` is what the approval screen
    keys off to grey those rows out, so this test pins the flag rather than
    trusting a human-readable reason string that anyone might reword.
    """
    ctx = policy_ctx(strategy=RecoveryStrategy.NO_RECOVERY)
    verdict = PolicyEngine(ctx.settings).evaluate(ctx)

    assert verdict.decision is GuardrailDecision.ALLOW
    assert len(verdict.evaluations) == RULE_COUNT
    assert all(not e.applicable for e in verdict.evaluations)
    assert all(e.reason == NOT_APPLICABLE_REASON for e in verdict.evaluations)


def test_a_money_moving_strategy_marks_every_rule_applicable(policy_ctx) -> None:
    """The complement: a real recovery must show checks that were actually run."""
    ctx = policy_ctx(strategy=RecoveryStrategy.SWITCH_TO_UPI)
    verdict = PolicyEngine(ctx.settings).evaluate(ctx)

    assert all(e.applicable for e in verdict.evaluations)
