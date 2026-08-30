"""
Pydantic v2 schemas -- the wire contract between FastAPI and the Next.js client.

These models are the *only* shapes that cross the HTTP boundary. ORM objects are
never returned directly, for three reasons:

1.  It stops internal columns leaking into a public response by accident.
2.  It gives FastAPI a real OpenAPI schema, which is what makes the mirrored
    TypeScript types in ``frontend/src/lib/types.ts`` trustworthy.
3.  It converts paise to rupees in exactly one place, so no component in the UI
    ever has to remember the unit.

Naming convention: ``*Out`` = server -> client, ``*In`` = client -> server.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import (
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
    ToolCapability,
)

# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class ORMModel(BaseModel):
    """Base for schemas built straight from SQLAlchemy rows."""

    model_config = ConfigDict(from_attributes=True)


def paise_to_rupees(paise: int) -> float:
    """
    Presentation-only conversion.

    Rupees appear at the API edge and nowhere else. All arithmetic, comparison
    and storage upstream of this call is integer paise.
    """
    return round(paise / 100, 2)


# ---------------------------------------------------------------------------
# Customers & payments
# ---------------------------------------------------------------------------


class CustomerOut(ORMModel):
    id: str
    name: str
    email: str
    phone: str = ""
    risk_flagged: bool = False
    total_payments: int = 0
    successful_payments: int = 0
    prior_success_rate: float = 0.5
    lifetime_value_paise: int = 0
    lifetime_value_rupees: float = 0.0


class PaymentOut(ORMModel):
    id: str
    customer_id: str
    customer: CustomerOut | None = None

    amount_paise: int
    amount_rupees: float
    currency: str = "INR"
    method: PaymentMethod
    status: PaymentStatus
    description: str = ""

    razorpay_order_id: str | None = None
    razorpay_payment_id: str | None = None

    error_code: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None
    error_description: str | None = None

    is_recovery_attempt: bool = False
    parent_payment_id: str | None = None

    #: Present on failed payments only: the id of the case opened for it, or
    #: ``None`` when the failure has not been analysed yet. Lets the payments
    #: table render "Analyse" vs "View case" without a second round trip.
    recovery_case_id: str | None = None

    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


class GuardrailEvaluationOut(BaseModel):
    """
    Result of one guardrail rule.

    ``observed`` and ``limit`` are carried as display strings so the UI can show
    "3 attempts vs limit 2" without knowing anything about the rule's internals.
    That keeps new rules from requiring frontend changes.
    """

    rule_id: str = Field(examples=["R1_MAX_ATTEMPTS"])
    name: str = Field(examples=["Maximum recovery attempts"])
    description: str
    decision: GuardrailDecision
    passed: bool
    reason: str
    observed: str | None = None
    limit: str | None = None
    #: False when the strategy creates no payment attempt, so the rule had nothing
    #: to constrain. Distinguishes "checked and cleared" from "never consulted",
    #: which are otherwise identical on the wire.
    applicable: bool = True


class GuardrailVerdictOut(BaseModel):
    """Aggregate of every rule: the most restrictive decision wins."""

    decision: GuardrailDecision
    evaluations: list[GuardrailEvaluationOut]
    blocking_rules: list[str] = Field(default_factory=list, description="rule_ids that returned DENY")
    approval_rules: list[str] = Field(default_factory=list, description="rule_ids that forced human approval")
    summary: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AgentToolCallOut(ORMModel):
    """One step of the reasoning trace, for the explainability panel."""

    id: str
    run_id: str
    step: int
    tool_name: str
    capability: ToolCapability
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    error: str | None = None
    latency_ms: int = 0
    created_at: datetime


class AgentRecoveryPlan(BaseModel):
    """
    The structured object the agent must produce -- LLM or rule-based alike.

    Two properties make this safe to accept from a language model:

    *   Every field is an enum or a bounded scalar. There is no free-form field
        that can express an amount, an account or an instruction to the gateway.
    *   ``model_config`` forbids extra keys, so a model that invents
        ``"amount_override": 100`` fails validation instead of being ignored.

    Note the absence of any money field. The recovery amount is copied from the
    original payment by the service layer; the agent is structurally incapable of
    influencing it.
    """

    model_config = ConfigDict(extra="forbid")

    failure_category: FailureCategory
    confidence: float = Field(ge=0.0, le=1.0)
    strategy: RecoveryStrategy
    #: Why, in the operator's language. Shown verbatim in the approval UI, so a
    #: human is judging the agent's actual reasoning rather than a summary of it.
    rationale: str = Field(min_length=10, max_length=1200)
    #: What the customer would be told. Kept separate from the rationale because
    #: the two audiences need different tone and different detail.
    customer_message: str = Field(min_length=10, max_length=500)
    #: Optional signals the model wants to record (e.g. "error_code=BAD_REQUEST").
    evidence: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("rationale", "customer_message")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class AgentRunOut(BaseModel):
    """Metadata about one analysis run, independent of its conclusions."""

    run_id: str
    mode: AgentMode
    model: str | None = None
    steps: int
    total_latency_ms: int
    degraded_reason: str | None = Field(
        default=None,
        description="Set when the LLM path was unavailable and the rule engine was used instead.",
    )


# ---------------------------------------------------------------------------
# ML propensity
# ---------------------------------------------------------------------------


class PropensityResultOut(BaseModel):
    """Prediction from the recovery-propensity model."""

    score: float = Field(ge=0.0, le=1.0, description="P(recovery attempt succeeds)")
    model_version: str
    #: Ordered, human-readable drivers behind this particular score.
    top_factors: list[str] = Field(default_factory=list)
    #: True when the trained artefact was missing and the documented heuristic
    #: fallback produced the score. Surfaced so nobody mistakes it for a model.
    is_fallback: bool = False


# ---------------------------------------------------------------------------
# Recovery cases
# ---------------------------------------------------------------------------


class RecoveryAttemptOut(ORMModel):
    id: str
    attempt_number: int
    strategy: RecoveryStrategy
    amount_paise: int
    amount_rupees: float
    #: Typed, like every other status on the wire. An attempt only ever holds the
    #: subset {executing, awaiting_payment, recovered, failed, expired}, all of
    #: which are valid members -- and typing it means a garbage value in the
    #: column fails at the boundary instead of serialising into the UI unnoticed.
    status: RecoveryStatus
    gateway_mode: GatewayMode
    razorpay_order_id: str | None = None
    razorpay_payment_id: str | None = None
    failure_reason: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class RecoveryCaseSummaryOut(ORMModel):
    """Row shape for list views. Deliberately light -- no trace, no evaluations."""

    id: str
    original_payment_id: str
    customer_id: str
    customer_name: str = ""
    status: RecoveryStatus
    failure_category: FailureCategory
    strategy: RecoveryStrategy
    guardrail_decision: GuardrailDecision
    propensity_score: float
    agent_mode: AgentMode
    amount_paise: int
    amount_rupees: float
    attempt_count: int
    created_at: datetime
    updated_at: datetime


class RecoveryCaseOut(RecoveryCaseSummaryOut):
    """Full case detail: everything the approval screen needs in one response."""

    classification_confidence: float
    agent_rationale: str
    customer_message: str
    propensity_model_version: str = ""
    propensity_is_fallback: bool = False

    guardrail_evaluations: list[GuardrailEvaluationOut] = Field(default_factory=list)
    policy_snapshot: dict[str, Any] = Field(default_factory=dict)

    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_by: str | None = None
    rejected_at: datetime | None = None
    rejection_reason: str | None = None

    recovered_at: datetime | None = None
    recovered_amount_paise: int = 0
    recovered_amount_rupees: float = 0.0
    failure_note: str | None = None
    expires_at: datetime | None = None

    original_payment: PaymentOut | None = None
    customer: CustomerOut | None = None
    attempts: list[RecoveryAttemptOut] = Field(default_factory=list)

    #: Precomputed by the service so the UI never re-implements policy logic.
    #: If the button's enabled state were derived in React, the frontend would
    #: become a second, divergent copy of the guardrail engine.
    can_approve: bool = False
    can_reject: bool = False
    approval_blocked_reason: str | None = None


class CheckoutSessionOut(BaseModel):
    """Everything the customer-facing recovery page needs to open Checkout."""

    case_id: str
    attempt_id: str
    order_id: str
    amount_paise: int
    amount_rupees: float
    currency: str
    #: Razorpay publishable key, or ``None`` in simulated mode. It is safe to
    #: expose: the key id is public by design; the secret never leaves the server.
    razorpay_key_id: str | None = None
    gateway_mode: GatewayMode
    customer_name: str
    customer_email: str
    customer_phone: str
    description: str
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditEventOut(ORMModel):
    id: str
    sequence: int
    case_id: str | None = None
    payment_id: str | None = None
    event_type: AuditEventType
    actor_type: ActorType
    actor_id: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str
    created_at: datetime


class AuditChainVerificationOut(BaseModel):
    """
    Result of recomputing the ledger's hash chain from genesis.

    A demo that only *claims* immutability proves nothing. This endpoint
    recomputes every hash and names the first sequence number that fails, which
    is what makes the guarantee inspectable by a reviewer.
    """

    valid: bool
    events_checked: int
    head_hash: str | None = None
    broken_at_sequence: int | None = None
    message: str


# ---------------------------------------------------------------------------
# Metrics & policy
# ---------------------------------------------------------------------------


class DashboardMetricsOut(BaseModel):
    """Merchant-level numbers for the landing dashboard."""

    total_volume_paise: int
    captured_volume_paise: int
    failed_volume_paise: int
    recoverable_volume_paise: int = Field(description="Failed volume that passed guardrails as recoverable")
    recovered_volume_paise: int

    total_volume_rupees: float
    captured_volume_rupees: float
    failed_volume_rupees: float
    recoverable_volume_rupees: float
    recovered_volume_rupees: float

    total_payments: int
    failed_payments: int
    unanalysed_failures: int

    cases_total: int
    cases_awaiting_approval: int
    cases_blocked: int
    cases_recovered: int
    cases_failed: int

    #: recovered_volume / recoverable_volume, as a percentage. 0.0 when there is
    #: nothing recoverable -- reported rather than hidden, so an empty database
    #: reads as "0% of nothing" instead of a crash or a misleading blank.
    recovery_rate_pct: float
    failure_rate_pct: float

    #: Value of recovery orders created today, against the daily budget cap.
    daily_budget_used_paise: int
    daily_budget_limit_paise: int


class FailureBreakdownItem(BaseModel):
    category: FailureCategory
    count: int
    volume_paise: int
    volume_rupees: float
    recovered_count: int


class PolicyOut(BaseModel):
    """
    The active guardrail configuration, exposed read-only.

    Read-only on purpose. If policy could be edited through the same API the
    agent's flow uses, the guardrails would be reachable from the automated path,
    and a limit an automated system can raise is not a limit.
    """

    max_recovery_attempts: int
    recovery_cooldown_seconds: int
    high_value_review_threshold_paise: int
    max_recovery_amount_paise: int
    daily_recovery_budget_paise: int
    max_cases_per_customer_per_day: int
    min_propensity_score: float
    max_payment_age_hours: int
    require_human_approval: bool
    auto_approve_enabled: bool
    auto_approve_max_paise: int
    auto_approve_min_propensity: float
    recovery_link_ttl_minutes: int
    non_recoverable_categories: list[FailureCategory]
    rules: list[dict[str, str]] = Field(description="Static catalogue: rule_id, name, description")


class SystemStatusOut(BaseModel):
    """Honest self-report of which subsystems are live vs simulated."""

    app: str
    version: str
    environment: str
    agent_mode: AgentMode
    gemini_model: str | None = None
    gateway_mode: GatewayMode
    ml_model_loaded: bool
    ml_model_version: str | None = None
    database: str
    #: Only conditions the typed fields above cannot express -- an interpreter
    #: outside the tested range, or a Gemini key that is present but unusable.
    #: Degraded modes themselves are read from `agent_mode`, `gateway_mode` and
    #: `ml_model_loaded`, so that no consumer has to render the same fact twice.
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class SimulateFailureIn(BaseModel):
    """
    Demo helper: manufacture a realistic failed payment.

    Exists because a submission reviewer cannot make a real card decline on
    demand. Every field is optional; omitted fields are drawn from the seeded
    scenario catalogue so a single empty POST produces a plausible failure.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: str | None = None
    amount_paise: int | None = Field(default=None, ge=100, le=100_000_000)
    method: PaymentMethod | None = None
    #: Named scenario from ``app/db/seed.py`` (e.g. ``"bank_decline_card"``).
    scenario: str | None = None
    description: str | None = Field(default=None, max_length=255)


class FailureScenarioOut(BaseModel):
    """
    One entry from the demo failure catalogue.

    Served so the "simulate a failed payment" picker can be built from the
    catalogue the server actually holds. The alternative -- a hard-coded list in
    the React component -- was tried and drifted immediately: six of its eight
    ids named scenarios that had never existed, so the picker's most important
    option (the fraud case the guardrails must refuse) returned a 404. A list of
    server-side keys maintained by hand on the client is a list that will be
    wrong.
    """

    key: str = Field(description="Pass this as `scenario` to POST /api/payments/simulate-failure")
    label: str
    method: PaymentMethod
    expected_category: FailureCategory
    error_reason: str
    error_description: str


class AnalyzeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Force the deterministic planner even when a Gemini key is configured.
    #: Used by tests and to demonstrate the degraded path on stage.
    force_rule_based: bool = False


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_by: str = Field(min_length=1, max_length=120, description="Operator identity, for the audit trail")
    note: str | None = Field(default=None, max_length=500)


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rejected_by: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=500)


class VerifyPaymentIn(BaseModel):
    """
    Payload posted back by Razorpay Checkout on success.

    The signature is verified server-side with HMAC-SHA256 before anything is
    marked recovered. A client-side "payment succeeded" callback is a claim, not
    proof -- trusting it would let anyone mark any case recovered with curl.
    """

    model_config = ConfigDict(extra="forbid")

    razorpay_order_id: str = Field(min_length=1, max_length=64)
    razorpay_payment_id: str = Field(min_length=1, max_length=64)
    razorpay_signature: str = Field(min_length=1, max_length=256)


class MarkAttemptFailedIn(BaseModel):
    """Demo helper: force the current attempt to fail, to exercise the failure path."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="Customer did not complete the payment", max_length=500)


class ErrorOut(BaseModel):
    """Uniform error envelope for every non-2xx response."""

    error: str = Field(description="Stable machine-readable code, e.g. 'guardrail_denied'")
    message: str = Field(description="Human-readable explanation")
    detail: dict[str, Any] | None = None


PaymentStatusFilter = Literal["all", "failed", "captured", "created", "authorized", "refunded"]
