"""
SQLAlchemy 2.0 ORM models -- the persistent shape of the system.

Modelling decisions worth knowing before you read the classes:

*   **Money is stored as integer paise.** Never ``Float``. Binary floating point
    cannot represent 0.10 exactly, so ``0.1 + 0.2 != 0.3``; in a payments system
    that is a defect, not trivia. Rupees exist only in the presentation layer.

*   **Times are timezone-aware UTC.** Guardrails do date arithmetic (cooldowns,
    daily budgets, staleness). A naive local timestamp would make those limits
    behave differently on a laptop in IST than on a server in UTC.

*   **One case, many attempts.** ``RecoveryCase`` is the decision record for a
    single failed payment; ``RecoveryAttempt`` is one concrete try at collecting.
    The "max 2 attempts" guardrail counts attempts inside a case. Keeping them in
    separate tables is what lets an attempt fail without destroying the reasoning
    that produced it.

*   **The audit ledger is append-only and hash-chained.** ``AuditEvent`` rows are
    never updated or deleted; each row commits to its predecessor's hash, so any
    edit to history is detectable. See ``app/audit/ledger.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """
    A timestamp column that is always timezone-aware UTC in Python.

    SQLite has no native timestamp type and stores datetimes as text with no
    offset, so a value written as ``2026-08-29 20:00:33+00:00`` comes back as a
    *naive* ``datetime`` -- the instant is right, the fact that it is UTC is not.
    ``DateTime(timezone=True)`` does not change that; on SQLite the flag is
    essentially advisory.

    That silent loss is not cosmetic. A naive datetime serialises to JSON without
    a ``Z``, and ``new Date("2026-08-29T20:00:33")`` in a browser parses it as
    *local* time. On a laptop in IST the audit ledger showed an event five and a
    half hours before it happened, and labelled it "IST" -- so the same recovery
    appeared to be paid on one date and recorded on another. An audit trail whose
    timestamps depend on the reader's timezone is not an audit trail.

    Comparisons are affected too: mixing naive and aware datetimes raises
    ``TypeError``, and the guardrails do date arithmetic on exactly these columns.

    Applying the normalisation in the column type fixes it once, for every table,
    rather than leaving each schema and each query to remember. The alternative --
    a Pydantic validator on each response field -- would have to be repeated on
    every timestamp in every schema, and would miss the comparison problem
    entirely because it only runs on the way out.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        """Normalise to UTC on the way into the database."""
        if value is None:
            return None
        if value.tzinfo is None:
            # Every writer in this codebase uses `utcnow()`, so a naive value is
            # a mistake rather than a local time. Treating it as UTC keeps the
            # instant correct instead of silently shifting it by the server's
            # offset, which is the failure mode that would be hardest to notice.
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        """Re-attach UTC on the way out, restoring what SQLite dropped."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for every table in the application."""


def utcnow() -> datetime:
    """
    Timezone-aware 'now'.

    Used as the default for every timestamp column. ``datetime.utcnow()`` is
    deliberately avoided: it returns a *naive* datetime that merely happens to
    hold UTC, which then compares incorrectly against aware datetimes.
    """
    return datetime.now(timezone.utc)


def as_utc(moment: datetime) -> datetime:
    """
    Return the same instant as a timezone-aware UTC datetime.

    A naive input is assumed to be UTC. That assumption is safe because
    ``UtcDateTime`` normalises everything on the way in and out of the database,
    so the only naive values in circulation are ones constructed by hand.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def utc_day_start(moment: datetime) -> datetime:
    """
    Return midnight UTC at the start of the given instant's day.

    "Today" is defined in UTC, not in the server's local zone, for the same reason
    money is stored in paise: the daily recovery budget must mean the same window
    regardless of where the process runs. A budget that resets at local midnight
    would give an IST deployment a five-and-a-half-hour overlap with a UTC one.

    Defined here, beside ``utcnow``, because more than one module needs it and a
    date boundary that is defined twice eventually gets defined differently --
    one copy using local midnight is all it takes to make the daily budget
    guardrail wrong for eight months of the year.
    """
    return as_utc(moment).replace(hour=0, minute=0, second=0, microsecond=0)


def _new_id(prefix: str) -> str:
    """
    Generate a readable, sortable-enough identifier such as ``case_9f2c1a...``.

    Prefixed opaque IDs (the convention Razorpay and Stripe both use) make logs
    and audit trails self-describing: you never have to look up which table a
    bare integer belongs to.
    """
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Customer(Base):
    """
    A paying customer of the merchant.

    The aggregate counters (``total_payments`` / ``successful_payments``) are
    denormalised on purpose: the ML propensity model needs a customer's historic
    success rate at inference time, and recomputing it with a COUNT on every
    prediction would put a table scan in the hot path of a payment flow.
    """

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _new_id("cust"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, default="")

    #: Set by the merchant's own risk processes. A flagged customer is denied
    #: automated recovery regardless of what the agent recommends.
    risk_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    total_payments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_payments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lifetime_value_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

    payments: Mapped[list["Payment"]] = relationship(back_populates="customer", cascade="all, delete-orphan")

    @property
    def prior_success_rate(self) -> float:
        """
        Share of this customer's past payments that succeeded.

        Returns the neutral prior 0.5 for a customer with no history rather than
        0.0, because "no data" is not the same as "always fails" -- scoring a new
        customer as hopeless would suppress legitimate recovery on their very
        first purchase.
        """
        if self.total_payments <= 0:
            return 0.5
        return self.successful_payments / self.total_payments


class Payment(Base):
    """
    One payment attempt, successful or not.

    A recovery attempt is *also* a ``Payment`` row -- flagged with
    ``is_recovery_attempt`` and pointed at the original via
    ``parent_payment_id``. Modelling it this way (rather than as a separate
    "recovery payment" table) means merchant revenue reporting sums one table and
    cannot accidentally omit recovered money.
    """

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _new_id("pay"))

    #: Gateway identifiers. Nullable because an order exists before a payment does.
    razorpay_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), index=True)

    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)

    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    method: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")  # PaymentMethod
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)          # PaymentStatus

    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # -- Failure detail, straight from the gateway -------------------------
    # Stored verbatim rather than pre-interpreted. The taxonomy that maps these
    # to a FailureCategory can be corrected later without losing the raw evidence
    # the classification was based on -- which is exactly what an audit needs.
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_source: Mapped[str | None] = mapped_column(String(32))       # bank | gateway | customer | internal
    error_step: Mapped[str | None] = mapped_column(String(64))         # payment_authorization | payment_initiation
    error_reason: Mapped[str | None] = mapped_column(String(120))      # Razorpay's machine reason
    error_description: Mapped[str | None] = mapped_column(Text)        # Human-readable text

    # -- Recovery lineage ---------------------------------------------------
    is_recovery_attempt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parent_payment_id: Mapped[str | None] = mapped_column(ForeignKey("payments.id"), index=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False)

    customer: Mapped["Customer"] = relationship(back_populates="payments")

    __table_args__ = (
        Index("ix_payments_status_created", "status", "created_at"),
    )


class RecoveryCase(Base):
    """
    The decision record for one failed payment: what the AI concluded, what the
    guardrails permitted, who approved it, and how it ended.

    This table is the answer to every question the audit trail is supposed to
    answer, denormalised into one row for fast rendering, with the full blow-by-
    blow available in ``audit_events`` and ``agent_tool_calls``.
    """

    __tablename__ = "recovery_cases"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _new_id("case"))

    #: The failed payment this case is trying to rescue. Unique: one open case per
    #: payment, enforced in the database rather than trusted to application code.
    original_payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)  # RecoveryStatus

    # -- What the agent concluded ------------------------------------------
    failure_category: Mapped[str] = mapped_column(String(32), nullable=False)          # FailureCategory
    classification_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)                  # RecoveryStrategy
    agent_rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    customer_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    agent_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="rule_based")  # AgentMode

    #: ML-predicted probability that a recovery attempt would succeed.
    propensity_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    propensity_model_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # -- What the guardrails decided ---------------------------------------
    guardrail_decision: Mapped[str] = mapped_column(String(24), nullable=False)  # GuardrailDecision
    #: Full per-rule evaluation, stored so the UI can show *which* rule fired and
    #: with what observed-vs-limit numbers, months after the fact.
    guardrail_evaluations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: Snapshot of the policy limits in force at decision time. Without this, a
    #: later config change would silently rewrite the meaning of old decisions.
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # -- Money (never derived from LLM output) ------------------------------
    #: Copied from the original payment by the service layer. The agent has no
    #: tool that can set or alter this value.
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # -- Human decision -----------------------------------------------------
    approved_by: Mapped[str | None] = mapped_column(String(120))
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    rejected_by: Mapped[str | None] = mapped_column(String(120))
    rejected_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    # -- Outcome ------------------------------------------------------------
    recovered_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    recovered_amount_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False)

    attempts: Mapped[list["RecoveryAttempt"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="RecoveryAttempt.attempt_number"
    )

    __table_args__ = (
        # One case per failed payment. This is the database-level half of the
        # "no duplicate recovery" guarantee; R6_DUPLICATE_ORDER is the
        # application-level half that produces a friendly explanation instead of
        # an integrity error.
        UniqueConstraint("original_payment_id", name="uq_case_per_payment"),
        Index("ix_cases_status_created", "status", "created_at"),
    )


class RecoveryAttempt(Base):
    """
    One concrete try at collecting the money: a Razorpay order plus its outcome.

    Separated from ``RecoveryCase`` because attempts are the unit the
    ``max_recovery_attempts`` guardrail counts, and because each attempt carries
    its own idempotency key.
    """

    __tablename__ = "recovery_attempts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _new_id("att"))
    case_id: Mapped[str] = mapped_column(ForeignKey("recovery_cases.id"), nullable=False, index=True)

    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)

    razorpay_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), index=True)
    gateway_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="simulated")  # GatewayMode

    #: Deterministic key derived from (case_id, attempt_number). Replaying the
    #: same approval -- a double-clicked button, a retried HTTP call, a duplicated
    #: webhook -- reuses this row instead of charging the customer twice.
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False)  # RecoveryStatus subset
    failure_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    case: Mapped["RecoveryCase"] = relationship(back_populates="attempts")

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_attempt_idempotency"),
        UniqueConstraint("case_id", "attempt_number", name="uq_attempt_number_per_case"),
    )


class AuditEvent(Base):
    """
    Append-only, hash-chained ledger entry.

    Each row stores the hash of the previous row, so the sequence forms a chain:
    changing or deleting any historical row breaks every hash after it, and
    ``GET /api/audit/verify`` will report exactly where. That turns "trust our
    logs" into "verify our logs", which is the whole point of an audit trail for
    autonomous financial actions.

    Rows are never UPDATEd or DELETEd. Corrections are appended as new events.
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _new_id("evt"))
    #: Monotonic position in the chain, assigned by the ledger as ``max + 1`` and
    #: protected by the unique constraint below. Gaplessness is what
    #: ``verify_chain`` checks: a missing sequence is itself evidence that a row
    #: was removed.
    #:
    #: Two writers racing on the same allocation is a real possibility -- FastAPI
    #: runs sync endpoints on a threadpool -- so the ledger serialises the
    #: allocation and retries once if it loses. It does not hold a database-level
    #: lock; the unique constraint is the thing that makes a lost race loud
    #: rather than silent.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)

    case_id: Mapped[str | None] = mapped_column(String(40), index=True)
    payment_id: Mapped[str | None] = mapped_column(String(40), index=True)

    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)  # AuditEventType
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)              # ActorType
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False, default="system")

    #: One-line human summary, written for a merchant operator, not a developer.
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    #: Structured detail (rule outputs, gateway ids, model scores). This dict is
    #: what gets hashed, so it is the part that cannot be quietly rewritten.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False, index=True)


class AgentToolCall(Base):
    """
    One step of the agent's reasoning loop: which tool it called, with what
    arguments, what came back, and how long it took.

    This is the explainability substrate. "Why did the AI decide that?" is
    answered by replaying these rows in order, and the recorded latency makes the
    cost of the reasoning loop visible rather than mysterious.
    """

    __tablename__ = "agent_tool_calls"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _new_id("tc"))
    #: Groups the steps of a single ``analyze`` invocation.
    run_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    case_id: Mapped[str | None] = mapped_column(String(40), index=True)
    payment_id: Mapped[str | None] = mapped_column(String(40), index=True)

    step: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Recorded so a reader can confirm no financial-capability tool was ever in
    #: the loop -- the claim is checkable from the data, not just from the code.
    capability: Mapped[str] = mapped_column(String(20), nullable=False, default="read_only")

    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
