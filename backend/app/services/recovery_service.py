"""
The recovery case lifecycle: analysis, guardrails, human approval, money, proof.

This is where the product's sentence -- *AI decides, guardrails control, Razorpay
executes, audit trail proves* -- is actually assembled. Every other module
contributes one clause: ``app/agent`` decides, ``app/policy`` controls,
``app/payments`` executes, ``app/audit`` proves. Nothing but this module is
allowed to put them in order, because the order is the safety property.

Three rules govern everything below.

**The service owns the transaction.** ``AuditLedger.record`` and the agent
orchestrator both flush and neither commits. Each public method here commits
exactly once, at the end. A case therefore cannot move without its ledger entry,
and a ledger entry cannot exist for a move that did not happen -- if either write
fails, the whole unit of work rolls back. That atomicity is the entire reason the
audit trail is worth reading; a ledger that can drift from the data it describes
is a log file with extra steps.

**Money never comes from the model.** ``case.amount_paise`` is copied from the
original ``Payment`` row, every time, and ``AgentRecoveryPlan`` has no amount
field to copy from even if someone wanted to. R9_AMOUNT_INTEGRITY re-checks it
independently at approval time.

**Guardrails bind at approval, not at proposal.** See ``approve``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.orchestrator import RecoveryAgent
from app.audit.ledger import AuditLedger
from app.config import Settings
from app.db.models import (
    AgentToolCall,
    AuditEvent,
    Customer,
    Payment,
    RecoveryAttempt,
    RecoveryCase,
    utcnow,
)
from app.domain.enums import (
    OPEN_ATTEMPT_STATUSES,
    ALLOWED_TRANSITIONS,
    ActorType,
    AgentMode,
    AuditEventType,
    FailureCategory,
    GatewayMode,
    GuardrailDecision,
    PaymentMethod,
    PaymentStatus,
    RecoveryStatus,
    RecoveryStrategy,
)
from app.domain.errors import (
    ApprovalRequired,
    ConfigurationError,
    DuplicateCaseError,
    GatewayError,
    GuardrailDenied,
    InvalidStateTransition,
    NotFoundError,
    SignatureVerificationError,
)
from app.domain.schemas import (
    AgentToolCallOut,
    AnalyzeIn,
    ApproveIn,
    CheckoutSessionOut,
    GuardrailEvaluationOut,
    MarkAttemptFailedIn,
    RecoveryAttemptOut,
    RecoveryCaseOut,
    RecoveryCaseSummaryOut,
    RejectIn,
    VerifyPaymentIn,
    paise_to_rupees,
)
from app.payments.gateway import get_gateway
from app.policy.engine import PolicyEngine, GuardrailVerdict
from app.policy.rules import GuardrailContext
from app.services.payment_service import PaymentService, as_utc, customer_to_out, utc_day_start

logger = logging.getLogger(__name__)

#: Attempt states that mean "an order is live and nobody has paid it yet". These
#: are what R6_DUPLICATE_ORDER counts, scoped to a single case: two live orders
#: for one failed payment is the shape a double charge takes.

#: Which instrument a recovered payment is recorded against. A recovery that
#: succeeded by switching rails did not happen on the original instrument, and
#: recording it as if it had would make "which method recovers best?" -- the
#: question the whole strategy taxonomy exists to answer -- unanswerable from the
#: payments table. ``None`` means "whatever the original payment used".
STRATEGY_METHOD: dict[RecoveryStrategy, PaymentMethod | None] = {
    RecoveryStrategy.SWITCH_TO_UPI: PaymentMethod.UPI,
    RecoveryStrategy.SWITCH_TO_CARD: PaymentMethod.CARD,
    RecoveryStrategy.SWITCH_TO_NETBANKING: PaymentMethod.NETBANKING,
    RecoveryStrategy.RETRY_SAME_METHOD: None,
}


def _humanise_seconds(seconds: int) -> str:
    """
    Render a duration the way a person would say it ("15 minutes", "2 hours").

    Used in operator-facing copy. The cooldown is configurable, so the sentence
    has to be derived from the setting rather than written out -- a hard-coded
    "15 minutes" would quietly start lying the moment somebody changed it.
    """
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"

    minutes, leftover_seconds = divmod(seconds, 60)
    if minutes < 60:
        # A remainder is kept rather than rounded away: the sentence tells an
        # operator when they may retry, and a value rounded down would invite a
        # click that the guardrail then refuses.
        if leftover_seconds:
            return f"{minutes}m {leftover_seconds}s"
        return f"{minutes} minute{'' if minutes == 1 else 's'}"

    hours, leftover_minutes = divmod(minutes, 60)
    if leftover_minutes:
        return f"{hours}h {leftover_minutes}m"
    return f"{hours} hour{'' if hours == 1 else 's'}"


class RecoveryService:
    """
    Orchestrates one failed payment from analysis to a proven outcome.

    Constructed per request with the session it will commit. Collaborators are
    built here rather than injected because they are all deterministic functions
    of ``(db, settings)``; the seam worth having for tests is the session and the
    settings object, and both are parameters.
    """

    def __init__(self, db: Session, settings: Settings) -> None:
        """
        Args:
            db: Request-scoped session. This service commits it; nothing beneath
                this layer does.
            settings: The policy and credential configuration in force.
        """
        self.db = db
        self.settings = settings
        self.ledger = AuditLedger(db)
        self.policy = PolicyEngine(settings)
        self.agent = RecoveryAgent(db, settings)
        self.gateway = get_gateway(settings)
        self.payments = PaymentService(db)

    # =====================================================================
    # Analysis
    # =====================================================================

    def analyze_payment(self, payment_id: str, body: AnalyzeIn) -> RecoveryCaseOut:
        """
        Analyse a failed payment and open (or re-open) its recovery case.

        Args:
            payment_id: The failed payment to analyse.
            body: ``force_rule_based`` forces the deterministic planner even when
                a Gemini key is configured.

        Returns:
            The case, routed to one of ``NO_ACTION``, ``ESCALATED``, ``BLOCKED``
            or ``AWAITING_APPROVAL``.

        Raises:
            NotFoundError: No such payment.
            InvalidStateTransition: The payment did not fail.
            DuplicateCaseError: A case for this payment is already finished, or
                already in flight.

        No money moves here. The output of this method is a *recommendation* plus
        the guardrail verdict on it; the furthest it can advance a case is to
        "waiting for a human to say yes".
        """
        payment = self.payments.get_payment(payment_id)

        # You cannot recover money that was collected. Anything other than a
        # failed payment is a category error, not a policy question, so it is
        # refused before the agent is ever asked to reason about it.
        if payment.status != PaymentStatus.FAILED.value:
            raise InvalidStateTransition(
                f"Payment {payment.id} is '{payment.status}', not 'failed'. "
                "Only a failed payment can be recovered.",
                detail={"payment_id": payment.id, "status": payment.status},
            )

        customer = payment.customer
        case = self._open_or_reuse_case(payment)

        self.ledger.record(
            event_type=AuditEventType.ANALYSIS_STARTED,
            actor_type=ActorType.SYSTEM,
            case_id=case.id,
            payment_id=payment.id,
            summary=f"Started recovery analysis for payment {payment.id}.",
            payload={
                "payment_id": payment.id,
                "attempt_count_so_far": case.attempt_count,
                "force_rule_based": body.force_rule_based,
            },
        )

        result = self.agent.analyze(payment, force_rule_based=body.force_rule_based)
        plan, run, propensity = result.plan, result.run, result.propensity
        agent_actor = run.model or run.mode.value

        # The orchestrator analyses a *payment* and knows nothing about cases, so
        # it cannot stamp its trace rows with a case id. The service owns that
        # relationship and writes it here, which is what lets the explainability
        # panel fetch a trace by case in a single indexed query.
        self._link_trace_to_case(run.run_id, case)

        self.ledger.record(
            event_type=AuditEventType.FAILURE_CLASSIFIED,
            actor_type=ActorType.AGENT,
            actor_id=agent_actor,
            case_id=case.id,
            payment_id=payment.id,
            summary=(
                f"Classified the failure as '{plan.failure_category.value}' "
                f"with {plan.confidence:.0%} confidence."
            ),
            payload={
                "failure_category": plan.failure_category.value,
                "confidence": plan.confidence,
                "evidence": list(plan.evidence),
                # The taxonomy's own opinion is kept alongside the agent's so a
                # reviewer can see whether the model followed the deterministic
                # classifier or overrode it.
                "taxonomy_category": result.taxonomy.category.value,
                "taxonomy_confidence": result.taxonomy.confidence,
                "taxonomy_matched_on": result.taxonomy.matched_on,
            },
        )
        self.ledger.record(
            event_type=AuditEventType.PROPENSITY_SCORED,
            actor_type=ActorType.AGENT,
            actor_id=propensity.model_version,
            case_id=case.id,
            payment_id=payment.id,
            summary=f"Predicted a {propensity.score:.0%} chance that a recovery attempt succeeds.",
            payload={
                "score": propensity.score,
                "model_version": propensity.model_version,
                # There is no column for this on the case row, and adding a
                # parallel field to models.py would create a second place the
                # truth could live. The ledger is the record of what happened, so
                # the ledger is where to_out() reads it back from.
                "is_fallback": propensity.is_fallback,
                "top_factors": list(propensity.top_factors),
            },
        )
        self.ledger.record(
            event_type=AuditEventType.STRATEGY_PROPOSED,
            actor_type=ActorType.AGENT,
            actor_id=agent_actor,
            case_id=case.id,
            payment_id=payment.id,
            summary=f"Proposed recovery strategy '{plan.strategy.value}'.",
            payload={
                "strategy": plan.strategy.value,
                "rationale": plan.rationale,
                "customer_message": plan.customer_message,
                "moves_money": plan.strategy.moves_money,
                "agent_mode": run.mode.value,
                "steps": run.steps,
                "total_latency_ms": run.total_latency_ms,
            },
        )

        if run.degraded_reason:
            self.ledger.record(
                event_type=AuditEventType.AGENT_DEGRADED,
                actor_type=ActorType.SYSTEM,
                case_id=case.id,
                payment_id=payment.id,
                summary=(
                    "The language model was unavailable; the deterministic rule "
                    "planner produced this recommendation instead."
                ),
                payload={"degraded_reason": run.degraded_reason, "agent_mode": run.mode.value},
            )

        # Record the agent's conclusions. The amount is pointedly absent from
        # this block: it was set from the payment when the case row was created
        # and is never rewritten from anything the agent produced.
        case.failure_category = plan.failure_category.value
        case.classification_confidence = plan.confidence
        case.strategy = plan.strategy.value
        case.agent_rationale = plan.rationale
        case.customer_message = plan.customer_message
        case.agent_mode = run.mode.value
        case.propensity_score = propensity.score
        case.propensity_model_version = propensity.model_version

        now = utcnow()
        verdict = self.policy.evaluate(
            self._guardrail_context(
                case=case,
                payment=payment,
                customer=customer,
                strategy=plan.strategy,
                failure_category=plan.failure_category,
                propensity_score=propensity.score,
                attempt_number=case.attempt_count + 1,
                now=now,
            )
        )
        self._persist_verdict(case, verdict)

        next_status = self._route(plan.strategy, verdict.decision)

        self.ledger.record(
            event_type=AuditEventType.GUARDRAILS_EVALUATED,
            actor_type=ActorType.SYSTEM,
            case_id=case.id,
            payment_id=payment.id,
            summary=verdict.summary,
            payload={
                "phase": "proposal",
                "decision": verdict.decision.value,
                "blocking_rules": list(verdict.blocking_rules),
                "approval_rules": list(verdict.approval_rules),
                "evaluations": case.guardrail_evaluations,
                # NO_ACTION and ESCALATED have no dedicated event type -- the
                # AuditEventType set is closed on purpose -- so the routing
                # decision is recorded here, inside the hashed payload.
                "resulting_status": next_status.value,
            },
        )

        self._transition(case, next_status)

        if next_status is RecoveryStatus.BLOCKED:
            self.ledger.record(
                event_type=AuditEventType.RECOVERY_BLOCKED,
                actor_type=ActorType.SYSTEM,
                case_id=case.id,
                payment_id=payment.id,
                summary=verdict.summary,
                payload={"blocking_rules": list(verdict.blocking_rules), "phase": "proposal"},
            )
        elif next_status is RecoveryStatus.AWAITING_APPROVAL:
            self.ledger.record(
                event_type=AuditEventType.APPROVAL_REQUESTED,
                actor_type=ActorType.SYSTEM,
                case_id=case.id,
                payment_id=payment.id,
                summary=(
                    f"Awaiting operator approval to recover "
                    f"Rs {paise_to_rupees(case.amount_paise):,.2f} via '{plan.strategy.value}'."
                ),
                payload={
                    "amount_paise": case.amount_paise,
                    "strategy": plan.strategy.value,
                    "approval_rules": list(verdict.approval_rules),
                },
            )

        self.db.commit()
        logger.info("Case %s analysed; routed to %s", case.id, next_status.value)
        return self.to_out(case)

    # =====================================================================
    # Reads
    # =====================================================================

    def list_cases(self, *, status: str = "all", limit: int = 100) -> list[RecoveryCaseSummaryOut]:
        """
        Return recovery cases, newest first.

        Args:
            status: A ``RecoveryStatus`` value, or ``"all"``.
            limit: Maximum rows.

        Returns:
            Light summary rows -- no trace, no evaluations, no nested payment.

        The customer name is joined in the same query rather than read through the
        relationship per row, because the recovery queue is the dashboard's
        busiest list and a name lookup per case is an N+1 waiting to happen.

        Sweeps expired links first -- see :meth:`expire_stale_cases` for why the
        sweep is driven from reads rather than from a background task.
        """
        self.expire_stale_cases()

        stmt = (
            select(RecoveryCase, Customer.name)
            .join(Customer, Customer.id == RecoveryCase.customer_id)
            .order_by(RecoveryCase.created_at.desc())
            .limit(limit)
        )
        if status != "all":
            stmt = stmt.where(RecoveryCase.status == status)

        return [
            RecoveryCaseSummaryOut(
                id=case.id,
                original_payment_id=case.original_payment_id,
                customer_id=case.customer_id,
                customer_name=customer_name,
                status=RecoveryStatus(case.status),
                failure_category=FailureCategory(case.failure_category),
                strategy=RecoveryStrategy(case.strategy),
                guardrail_decision=GuardrailDecision(case.guardrail_decision),
                propensity_score=case.propensity_score,
                agent_mode=AgentMode(case.agent_mode),
                amount_paise=case.amount_paise,
                amount_rupees=paise_to_rupees(case.amount_paise),
                attempt_count=case.attempt_count,
                created_at=as_utc(case.created_at),
                updated_at=as_utc(case.updated_at),
            )
            for case, customer_name in self.db.execute(stmt).all()
        ]

    def get_case(self, case_id: str) -> RecoveryCase:
        """
        Load one case.

        Args:
            case_id: The case id.

        Returns:
            The ORM row.

        Raises:
            NotFoundError: When no such case exists.
        """
        case = self.db.get(RecoveryCase, case_id)
        if case is None:
            raise NotFoundError(f"No recovery case with id '{case_id}'.", detail={"case_id": case_id})
        return case

    def to_out(self, case: RecoveryCase) -> RecoveryCaseOut:
        """
        Render a case as the full detail schema the approval screen consumes.

        Args:
            case: The ORM row.

        Returns:
            ``RecoveryCaseOut``, including the server-computed ``can_approve``,
            ``can_reject`` and ``approval_blocked_reason``.

        Those three fields are computed here, not in React. Deciding whether the
        Approve button is live means knowing the state machine and the guardrail
        verdict; implementing that in the frontend would fork the policy into a
        second copy that drifts the first time a rule changes, and the copy a
        user can see is not the copy that guards the money. The server decides;
        the button renders.
        """
        status = RecoveryStatus(case.status)
        decision = GuardrailDecision(case.guardrail_decision)
        payment = self._original_payment(case)
        customer = self._customer(case)

        can_reject = status is RecoveryStatus.AWAITING_APPROVAL
        can_approve = can_reject and decision is not GuardrailDecision.DENY

        return RecoveryCaseOut(
            id=case.id,
            original_payment_id=case.original_payment_id,
            customer_id=case.customer_id,
            customer_name=customer.name,
            status=status,
            failure_category=FailureCategory(case.failure_category),
            strategy=RecoveryStrategy(case.strategy),
            guardrail_decision=decision,
            propensity_score=case.propensity_score,
            agent_mode=AgentMode(case.agent_mode),
            amount_paise=case.amount_paise,
            amount_rupees=paise_to_rupees(case.amount_paise),
            attempt_count=case.attempt_count,
            created_at=as_utc(case.created_at),
            updated_at=as_utc(case.updated_at),
            classification_confidence=case.classification_confidence,
            agent_rationale=case.agent_rationale,
            customer_message=case.customer_message,
            propensity_model_version=case.propensity_model_version,
            propensity_is_fallback=self._propensity_was_fallback(case.id),
            guardrail_evaluations=[
                GuardrailEvaluationOut(**row) for row in (case.guardrail_evaluations or [])
            ],
            policy_snapshot=case.policy_snapshot or {},
            approved_by=case.approved_by,
            approved_at=as_utc(case.approved_at) if case.approved_at else None,
            rejected_by=case.rejected_by,
            rejected_at=as_utc(case.rejected_at) if case.rejected_at else None,
            rejection_reason=case.rejection_reason,
            recovered_at=as_utc(case.recovered_at) if case.recovered_at else None,
            recovered_amount_paise=case.recovered_amount_paise,
            recovered_amount_rupees=paise_to_rupees(case.recovered_amount_paise),
            failure_note=case.failure_note,
            expires_at=as_utc(case.expires_at) if case.expires_at else None,
            original_payment=self.payments.to_out(payment),
            customer=customer_to_out(customer),
            attempts=[self._attempt_out(attempt) for attempt in case.attempts],
            can_approve=can_approve,
            can_reject=can_reject,
            approval_blocked_reason=self._approval_blocked_reason(case, status, decision),
        )

    def trace(self, case_id: str) -> list[AgentToolCallOut]:
        """
        Return the agent's reasoning steps for a case, oldest first.

        Args:
            case_id: The case id.

        Returns:
            Every recorded tool call, in the order it happened.

        Raises:
            NotFoundError: When no such case exists.

        A re-analysed case has more than one run. All of them are returned rather
        than only the latest, because "why did it decide differently the second
        time?" is a question the trace should be able to answer; ``run_id`` is on
        every row so the UI can group them.
        """
        case = self.get_case(case_id)
        rows = self.db.execute(
            select(AgentToolCall)
            .where(AgentToolCall.case_id == case.id)
            .order_by(AgentToolCall.created_at.asc(), AgentToolCall.step.asc())
        ).scalars()
        return [
            AgentToolCallOut.model_validate(row).model_copy(
                update={"created_at": as_utc(row.created_at)}
            )
            for row in rows
        ]

    # =====================================================================
    # The human decision
    # =====================================================================

    def approve(self, case_id: str, body: ApproveIn) -> RecoveryCaseOut:
        """
        Authorise a recovery and create the order the customer can pay.

        Args:
            case_id: The case awaiting approval.
            body: Operator identity and an optional note, both for the ledger.

        Returns:
            The case, now ``AWAITING_PAYMENT``.

        Raises:
            InvalidStateTransition: The case is not awaiting approval.
            GuardrailDenied: A guardrail refused at approval time. The refusal is
                recorded and committed before the exception is raised.
            GatewayError: Order creation failed. The case is moved to ``FAILED``.

        This is the only method in the system that causes an order to exist, and
        its order of operations *is* the product:

        1. Confirm the case is still awaiting approval.
        2. **Re-evaluate every guardrail against live state.** A proposal is a
           snapshot; the world moves between proposing and approving. The daily
           budget has been spent down by other cases, the customer has opened
           more, the payment has aged past the freshness limit, an attempt has
           accumulated. The binding check is the one taken at the instant money
           would move -- checking only at proposal time would mean an approval
           screen left open over lunch could authorise something policy now
           forbids.
        3. On DENY: record the refusal, freeze it onto the case, and refuse.
        4. Record the human approval, with a name attached.
        5. Create the attempt row under a deterministic idempotency key.
        6. Ask the gateway for an order, for the amount on the *payment*.
        7. Publish the recovery link and start the expiry clock.
        8. Commit once.
        """
        case = self.get_case(case_id)
        status = RecoveryStatus(case.status)

        # Step 1.
        if status is not RecoveryStatus.AWAITING_APPROVAL:
            raise InvalidStateTransition(
                f"Case {case.id} is '{status.value}' and cannot be approved; "
                "only a case awaiting approval can be.",
                detail={"case_id": case.id, "status": status.value},
            )

        payment = self._original_payment(case)
        customer = self._customer(case)
        now = utcnow()
        attempt_number = case.attempt_count + 1

        # Step 2.
        verdict = self.policy.evaluate(
            self._guardrail_context(
                case=case,
                payment=payment,
                customer=customer,
                strategy=RecoveryStrategy(case.strategy),
                failure_category=FailureCategory(case.failure_category),
                propensity_score=case.propensity_score,
                attempt_number=attempt_number,
                now=now,
            )
        )
        # The approval-time verdict replaces the proposal-time one on the case
        # row, because it is the verdict that authorised (or refused) the money.
        # The earlier one is not lost: both evaluations sit in the ledger, so the
        # pair can be compared long after the case closes.
        self._persist_verdict(case, verdict)
        self.ledger.record(
            event_type=AuditEventType.GUARDRAILS_EVALUATED,
            actor_type=ActorType.SYSTEM,
            case_id=case.id,
            payment_id=payment.id,
            summary=f"Re-evaluated guardrails at approval time. {verdict.summary}",
            payload={
                "phase": "approval",
                "decision": verdict.decision.value,
                "blocking_rules": list(verdict.blocking_rules),
                "approval_rules": list(verdict.approval_rules),
                "evaluations": case.guardrail_evaluations,
                "attempt_number": attempt_number,
            },
        )

        # Step 3.
        if verdict.decision is GuardrailDecision.DENY:
            # BLOCKED, not REJECTED. REJECTED means "a human declined" and
            # carries ``rejected_by``; writing a machine's refusal into a field
            # the audit trail attributes to a named person is the one thing an
            # audit trail must never do.
            #
            # Moving the status matters as much as raising the exception. The
            # stored verdict alone would already kill the Approve button, because
            # to_out() derives can_approve from ``guardrail_decision``. But the
            # status column is what queues, filters and dashboard counts are built
            # on, so a refused case left sitting in AWAITING_APPROVAL would clog
            # an operator's work queue with cases that can never be approved and
            # inflate the pending-approval metric indefinitely.
            self._transition(case, RecoveryStatus.BLOCKED)
            self.ledger.record(
                event_type=AuditEventType.RECOVERY_BLOCKED,
                actor_type=ActorType.SYSTEM,
                case_id=case.id,
                payment_id=payment.id,
                summary=verdict.summary,
                payload={
                    "blocking_rules": list(verdict.blocking_rules),
                    "phase": "approval",
                    "status": case.status,
                },
            )
            # Commit *before* raising. The block is a decision that has to
            # survive; the exception is only how it reaches the operator's
            # screen. Raising first would let the session close with the
            # transaction unflushed, and the case would silently sit in
            # AWAITING_APPROVAL as though nothing had been checked.
            self.db.commit()
            raise GuardrailDenied(
                verdict.summary,
                detail={
                    "case_id": case.id,
                    "blocking_rules": list(verdict.blocking_rules),
                    "status": case.status,
                },
            )

        # Step 4.
        self._transition(case, RecoveryStatus.APPROVED)
        case.approved_by = body.approved_by
        case.approved_at = now
        self.ledger.record(
            event_type=AuditEventType.APPROVAL_GRANTED,
            actor_type=ActorType.HUMAN,
            actor_id=body.approved_by,
            case_id=case.id,
            payment_id=payment.id,
            summary=(
                f"{body.approved_by} approved recovery of "
                f"Rs {paise_to_rupees(case.amount_paise):,.2f} via '{case.strategy}'."
            ),
            payload={
                "approved_by": body.approved_by,
                "note": body.note,
                "amount_paise": case.amount_paise,
                "strategy": case.strategy,
                "attempt_number": attempt_number,
            },
        )

        # Step 5.
        self._transition(case, RecoveryStatus.EXECUTING)
        idempotency_key = f"{case.id}:{attempt_number}"
        attempt = self.db.execute(
            select(RecoveryAttempt).where(RecoveryAttempt.idempotency_key == idempotency_key)
        ).scalar_one_or_none()

        if attempt is None:
            attempt = RecoveryAttempt(
                case_id=case.id,
                attempt_number=attempt_number,
                strategy=case.strategy,
                # From the case, which took it from the payment. Never from the
                # plan, and never recomputed from a rupee value.
                amount_paise=case.amount_paise,
                gateway_mode=self.gateway.mode.value,
                idempotency_key=idempotency_key,
                status=RecoveryStatus.EXECUTING.value,
            )
            self.db.add(attempt)
            case.attempt_count = attempt_number
            self.db.flush()
        else:
            # A double-clicked Approve button is not a hypothetical, and neither
            # is a client that retries a request whose response it never saw.
            # Reusing the row keyed on (case, attempt number) means the second
            # arrival finishes the first one's work instead of asking the gateway
            # for a second order against the same money.
            logger.info("Reusing existing recovery attempt %s for key %s", attempt.id, idempotency_key)

        # Step 6.
        if attempt.razorpay_order_id is None:
            try:
                order = self.gateway.create_order(
                    amount_paise=case.amount_paise,
                    currency=case.currency,
                    receipt=f"{case.id}-{attempt_number}",
                    notes={
                        "case_id": case.id,
                        "original_payment_id": case.original_payment_id,
                        "strategy": case.strategy,
                        "attempt_number": str(attempt_number),
                    },
                )
            except GatewayError as exc:
                self._fail_attempt(case, attempt, reason=f"Order creation failed: {exc.message}")
                self.db.commit()
                raise
            # The gateway is an external system, so what it returns is checked
            # rather than assumed. This is the last mile of the amount-integrity
            # invariant: everything upstream can be correct and a mis-sent field
            # would still put the wrong number in front of the customer.
            #
            # Raised without committing, unlike the guardrail and order-creation
            # failures above. Those are outcomes worth recording; this is a
            # contradiction between what we asked for and what came back, and the
            # right response to a system behaving impossibly is to persist nothing
            # and let the whole unit of work roll back. The case is left exactly
            # as it was, awaiting approval.
            if order.amount_paise != case.amount_paise:
                raise GatewayError(
                    "The gateway created an order for a different amount than requested.",
                    detail={
                        "requested_paise": case.amount_paise,
                        "returned_paise": order.amount_paise,
                        "order_id": order.id,
                    },
                )
            attempt.razorpay_order_id = order.id

            if self.gateway.mode == GatewayMode.SIMULATED:
                # Said out loud in the ledger so a demo can never be mistaken for
                # a real collection.
                self.ledger.record(
                    event_type=AuditEventType.GATEWAY_SIMULATED,
                    actor_type=ActorType.SYSTEM,
                    case_id=case.id,
                    payment_id=payment.id,
                    summary="No Razorpay credentials configured; the order was created in the simulator.",
                    payload={"order_id": order.id, "gateway_mode": self.gateway.mode.value},
                )

        # Step 7.
        self._transition(case, RecoveryStatus.AWAITING_PAYMENT)
        attempt.status = RecoveryStatus.AWAITING_PAYMENT.value
        case.expires_at = now + timedelta(minutes=self.settings.recovery_link_ttl_minutes)
        recovery_link = f"{self.settings.frontend_base_url.rstrip('/')}/checkout/{case.id}"

        self.ledger.record(
            event_type=AuditEventType.RECOVERY_ORDER_CREATED,
            actor_type=ActorType.SYSTEM,
            case_id=case.id,
            payment_id=payment.id,
            summary=(
                f"Created recovery order {attempt.razorpay_order_id} for "
                f"Rs {paise_to_rupees(case.amount_paise):,.2f}."
            ),
            payload={
                "order_id": attempt.razorpay_order_id,
                "attempt_id": attempt.id,
                "attempt_number": attempt_number,
                "amount_paise": case.amount_paise,
                "gateway_mode": attempt.gateway_mode,
                "idempotency_key": idempotency_key,
            },
        )
        self.ledger.record(
            event_type=AuditEventType.RECOVERY_LINK_SENT,
            actor_type=ActorType.SYSTEM,
            case_id=case.id,
            payment_id=payment.id,
            summary=f"Recovery link issued to {customer.email or customer.name}.",
            payload={
                "link": recovery_link,
                "expires_at": case.expires_at.isoformat(),
                "customer_message": case.customer_message,
            },
        )

        # Step 8.
        self.db.commit()
        logger.info("Case %s approved by %s; order %s live", case.id, body.approved_by, attempt.razorpay_order_id)
        return self.to_out(case)

    def reject(self, case_id: str, body: RejectIn) -> RecoveryCaseOut:
        """
        Decline a proposed recovery.

        Args:
            case_id: The case awaiting approval.
            body: Who declined and why. The reason is mandatory.

        Returns:
            The case, now ``REJECTED``.

        Raises:
            InvalidStateTransition: The case is not awaiting approval.

        The reason is required rather than optional because a rejection is
        training data for the next version of the policy: "operators decline this
        category 80% of the time" is only learnable if they had to say so.
        """
        case = self.get_case(case_id)
        status = RecoveryStatus(case.status)
        if status is not RecoveryStatus.AWAITING_APPROVAL:
            raise InvalidStateTransition(
                f"Case {case.id} is '{status.value}' and cannot be rejected; "
                "only a case awaiting approval can be.",
                detail={"case_id": case.id, "status": status.value},
            )

        self._transition(case, RecoveryStatus.REJECTED)
        case.rejected_by = body.rejected_by
        case.rejected_at = utcnow()
        case.rejection_reason = body.reason

        self.ledger.record(
            event_type=AuditEventType.APPROVAL_REJECTED,
            actor_type=ActorType.HUMAN,
            actor_id=body.rejected_by,
            case_id=case.id,
            payment_id=case.original_payment_id,
            summary=f"{body.rejected_by} declined the recovery: {body.reason}",
            payload={"rejected_by": body.rejected_by, "reason": body.reason},
        )
        self.db.commit()
        return self.to_out(case)

    # =====================================================================
    # Collection
    # =====================================================================

    def checkout_session(self, case_id: str) -> CheckoutSessionOut:
        """
        Everything the customer-facing recovery page needs to open checkout.

        Args:
            case_id: The case with a live order.

        Returns:
            ``CheckoutSessionOut``. ``razorpay_key_id`` is ``None`` in simulated
            mode, which is how the page knows to render the simulator controls
            instead of loading Razorpay's SDK.

        Raises:
            ApprovalRequired: The case has not been approved yet.
            InvalidStateTransition: The case is in a state with no payable order.
            NotFoundError: No attempt carries an order id.

        Exposing the publishable key id is safe by design -- it identifies the
        merchant account to Razorpay's browser SDK and can do nothing on its own;
        the secret that signs and verifies never leaves the server.

        Sweeps expired links before reading the case. Without it the page would
        happily render "this link is valid until 02:57" for a link that ran out an
        hour ago, and the customer would only discover otherwise by pressing Pay.
        """
        self.expire_stale_cases()

        case = self.get_case(case_id)
        status = RecoveryStatus(case.status)

        if status in (RecoveryStatus.PROPOSED, RecoveryStatus.AWAITING_APPROVAL):
            raise ApprovalRequired(
                "This recovery has not been approved yet, so there is nothing to pay.",
                detail={"case_id": case.id, "status": status.value},
            )
        if status is not RecoveryStatus.AWAITING_PAYMENT:
            raise InvalidStateTransition(
                f"Case {case.id} is '{status.value}'; there is no open payment for it.",
                detail={"case_id": case.id, "status": status.value},
            )

        attempt = self._latest_attempt(case)
        if attempt is None or attempt.razorpay_order_id is None:
            raise NotFoundError(
                f"Case {case.id} has no recovery order to pay.", detail={"case_id": case.id}
            )

        customer = self._customer(case)
        payment = self._original_payment(case)

        # Contact details are sent only when the gateway that will consume them is
        # actually live.
        #
        # This is the one endpoint in the system a stranger is *meant* to reach:
        # the customer opens it from a link that arrived by email or SMS, so the
        # link travels through channels that get forwarded, screenshotted and read
        # on shared devices. Razorpay's hosted Checkout takes name/email/contact as
        # `prefill`, which is a real requirement -- but only in `razorpay_test`
        # mode. In simulated mode the page renders none of them, so sending them
        # put three pieces of personal data on the most exposed surface in the
        # product in exchange for nothing.
        #
        # Anyone holding a valid link can still see them when Razorpay is live.
        # That is inherent to prefill and is stated as a limitation rather than
        # pretended away; what is fixed here is paying that cost when nothing
        # collects the benefit.
        needs_prefill = self.gateway.mode is GatewayMode.RAZORPAY_TEST

        return CheckoutSessionOut(
            case_id=case.id,
            attempt_id=attempt.id,
            order_id=attempt.razorpay_order_id,
            amount_paise=case.amount_paise,
            amount_rupees=paise_to_rupees(case.amount_paise),
            currency=case.currency,
            razorpay_key_id=self.gateway.key_id,
            gateway_mode=GatewayMode(attempt.gateway_mode),
            customer_name=customer.name if needs_prefill else "",
            customer_email=customer.email if needs_prefill else "",
            customer_phone=customer.phone if needs_prefill else "",
            description=case.customer_message or payment.description or "Complete your payment",
            expires_at=as_utc(case.expires_at) if case.expires_at else None,
        )

    def verify_payment(self, case_id: str, body: VerifyPaymentIn) -> RecoveryCaseOut:
        """
        Verify a checkout callback and, if it is authentic, close the case as
        recovered.

        Args:
            case_id: The case whose order was paid.
            body: The order id, payment id and signature Razorpay Checkout posted.

        Returns:
            The case, now ``RECOVERED``.

        Raises:
            InvalidStateTransition: The case has no payment outstanding.
            NotFoundError: The order does not belong to this case.
            SignatureVerificationError: The HMAC did not verify.

        **The signature is checked before anything else is written.** The browser
        callback is a claim, not proof: the client posts "payment succeeded" from
        JavaScript an attacker fully controls. Without server-side verification,
        marking any case recovered would be one curl command with a guessed case
        id -- the merchant's dashboard would report money that was never
        collected, which is worse than reporting none at all.
        """
        case = self.get_case(case_id)
        status = RecoveryStatus(case.status)
        if status is not RecoveryStatus.AWAITING_PAYMENT:
            raise InvalidStateTransition(
                f"Case {case.id} is '{status.value}'; it has no payment outstanding to verify.",
                detail={"case_id": case.id, "status": status.value},
            )

        # Scoped to this case's own attempts on purpose. The signature covers
        # only "<order_id>|<payment_id>", so a genuinely signed pair from a
        # different case would verify perfectly well; binding the callback to an
        # order this case actually created is what stops one customer's valid
        # receipt from closing someone else's case.
        attempt = self.db.execute(
            select(RecoveryAttempt).where(
                RecoveryAttempt.case_id == case.id,
                RecoveryAttempt.razorpay_order_id == body.razorpay_order_id,
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise NotFoundError(
                f"Order '{body.razorpay_order_id}' does not belong to case {case.id}.",
                detail={"case_id": case.id, "order_id": body.razorpay_order_id},
            )

        if not self.gateway.verify_payment_signature(
            order_id=body.razorpay_order_id,
            payment_id=body.razorpay_payment_id,
            signature=body.razorpay_signature,
        ):
            self.ledger.record(
                event_type=AuditEventType.RECOVERY_FAILED,
                actor_type=ActorType.SYSTEM,
                actor_id="checkout",
                case_id=case.id,
                payment_id=case.original_payment_id,
                summary=(
                    "Rejected a payment confirmation: the signature did not match. "
                    "The case was left open."
                ),
                payload={
                    "reason": "invalid_signature",
                    "order_id": body.razorpay_order_id,
                    "payment_id": body.razorpay_payment_id,
                },
            )
            # The rejection is committed and the case deliberately left in
            # AWAITING_PAYMENT: a forged callback must not be able to close a
            # live case, and the real customer may still pay.
            self.db.commit()
            raise SignatureVerificationError(
                "Payment signature verification failed. This payment has not been accepted.",
                detail={"case_id": case.id, "order_id": body.razorpay_order_id},
            )

        now = utcnow()
        original = self._original_payment(case)
        customer = self._customer(case)

        recovery_payment = Payment(
            razorpay_order_id=body.razorpay_order_id,
            razorpay_payment_id=body.razorpay_payment_id,
            customer_id=case.customer_id,
            # From the case, which took it from the original payment. The
            # callback body carries no amount for exactly this reason.
            amount_paise=case.amount_paise,
            currency=case.currency,
            method=self._recovered_method(case, original).value,
            status=PaymentStatus.CAPTURED.value,
            description=f"Recovery of payment {original.id}",
            is_recovery_attempt=True,
            parent_payment_id=original.id,
        )
        self.db.add(recovery_payment)

        # A recovery is a real payment in this customer's history, so both
        # counters move. Only incrementing the successes would inflate
        # prior_success_rate above what actually happened and feed the propensity
        # model a number it can never reproduce from the payments table.
        customer.total_payments += 1
        customer.successful_payments += 1
        customer.lifetime_value_paise += case.amount_paise

        attempt.razorpay_payment_id = body.razorpay_payment_id
        attempt.status = RecoveryStatus.RECOVERED.value
        attempt.completed_at = now

        self._transition(case, RecoveryStatus.RECOVERED)
        case.recovered_at = now
        case.recovered_amount_paise = case.amount_paise
        self.db.flush()

        self.ledger.record(
            event_type=AuditEventType.PAYMENT_VERIFIED,
            actor_type=ActorType.SYSTEM,
            actor_id="checkout",
            case_id=case.id,
            payment_id=recovery_payment.id,
            summary=f"Verified the HMAC signature for payment {body.razorpay_payment_id}.",
            payload={
                "order_id": body.razorpay_order_id,
                "razorpay_payment_id": body.razorpay_payment_id,
                "gateway_mode": attempt.gateway_mode,
                "recovery_payment_id": recovery_payment.id,
            },
        )
        self.ledger.record(
            event_type=AuditEventType.RECOVERY_SUCCEEDED,
            actor_type=ActorType.SYSTEM,
            actor_id="checkout",
            case_id=case.id,
            payment_id=recovery_payment.id,
            summary=(
                f"Recovered Rs {paise_to_rupees(case.amount_paise):,.2f} from {customer.name} "
                f"on attempt {attempt.attempt_number} via '{case.strategy}'."
            ),
            payload={
                "amount_paise": case.amount_paise,
                "attempt_number": attempt.attempt_number,
                "strategy": case.strategy,
                "original_payment_id": original.id,
            },
        )

        self.db.commit()
        logger.info("Case %s recovered (payment %s)", case.id, recovery_payment.id)
        return self.to_out(case)

    def simulate_checkout(self, case_id: str, *, succeed: bool) -> RecoveryCaseOut:
        """
        Play the customer's side of checkout, for demos and tests.

        Args:
            case_id: The case with a live order.
            succeed: Whether the simulated customer pays or walks away.

        Returns:
            The case, now ``RECOVERED`` or ``FAILED``.

        Raises:
            ConfigurationError: The live gateway is active.
            NotFoundError: No order to pay.

        Refused whenever real Razorpay credentials are configured. A demo
        shortcut that still works when the system is pointed at a real gateway is
        a way to mark real money collected without collecting it, so the check is
        on the gateway that is actually wired up, not on an environment name a
        deployment could get wrong.

        On success it mints a genuine HMAC and posts it through ``verify_payment``
        rather than transitioning the case directly. The verification code is the
        part most worth exercising, and a shortcut past it would mean the path
        the demo proves is not the path production runs.
        """
        if self.gateway.mode != GatewayMode.SIMULATED:
            raise ConfigurationError(
                "Simulated checkout is disabled because a live Razorpay gateway is configured. "
                "Complete the payment through Razorpay Checkout instead.",
                detail={"gateway_mode": self.gateway.mode.value},
            )

        case = self.get_case(case_id)
        attempt = self._latest_attempt(case)
        if attempt is None or attempt.razorpay_order_id is None:
            raise NotFoundError(
                f"Case {case.id} has no recovery order to pay.", detail={"case_id": case.id}
            )

        if not succeed:
            return self.mark_attempt_failed(
                case.id,
                MarkAttemptFailedIn(reason="Simulated checkout: the customer did not complete payment"),
            )

        # The amount comes from the case, not from the gateway's memory: the
        # simulator is in-process, so an approved case must stay payable across a
        # restart.
        payment_id, signature = self.gateway.simulate_payment(
            attempt.razorpay_order_id,
            amount_paise=case.amount_paise,
            succeed=True,
        )
        return self.verify_payment(
            case.id,
            VerifyPaymentIn(
                razorpay_order_id=attempt.razorpay_order_id,
                razorpay_payment_id=payment_id,
                razorpay_signature=signature,
            ),
        )

    def mark_attempt_failed(self, case_id: str, body: MarkAttemptFailedIn) -> RecoveryCaseOut:
        """
        Close the live attempt as failed and say whether another one is possible.

        Args:
            case_id: The case with a live attempt.
            body: The reason to record.

        Returns:
            The case, now ``FAILED``, with ``failure_note`` explaining the retry
            budget in plain words.

        Raises:
            InvalidStateTransition: The case has no live attempt.

        The note is written for the merchant, not for a log reader. "Retry limit
        reached (2 of 2 attempts used). No further recovery attempt will be
        created." is the sentence that closes the loop on R1_MAX_ATTEMPTS: the
        limit is not merely enforced somewhere in a rules file, it is stated back
        to the person whose money it is.
        """
        case = self.get_case(case_id)
        attempt = self._latest_attempt(case)

        self._transition(case, RecoveryStatus.FAILED)
        if attempt is not None:
            attempt.status = RecoveryStatus.FAILED.value
            attempt.failure_reason = body.reason
            attempt.completed_at = utcnow()

        case.failure_note = f"{body.reason.rstrip('.')}. {self._retry_budget_sentence(case)}"
        case.expires_at = None

        self.ledger.record(
            event_type=AuditEventType.RECOVERY_FAILED,
            actor_type=ActorType.SYSTEM,
            case_id=case.id,
            payment_id=case.original_payment_id,
            summary=case.failure_note,
            payload={
                "reason": body.reason,
                "attempt_number": attempt.attempt_number if attempt else None,
                "attempts_used": case.attempt_count,
                "max_recovery_attempts": self.settings.max_recovery_attempts,
                "further_attempt_permitted": case.attempt_count < self.settings.max_recovery_attempts,
            },
        )
        self.db.commit()
        return self.to_out(case)

    def expire_stale_cases(self) -> int:
        """
        Expire every case whose recovery link has run out of time.

        Returns:
            How many cases were expired.

        Bounds how long a payable order can sit open. Without it a case would
        wait in ``AWAITING_PAYMENT`` forever, holding an open order that
        R6_DUPLICATE_ORDER counts -- so one abandoned checkout would silently
        block every future recovery for that payment.

        The cutoff is compared inside the query rather than in Python. SQLite
        stores these timestamps without a zone, so filtering in SQL keeps the
        comparison between two values written by the same code path instead of
        between a naive value read back and an aware ``utcnow()``.

        **Called from reads, not from a scheduler.** ``list_cases`` and
        ``checkout_session`` both invoke it, so a link expires the first time
        anyone looks at the queue or opens the payment page. The alternative was a
        background task on the app's lifespan, and it lost for two reasons: it
        adds a second thing that can fail in a project whose whole premise is that
        it runs with nothing installed, and a case that expires while nobody is
        watching has not yet affected anyone. Sweeping on read means the state a
        caller sees is always current, which is the only property that actually
        matters here.

        The cost is one indexed query per read of those two endpoints, and it is
        a no-op when nothing is stale.
        """
        now = utcnow()
        stale = list(
            self.db.execute(
                select(RecoveryCase).where(
                    RecoveryCase.status == RecoveryStatus.AWAITING_PAYMENT.value,
                    RecoveryCase.expires_at.is_not(None),
                    RecoveryCase.expires_at < now,
                )
            ).scalars()
        )

        for case in stale:
            self._transition(case, RecoveryStatus.EXPIRED)
            attempt = self._latest_attempt(case)
            if attempt is not None and attempt.status in OPEN_ATTEMPT_STATUSES:
                attempt.status = RecoveryStatus.EXPIRED.value
                attempt.completed_at = now
            case.failure_note = (
                f"The recovery link expired unused after "
                f"{self.settings.recovery_link_ttl_minutes} minutes."
            )
            self.ledger.record(
                event_type=AuditEventType.RECOVERY_EXPIRED,
                actor_type=ActorType.SYSTEM,
                case_id=case.id,
                payment_id=case.original_payment_id,
                summary=case.failure_note,
                payload={
                    "expired_at": now.isoformat(),
                    "ttl_minutes": self.settings.recovery_link_ttl_minutes,
                    "attempt_id": attempt.id if attempt else None,
                },
            )

        if stale:
            self.db.commit()
            logger.info("Expired %d stale recovery case(s)", len(stale))
        return len(stale)

    # =====================================================================
    # Internals
    # =====================================================================

    def _transition(self, case: RecoveryCase, to_status: RecoveryStatus) -> None:
        """
        Move a case to a new state, or refuse.

        Args:
            case: The case to move.
            to_status: The target state.

        Raises:
            InvalidStateTransition: When the edge is not in ``ALLOWED_TRANSITIONS``.

        Every state change in the system goes through this one method, so the
        table in ``domain/enums.py`` is the whole truth about what can happen to a
        case. Scattering ``case.status = ...`` across the service would make that
        table decorative, and a replayed webhook or an out-of-order gateway
        callback would corrupt a case instead of raising here.

        Deliberately does not write to the ledger: different transitions warrant
        different event types and different summaries, so the caller -- which
        knows why it is moving the case -- records it.
        """
        current = RecoveryStatus(case.status)
        if to_status not in ALLOWED_TRANSITIONS[current]:
            raise InvalidStateTransition(
                f"Cannot move case {case.id} from '{current.value}' to '{to_status.value}'.",
                detail={
                    "case_id": case.id,
                    "from": current.value,
                    "to": to_status.value,
                    "allowed": [s.value for s in ALLOWED_TRANSITIONS[current]],
                },
            )
        case.status = to_status.value

    def _open_or_reuse_case(self, payment: Payment) -> RecoveryCase:
        """
        Return the case to analyse into: an existing failed one, or a new one.

        Raises:
            DuplicateCaseError: The payment's case is finished, or in flight.

        A previously failed attempt is re-proposed onto the **same** case, which
        is what carries ``attempt_count`` forward. Opening a fresh case would
        reset that counter to zero, and R1_MAX_ATTEMPTS counts attempts within a
        case -- so "max 2 attempts" would become "max 2 attempts per analysis",
        i.e. unlimited. A retry limit is exactly the kind of rule that needs this
        hole closed, because retrying is the thing it exists to bound.
        """
        existing = self.db.execute(
            select(RecoveryCase).where(RecoveryCase.original_payment_id == payment.id)
        ).scalar_one_or_none()

        if existing is not None:
            status = RecoveryStatus(existing.status)
            if status.is_terminal:
                raise DuplicateCaseError(
                    f"Payment {payment.id} already has a closed recovery case "
                    f"({status.value}).",
                    detail={"payment_id": payment.id, "case_id": existing.id, "status": status.value},
                )
            if status is not RecoveryStatus.FAILED:
                raise DuplicateCaseError(
                    f"Payment {payment.id} already has a recovery case in progress "
                    f"({status.value}).",
                    detail={"payment_id": payment.id, "case_id": existing.id, "status": status.value},
                )

            self._transition(existing, RecoveryStatus.PROPOSED)
            # The previous approval authorised the previous attempt and nothing
            # else. Carrying it forward would let one operator click authorise a
            # second charge, which is precisely what a per-attempt approval is
            # supposed to prevent.
            existing.approved_by = None
            existing.approved_at = None
            existing.expires_at = None
            return existing

        # A new case is created *before* the agent runs so that every audit
        # event, starting with ANALYSIS_STARTED, can name the case it belongs to.
        # The three columns the agent has not filled in yet are seeded with the
        # most conservative values in each enum, so a crash mid-analysis leaves a
        # case that is unclassified, escalated to a human and denied by policy --
        # a case that cannot move money on its own.
        case = RecoveryCase(
            original_payment_id=payment.id,
            customer_id=payment.customer_id,
            status=RecoveryStatus.PROPOSED.value,
            failure_category=FailureCategory.UNKNOWN.value,
            strategy=RecoveryStrategy.MANUAL_REVIEW.value,
            guardrail_decision=GuardrailDecision.DENY.value,
            guardrail_evaluations=[],
            policy_snapshot={},
            amount_paise=payment.amount_paise,
            currency=payment.currency,
            attempt_count=0,
        )
        self.db.add(case)
        self.db.flush()
        return case

    def _route(self, strategy: RecoveryStrategy, decision: GuardrailDecision) -> RecoveryStatus:
        """
        Decide where a freshly analysed case goes.

        Args:
            strategy: What the agent recommended.
            decision: The guardrail engine's aggregate verdict.

        Returns:
            The status to transition into.

        The strategy is consulted before the verdict, because a strategy that
        moves no money has nothing for the guardrails to permit or refuse -- the
        rules short-circuit to ALLOW in that case, and reading that ALLOW as
        "approved" would put "do nothing" in front of an operator for sign-off.
        The three terminal outcomes stay distinct because their causes differ and
        a merchant needs to tell them apart: nothing worth doing, a human must
        handle it, or policy said no.
        """
        if strategy in (RecoveryStrategy.NO_RECOVERY, RecoveryStrategy.RETRY_LATER):
            return RecoveryStatus.NO_ACTION
        if strategy is RecoveryStrategy.MANUAL_REVIEW:
            return RecoveryStatus.ESCALATED
        if decision is GuardrailDecision.DENY:
            return RecoveryStatus.BLOCKED
        return RecoveryStatus.AWAITING_APPROVAL

    def _guardrail_context(
        self,
        *,
        case: RecoveryCase,
        payment: Payment,
        customer: Customer,
        strategy: RecoveryStrategy,
        failure_category: FailureCategory,
        propensity_score: float,
        attempt_number: int,
        now: datetime,
    ) -> GuardrailContext:
        """
        Gather every fact the thirteen rules are allowed to see.

        Args:
            case: The case being judged.
            payment: The original failed payment.
            customer: The payer.
            strategy: The proposed strategy.
            failure_category: The classified failure reason.
            propensity_score: ML-predicted probability of success.
            attempt_number: Which attempt this would be, counting from 1.
            now: The single instant the whole evaluation is judged against.

        Returns:
            A frozen ``GuardrailContext``.

        The rules are pure functions; all four counting queries happen here. That
        is what makes every rule judge the same instant and the same numbers -- if
        each rule ran its own query, a slow evaluation could straddle midnight and
        produce a verdict no one could reproduce.
        """
        day_start = utc_day_start(now)

        last_attempt_at = self.db.execute(
            select(func.max(RecoveryAttempt.created_at)).where(RecoveryAttempt.case_id == case.id)
        ).scalar()

        open_attempts = self.db.execute(
            select(func.count(RecoveryAttempt.id)).where(
                RecoveryAttempt.case_id == case.id,
                RecoveryAttempt.status.in_(OPEN_ATTEMPT_STATUSES),
            )
        ).scalar_one()

        daily_recovery_total_paise = self.db.execute(
            select(func.coalesce(func.sum(RecoveryAttempt.amount_paise), 0)).where(
                RecoveryAttempt.created_at >= day_start
            )
        ).scalar_one()

        # Counts the customer's *other* cases today. R8 fires on ">= limit", so
        # with a limit of three the fourth case is the one refused; including the
        # case being opened in its own count would make the third one fail and
        # turn a limit of three into a limit of two.
        customer_cases_today = self.db.execute(
            select(func.count(RecoveryCase.id)).where(
                RecoveryCase.customer_id == case.customer_id,
                RecoveryCase.created_at >= day_start,
                RecoveryCase.id != case.id,
            )
        ).scalar_one()

        return GuardrailContext(
            payment=payment,
            customer=customer,
            strategy=strategy,
            failure_category=failure_category,
            propensity_score=propensity_score,
            # The one line in this method that is not a lookup, and the most
            # important: the amount comes from the payment row, always.
            amount_paise=payment.amount_paise,
            attempt_number=attempt_number,
            now=now,
            last_attempt_at=as_utc(last_attempt_at) if last_attempt_at is not None else None,
            open_attempt_exists=open_attempts > 0,
            daily_recovery_total_paise=int(daily_recovery_total_paise),
            customer_cases_today=int(customer_cases_today),
            settings=self.settings,
        )

    def _persist_verdict(self, case: RecoveryCase, verdict: GuardrailVerdict) -> None:
        """
        Freeze a verdict and the policy that produced it onto the case row.

        The evaluations are stored in the exact wire shape the detail screen
        consumes, so a decision taken months ago renders without a translation
        step that could quietly reinterpret it. The policy snapshot travels with
        them because otherwise a later config change would silently rewrite the
        meaning of every historical decision -- "denied for exceeding the ceiling"
        is unreadable without knowing what the ceiling was that day.
        """
        case.guardrail_decision = verdict.decision.value
        case.guardrail_evaluations = [
            GuardrailEvaluationOut(**asdict(evaluation)).model_dump(mode="json")
            for evaluation in verdict.evaluations
        ]
        case.policy_snapshot = self.policy.policy_snapshot()

    def _link_trace_to_case(self, run_id: str, case: RecoveryCase) -> None:
        """Stamp this run's tool-call rows with the case they produced."""
        # Flush first: the orchestrator's rows may still be pending in the
        # session, and a query that does not autoflush would not see them.
        self.db.flush()
        for row in self.db.execute(
            select(AgentToolCall).where(AgentToolCall.run_id == run_id)
        ).scalars():
            row.case_id = case.id

    def _fail_attempt(self, case: RecoveryCase, attempt: RecoveryAttempt, *, reason: str) -> None:
        """Close an attempt that never got off the ground, and record why."""
        attempt.status = RecoveryStatus.FAILED.value
        attempt.failure_reason = reason
        attempt.completed_at = utcnow()
        self._transition(case, RecoveryStatus.FAILED)
        case.failure_note = f"{reason.rstrip('.')}. {self._retry_budget_sentence(case)}"
        self.ledger.record(
            event_type=AuditEventType.RECOVERY_FAILED,
            actor_type=ActorType.SYSTEM,
            case_id=case.id,
            payment_id=case.original_payment_id,
            summary=case.failure_note,
            payload={"reason": reason, "attempt_id": attempt.id, "attempts_used": case.attempt_count},
        )

    def _retry_budget_sentence(self, case: RecoveryCase) -> str:
        """
        State the remaining retry budget in a sentence a merchant can read.

        Derived from ``settings.max_recovery_attempts`` -- the same value
        R1_MAX_ATTEMPTS enforces -- rather than from a constant repeated here, so
        the sentence cannot promise a retry the guardrail would refuse.

        The cooldown is named alongside the count for the same reason. R1 is not
        the only rule standing between a failed attempt and the next one: R2
        refuses anything inside the quiet period, and it is the rule that
        actually fires first. Saying only "one further attempt may be proposed"
        is true but incomplete, and an operator who reads it, clicks re-analyse
        and is refused by a rule the sentence never mentioned has been told
        something misleading by omission.
        """
        used = case.attempt_count
        allowed = self.settings.max_recovery_attempts
        if used >= allowed:
            return (
                f"Retry limit reached ({used} of {allowed} attempts used). "
                "No further recovery attempt will be created."
            )
        remaining = allowed - used
        noun = "attempt" if remaining == 1 else "attempts"
        return (
            f"{used} of {allowed} attempts used; {remaining} further {noun} "
            "may be proposed once the cooldown has passed "
            f"({_humanise_seconds(self.settings.recovery_cooldown_seconds)})."
        )

    def _approval_blocked_reason(
        self, case: RecoveryCase, status: RecoveryStatus, decision: GuardrailDecision
    ) -> str | None:
        """
        Explain, in one sentence, why Approve is unavailable -- or ``None``.

        Built from the stored evaluations rather than re-run, so the explanation
        the operator reads is the verdict that was actually recorded.
        """
        if status is RecoveryStatus.AWAITING_APPROVAL and decision is not GuardrailDecision.DENY:
            return None

        denied = [
            row.get("reason", "")
            for row in (case.guardrail_evaluations or [])
            if row.get("decision") == GuardrailDecision.DENY.value
        ]
        if denied:
            return " ".join(reason for reason in denied if reason)
        return (
            f"This case is {status.value.replace('_', ' ')}; approval is only possible "
            "while it is awaiting approval."
        )

    def _propensity_was_fallback(self, case_id: str) -> bool:
        """
        Read back whether the propensity score came from the heuristic fallback.

        ``RecoveryCase`` has no column for this, and adding one would put the same
        fact in two places. The ledger already records it inside the hashed
        ``PROPENSITY_SCORED`` payload, so it is read from there: one indexed
        lookup, on the detail view only, and it cannot disagree with the audit
        trail because it *is* the audit trail.
        """
        payload = self.db.execute(
            select(AuditEvent.payload)
            .where(
                AuditEvent.case_id == case_id,
                AuditEvent.event_type == AuditEventType.PROPENSITY_SCORED.value,
            )
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        ).scalar_one_or_none()
        return bool((payload or {}).get("is_fallback", False))

    def _recovered_method(self, case: RecoveryCase, original: Payment) -> PaymentMethod:
        """Which instrument to record a successful recovery against."""
        strategy = RecoveryStrategy(case.strategy)
        mapped = STRATEGY_METHOD.get(strategy)
        if mapped is not None:
            return mapped
        try:
            return PaymentMethod(original.method)
        except ValueError:
            return PaymentMethod.UNKNOWN

    def _latest_attempt(self, case: RecoveryCase) -> RecoveryAttempt | None:
        """The highest-numbered attempt on a case, or ``None`` if it has none."""
        return self.db.execute(
            select(RecoveryAttempt)
            .where(RecoveryAttempt.case_id == case.id)
            .order_by(RecoveryAttempt.attempt_number.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _original_payment(self, case: RecoveryCase) -> Payment:
        """The failed payment a case is trying to rescue."""
        return self.payments.get_payment(case.original_payment_id)

    def _customer(self, case: RecoveryCase) -> Customer:
        """The payer a case belongs to."""
        customer = self.db.get(Customer, case.customer_id)
        if customer is None:
            raise NotFoundError(
                f"No customer with id '{case.customer_id}'.", detail={"customer_id": case.customer_id}
            )
        return customer

    @staticmethod
    def _attempt_out(attempt: RecoveryAttempt) -> RecoveryAttemptOut:
        """Render one attempt as its wire schema."""
        return RecoveryAttemptOut(
            id=attempt.id,
            attempt_number=attempt.attempt_number,
            strategy=RecoveryStrategy(attempt.strategy),
            amount_paise=attempt.amount_paise,
            amount_rupees=paise_to_rupees(attempt.amount_paise),
            status=attempt.status,
            gateway_mode=GatewayMode(attempt.gateway_mode),
            razorpay_order_id=attempt.razorpay_order_id,
            razorpay_payment_id=attempt.razorpay_payment_id,
            failure_reason=attempt.failure_reason,
            created_at=as_utc(attempt.created_at),
            completed_at=as_utc(attempt.completed_at) if attempt.completed_at else None,
        )
