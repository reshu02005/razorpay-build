"""
End-to-end properties of the recovery lifecycle, exercised through the service
layer rather than through HTTP.

Every test here passes ``force_rule_based=True``. That is not a convenience: it
is what makes the suite runnable on a machine with no Gemini key and no network,
which is the configuration a reviewer will actually have. The LLM path produces
an ``AgentRecoveryPlan`` and nothing else -- it feeds the same guardrails, the
same state machine and the same gateway -- so pinning the deterministic planner
removes a network dependency without removing any of the behaviour these tests
are about.

The properties under test are the ones that decide whether an autonomous agent is
safe to point at a payment system: money moves only after a human said yes,
approval is re-checked against the world as it is at approval time rather than as
it was at proposal time, a replayed approval charges once, the attempt budget is
real, and nothing is ever marked recovered on a customer's say-so.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.llm import GeminiClient, LLMUnavailable
from app.agent.orchestrator import RecoveryAgent
from app.audit.ledger import AuditLedger
from app.config import Settings
from app.db.models import AuditEvent, Customer, Payment, RecoveryAttempt
from app.domain.enums import (
    AuditEventType,
    GuardrailDecision,
    RecoveryStrategy,
    AgentMode,
    FailureCategory,
    PaymentStatus,
    RecoveryStatus,
)
from app.domain.errors import (
    GuardrailDenied,
    InvalidStateTransition,
    SignatureVerificationError,
)
from app.domain.schemas import (
    AgentRecoveryPlan,
    AnalyzeIn,
    ApproveIn,
    MarkAttemptFailedIn,
    VerifyPaymentIn,
)
from app.payments.gateway import get_gateway
from app.services.recovery_service import RecoveryService

RULE_BASED = AnalyzeIn(force_rule_based=True)
OPERATOR = "ops@merchant.test"


@pytest.fixture()
def service(db: Session, settings: Settings) -> RecoveryService:
    return RecoveryService(db, settings)


def attempts_for(db: Session, case_id: str) -> list[RecoveryAttempt]:
    return db.query(RecoveryAttempt).filter(RecoveryAttempt.case_id == case_id).all()


# ---------------------------------------------------------------------------
# The path the product exists for
# ---------------------------------------------------------------------------


def test_a_failed_payment_walks_all_the_way_to_recovered(
    db: Session, service: RecoveryService, failed_payment: Payment, customer: Customer
) -> None:
    """
    The whole product in one test: a failure is analysed, waits for a human,
    becomes a payable order once approved, and is only counted as revenue after
    the payment comes back verified.

    The assertions that matter are the ones about money. The case amount is copied
    from the original payment, the recovery lands as a ``Payment`` row pointing at
    its parent -- so a merchant summing the payments table cannot miss recovered
    revenue -- and the customer's history is updated, because the next prediction
    for this customer depends on it.
    """
    payments_before = customer.total_payments
    successes_before = customer.successful_payments

    case = service.analyze_payment(failed_payment.id, RULE_BASED)
    assert case.status is RecoveryStatus.AWAITING_APPROVAL
    assert case.agent_mode is AgentMode.RULE_BASED
    assert case.amount_paise == failed_payment.amount_paise
    assert case.can_approve is True
    assert attempts_for(db, case.id) == [], "no order may exist before a human approves"

    approved = service.approve(case.id, ApproveIn(approved_by=OPERATOR))
    assert approved.status is RecoveryStatus.AWAITING_PAYMENT
    assert approved.approved_by == OPERATOR

    attempts = attempts_for(db, case.id)
    assert len(attempts) == 1
    assert attempts[0].amount_paise == failed_payment.amount_paise
    assert attempts[0].razorpay_order_id
    # The idempotency key is derived, not random: that is what makes a replayed
    # approval collide with the existing row instead of creating a second order.
    assert attempts[0].idempotency_key == f"{case.id}:{attempts[0].attempt_number}"

    recovered = service.simulate_checkout(case.id, succeed=True)
    assert recovered.status is RecoveryStatus.RECOVERED
    assert recovered.recovered_amount_paise == failed_payment.amount_paise
    assert recovered.recovered_at is not None

    recovery_payment = (
        db.query(Payment).filter(Payment.parent_payment_id == failed_payment.id).one()
    )
    assert recovery_payment.is_recovery_attempt is True
    assert recovery_payment.amount_paise == failed_payment.amount_paise
    assert recovery_payment.status == PaymentStatus.CAPTURED.value

    assert customer.total_payments == payments_before + 1
    assert customer.successful_payments == successes_before + 1


# ---------------------------------------------------------------------------
# Replay and budget
# ---------------------------------------------------------------------------


def test_approving_twice_never_creates_a_second_order(
    db: Session, service: RecoveryService, failed_payment: Payment
) -> None:
    """
    A double-clicked approve button, a retried HTTP request and a duplicated
    webhook are all the same event arriving twice.

    The invariant is about the customer, not about the response: they must never
    be handed two live payment links for one failure. Whether the second call
    returns the unchanged case or refuses it outright is an interface choice --
    the case is no longer awaiting approval either way -- so the test accepts both
    and asserts on the thing that would actually cost money.
    """
    case = service.analyze_payment(failed_payment.id, RULE_BASED)
    service.approve(case.id, ApproveIn(approved_by=OPERATOR))

    try:
        service.approve(case.id, ApproveIn(approved_by=OPERATOR))
    except InvalidStateTransition:
        pass

    attempts = attempts_for(db, case.id)
    assert len(attempts) == 1
    assert len({attempt.razorpay_order_id for attempt in attempts}) == 1


def test_the_attempt_budget_is_exhausted_and_then_enforced(
    db: Session, settings: Settings, make_failed_payment
) -> None:
    """
    The stop condition on the FAILED -> PROPOSED loop.

    The state machine deliberately allows a failed case to be re-proposed; the
    only thing preventing an infinite chase is ``R1_MAX_ATTEMPTS``. This test
    spends the entire budget through real approvals and failures, then asserts
    that the next analysis is blocked and, crucially, that no attempt row was
    created for it -- a case can be marked blocked and still have created an order
    first, which would be the expensive version of this bug.

    The cooldown is set to zero because otherwise ``R2_COOLDOWN`` would deny the
    second round first and the test would pass without ever reaching the rule it
    is named after.
    """
    policy = settings.model_copy(update={"recovery_cooldown_seconds": 0})
    service = RecoveryService(db, policy)
    payment = make_failed_payment()

    for round_number in range(policy.max_recovery_attempts):
        case = service.analyze_payment(payment.id, RULE_BASED)
        assert case.status is RecoveryStatus.AWAITING_APPROVAL, f"round {round_number}"
        service.approve(case.id, ApproveIn(approved_by=OPERATOR))
        failed = service.mark_attempt_failed(
            case.id, MarkAttemptFailedIn(reason="Customer did not complete the payment")
        )
        assert failed.status is RecoveryStatus.FAILED

    attempts_before = db.query(RecoveryAttempt).count()
    assert attempts_before == policy.max_recovery_attempts

    blocked = service.analyze_payment(payment.id, RULE_BASED)
    assert blocked.status is RecoveryStatus.BLOCKED

    fired = {e.rule_id for e in blocked.guardrail_evaluations if not e.passed}
    assert "R1_MAX_ATTEMPTS" in fired
    assert "R2_COOLDOWN" not in fired, "the cooldown was disabled; R1 must be the rule that fired"
    assert db.query(RecoveryAttempt).count() == attempts_before


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_risk_blocked_failure_never_opens_an_approval_path(
    db: Session, service: RecoveryService, make_failed_payment
) -> None:
    """
    The category where being wrong helps someone use a stolen instrument.

    The case must never reach a state where a human could approve it -- an
    operator clicking through a queue is not a control if the queue contains
    things that should never have been offered. The exact terminal state is not
    pinned: the planner downgrades an unrecoverable failure to a strategy that
    moves no money, which routes the case to escalation rather than to a guardrail
    block. Both are correct outcomes; opening an approval is not.
    """
    payment = make_failed_payment(category=FailureCategory.RISK_BLOCKED)
    case = service.analyze_payment(payment.id, RULE_BASED)

    assert case.status is not RecoveryStatus.AWAITING_APPROVAL
    assert case.status.is_terminal
    assert case.strategy.moves_money is False
    assert case.can_approve is False
    assert attempts_for(db, case.id) == []


def test_approving_a_case_that_is_not_awaiting_approval_is_refused(
    service: RecoveryService, make_failed_payment
) -> None:
    """
    Every state change goes through one validated transition table, so approving a
    terminal case raises rather than resurrecting it.

    Without this, a stale browser tab -- or a replayed request against a case that
    was already rejected, blocked or expired -- could restart the money path on a
    decision that had already been made.
    """
    payment = make_failed_payment(category=FailureCategory.RISK_BLOCKED)
    case = service.analyze_payment(payment.id, RULE_BASED)
    assert case.status is not RecoveryStatus.AWAITING_APPROVAL

    with pytest.raises(InvalidStateTransition):
        service.approve(case.id, ApproveIn(approved_by=OPERATOR))


def test_guardrails_are_re_evaluated_against_the_world_at_approval_time(
    db: Session, service: RecoveryService, failed_payment: Payment, customer: Customer
) -> None:
    """
    An approval screen can sit open for an hour. In that hour budgets move,
    attempts accumulate, links expire and -- as here -- a risk team flags the
    customer.

    Trusting the verdict computed at proposal time would mean the guardrails
    describe a world that no longer exists, which is the single most likely way
    this system would let something through that it had already decided to stop.
    The case must land in BLOCKED with no order created.
    """
    case = service.analyze_payment(failed_payment.id, RULE_BASED)
    assert case.status is RecoveryStatus.AWAITING_APPROVAL

    customer.risk_flagged = True
    db.commit()

    with pytest.raises(GuardrailDenied):
        service.approve(case.id, ApproveIn(approved_by=OPERATOR))

    assert service.get_case(case.id).status == RecoveryStatus.BLOCKED.value
    assert attempts_for(db, case.id) == []


def test_an_unverified_signature_cannot_mark_a_case_recovered(
    service: RecoveryService, failed_payment: Payment
) -> None:
    """
    "The payment succeeded" is a claim made by the customer's browser.

    If the server took that at face value, anyone could mark any case recovered
    with a single POST, and the merchant's recovered-revenue figure would be
    whatever the internet decided it was. The case has to stay unrecovered.
    """
    case = service.analyze_payment(failed_payment.id, RULE_BASED)
    service.approve(case.id, ApproveIn(approved_by=OPERATOR))
    checkout = service.checkout_session(case.id)

    with pytest.raises(SignatureVerificationError):
        service.verify_payment(
            case.id,
            VerifyPaymentIn(
                razorpay_order_id=checkout.order_id,
                razorpay_payment_id="pay_forged_00000001",
                razorpay_signature="0" * 64,
            ),
        )

    after = service.to_out(service.get_case(case.id))
    assert after.status is not RecoveryStatus.RECOVERED
    assert after.recovered_amount_paise == 0


def test_a_verified_signature_is_what_marks_a_case_recovered(
    service: RecoveryService, settings: Settings, failed_payment: Payment
) -> None:
    """
    The positive counterpart to the test above -- without it, a ``verify_payment``
    that rejected everything would look perfectly healthy.
    """
    case = service.analyze_payment(failed_payment.id, RULE_BASED)
    service.approve(case.id, ApproveIn(approved_by=OPERATOR))
    checkout = service.checkout_session(case.id)

    # The amount is passed in rather than looked up in the gateway's memory, so
    # this works against any instance -- including one that has just been
    # restarted and has no record of the order.
    payment_id, signature = get_gateway(settings).simulate_payment(
        checkout.order_id, amount_paise=checkout.amount_paise, succeed=True
    )
    recovered = service.verify_payment(
        case.id,
        VerifyPaymentIn(
            razorpay_order_id=checkout.order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=signature,
        ),
    )

    assert recovered.status is RecoveryStatus.RECOVERED
    assert recovered.recovered_amount_paise == failed_payment.amount_paise


def test_the_guardrails_stop_an_agent_that_recommends_recovering_a_fraud_block(
    db: Session,
    service: RecoveryService,
    make_failed_payment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The central claim of this project, tested against a *misbehaving* agent.

    Every other end-to-end test exercises the deterministic planner, which is
    sensible: it returns ``NO_RECOVERY`` for a risk-blocked failure, so the case
    never reaches an approval path. That proves the planner is well written. It
    does **not** prove what the project actually claims, which is that the
    guardrails catch the agent when the agent is wrong -- and a suite that only
    ever feeds itself good plans is a suite that has never tested its own thesis.

    So the agent is replaced with one that returns exactly what a jailbroken,
    prompt-injected or simply hallucinating model might: a confident, well-formed
    recommendation to re-present a payment the issuer's risk engine blocked.
    Nothing about the plan is malformed -- schema validation would have caught
    that. It is a *plausible* plan that happens to be dangerous, which is the only
    interesting case.

    The assertions are the guarantee: R3 denies, the case is BLOCKED, no attempt
    row exists, no order was created, and the refusal is on the ledger.
    """
    payment = make_failed_payment(category=FailureCategory.RISK_BLOCKED)

    dangerous = AgentRecoveryPlan(
        failure_category=FailureCategory.RISK_BLOCKED,
        confidence=0.95,
        strategy=RecoveryStrategy.SWITCH_TO_UPI,
        rationale=(
            "The card was blocked but the customer has a strong payment history, "
            "so routing this through UPI should complete without issue."
        ),
        customer_message="Please complete your payment using UPI instead.",
        evidence=["customer has 8 of 10 prior payments successful"],
    )

    real_analyze = RecoveryAgent.analyze

    def hijacked(self, payment, *, force_rule_based=False):  # type: ignore[no-untyped-def]
        result = real_analyze(self, payment, force_rule_based=force_rule_based)
        # Keep the genuine taxonomy, propensity and run metadata; swap only the
        # conclusion, so everything downstream sees a realistic analysis that
        # happens to end in a dangerous recommendation.
        return replace(result, plan=dangerous)

    monkeypatch.setattr(RecoveryAgent, "analyze", hijacked)

    case = service.analyze_payment(payment.id, RULE_BASED)

    assert case.strategy is RecoveryStrategy.SWITCH_TO_UPI, (
        "the agent's recommendation should be recorded verbatim, not silently rewritten"
    )
    assert case.guardrail_decision is GuardrailDecision.DENY
    assert case.status is RecoveryStatus.BLOCKED
    assert case.can_approve is False

    denied = [e for e in case.guardrail_evaluations if e.decision is GuardrailDecision.DENY]
    assert any(e.rule_id == "R3_RECOVERABLE_CATEGORY" for e in denied), (
        f"R3 should be the rule that refuses; denials were {[e.rule_id for e in denied]}"
    )

    assert attempts_for(db, case.id) == [], (
        "a denied case must never produce a payment attempt"
    )

    # And approving it is refused too -- the block is not merely a UI state.
    #
    # The refusal is `InvalidStateTransition`, not `GuardrailDenied`, and that is
    # the stronger outcome: the state machine turns the request away before the
    # guardrails are even consulted. Two independent mechanisms would each have
    # stopped it, which is what defence in depth is supposed to look like on the
    # one invariant worth spending it on.
    with pytest.raises(InvalidStateTransition):
        service.approve(case.id, ApproveIn(approved_by=OPERATOR))

    assert AuditLedger(db).verify_chain().valid, (
        "the refusal and everything around it must leave the ledger intact"
    )


def test_an_llm_failure_mid_run_degrades_to_the_rule_planner(
    db: Session,
    service: RecoveryService,
    make_failed_payment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The resilience claim, tested against a model that fails rather than one absent.

    Every other test in this suite passes ``force_rule_based=True``, so the whole
    suite exercises the fallback while never exercising the thing that falls back.
    That leaves the documented promise -- "a recovery pipeline that stops working
    when a third-party model API has an outage is not a pipeline" -- resting on a
    code path no test enters. Coverage of ``app/agent/llm.py`` is about a third,
    and the tool loop is the untouched part.

    This stubs the client to look available and then fail the way a real outage
    does: partway through, after the run has started. The contract asserted here
    is what the merchant is actually promised -- a usable plan, the case still
    moving, the degradation named rather than swallowed, and the partial trace
    kept as evidence of what was attempted.
    """
    payment = make_failed_payment(category=FailureCategory.GATEWAY_ERROR)

    monkeypatch.setattr(GeminiClient, "available", property(lambda self: True))
    monkeypatch.setattr(GeminiClient, "model", "gemini-stub", raising=False)

    def exploding_loop(self, **kwargs):  # type: ignore[no-untyped-def]
        raise LLMUnavailable("upstream returned 503 after 2 tool calls")

    monkeypatch.setattr(GeminiClient, "run_tool_loop", exploding_loop)

    case = service.analyze_payment(payment.id, AnalyzeIn(force_rule_based=False))

    assert case.agent_mode is AgentMode.RULE_BASED, (
        "an LLM outage must not leave the case without a plan"
    )
    assert case.status is RecoveryStatus.AWAITING_APPROVAL
    assert case.strategy.moves_money, "the deterministic planner should still propose a recovery"
    assert case.agent_rationale, "a degraded run must still explain itself"

    trace = service.trace(case.id)
    assert trace, "the degraded run must leave a trace, not an empty one"

    degraded = [
        event
        for event in db.execute(select(AuditEvent)).scalars()
        if event.event_type == AuditEventType.AGENT_DEGRADED.value
    ]
    assert degraded, "the degradation must be on the ledger, not only in a log line"
    assert "503" in str(degraded[0].payload) or "503" in degraded[0].summary, (
        "the recorded reason should carry what actually went wrong"
    )
