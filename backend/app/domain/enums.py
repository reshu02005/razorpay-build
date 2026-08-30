"""
Canonical vocabulary for the whole system.

Every layer -- the AI agent, the guardrail engine, the database, the REST API and
the Next.js frontend -- speaks these exact strings. They are defined once, here.

Why `str, Enum` and not plain constants?
    Subclassing ``str`` means the members serialise to their value automatically
    (``json.dumps(FailureCategory.BANK_DECLINE) -> "bank_decline"``), so the same
    token travels unchanged from Python -> SQLite -> JSON -> TypeScript. It also
    means we can compare against raw strings coming back from the LLM without
    conversion boilerplate, while still getting a hard ``ValueError`` when the LLM
    hallucinates a category that does not exist. That validation-at-the-boundary
    behaviour is deliberate: an unknown category must fail loudly, never silently
    become "unknown".
"""

from __future__ import annotations

from enum import Enum


class PaymentStatus(str, Enum):
    """Lifecycle of a single payment attempt (mirrors Razorpay's own vocabulary)."""

    CREATED = "created"        # Order created, customer has not paid yet
    AUTHORIZED = "authorized"  # Funds held by the issuer, not yet settled
    CAPTURED = "captured"      # Money actually collected -- terminal success
    FAILED = "failed"          # Attempt failed -- the event RecoverAI reacts to
    REFUNDED = "refunded"      # Captured then returned


class FailureCategory(str, Enum):
    """
    Why a payment failed, normalised into categories that imply *different*
    recovery actions.

    This taxonomy is the single most important design artefact in the project:
    the whole thesis is "the correct recovery action differs per failure reason",
    so the categories are chosen by *what you should do about them*, not by what
    the gateway happens to call them.

    See ``app/agent/taxonomy.py`` for the Razorpay error-code -> category mapping
    and ``docs/03-GUARDRAILS.md`` for which categories are recoverable at all.
    """

    BANK_DECLINE = "bank_decline"                    # Issuer said no (generic)
    INSUFFICIENT_FUNDS = "insufficient_funds"        # No money right now -> retry LATER, not now
    UPI_TIMEOUT = "upi_timeout"                      # Collect request expired unanswered
    SESSION_EXPIRED = "session_expired"              # Checkout window closed
    GATEWAY_ERROR = "gateway_error"                  # Razorpay/acquirer side fault
    NETWORK_ERROR = "network_error"                  # Transport level failure
    AUTHENTICATION_FAILED = "authentication_failed"  # OTP / 3-D Secure not completed
    INVALID_INSTRUMENT = "invalid_instrument"        # Expired or wrong card details
    RISK_BLOCKED = "risk_blocked"                    # Fraud / stolen instrument -> NEVER recover
    CUSTOMER_ABANDONED = "customer_abandoned"        # Customer walked away from checkout
    UNKNOWN = "unknown"                              # Unmapped -> forced to manual review

    @property
    def is_recoverable(self) -> bool:
        """
        Hard, non-negotiable list of categories where an automated recovery
        attempt must never be offered.

        ``RISK_BLOCKED`` means the issuer or Razorpay's risk engine flagged the
        transaction. Re-presenting it is, at best, a wasted gateway call and at
        worst helps someone push a stolen card through. ``UNKNOWN`` is excluded
        because we cannot reason about a failure we could not classify -- absence
        of evidence is not evidence of safety.
        """
        return self not in (FailureCategory.RISK_BLOCKED, FailureCategory.UNKNOWN)


class PaymentMethod(str, Enum):
    """Instrument used for the attempt. Drives which recovery strategies apply."""

    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMI = "emi"
    UNKNOWN = "unknown"


class RecoveryStrategy(str, Enum):
    """
    The action the agent may recommend.

    Note what is *absent*: there is no "charge the customer" strategy and no
    "change the amount" strategy. The agent can only recommend how to re-present
    the same amount to the same customer. Everything money-moving lives behind
    human approval in ``app/services/recovery_service.py``.
    """

    RETRY_SAME_METHOD = "retry_same_method"    # Transient fault -> same rails will work
    SWITCH_TO_UPI = "switch_to_upi"            # Card path is broken, UPI is not
    SWITCH_TO_CARD = "switch_to_card"          # UPI path is broken, card is not
    SWITCH_TO_NETBANKING = "switch_to_netbanking"
    RETRY_LATER = "retry_later"                # e.g. insufficient funds -> wait for payday
    MANUAL_REVIEW = "manual_review"            # Escalate to a human operator
    NO_RECOVERY = "no_recovery"                # Correct answer is to do nothing

    @property
    def moves_money(self) -> bool:
        """True when acting on this strategy would create a new payment attempt."""
        return self not in (
            RecoveryStrategy.MANUAL_REVIEW,
            RecoveryStrategy.NO_RECOVERY,
            RecoveryStrategy.RETRY_LATER,
        )


class RecoveryStatus(str, Enum):
    """
    State machine for one recovery case (one case == one failed payment).

    Legal transitions are declared in ``ALLOWED_TRANSITIONS`` below and enforced
    centrally in ``RecoveryService._transition``. There is exactly one state
    machine for recovery in this codebase -- if you need a new lifecycle concept,
    extend this enum rather than adding a parallel boolean flag somewhere.

        Three different terminal states mean "no money will move", and they are
    kept distinct because their causes are different and a merchant needs to tell
    them apart: BLOCKED = a guardrail refused; NO_ACTION = the agent itself judged
    that doing nothing is correct; ESCALATED = a human must handle it off-platform.

    PROPOSED ---------> BLOCKED            (guardrails said DENY)
             |
             +-----------> NO_ACTION       (agent: nothing worth doing)
             +-----------> ESCALATED       (agent: needs a human)
             |
             +-----------> AWAITING_APPROVAL
                                |
                                +--> REJECTED  (human said no)
                                |
                                +--> APPROVED --> EXECUTING --> AWAITING_PAYMENT
                                                       |              |
                                                       |              +--> RECOVERED
                                                       |              +--> FAILED
                                                       |              +--> EXPIRED
                                                       +--> FAILED    (order creation failed)
    """

    PROPOSED = "proposed"                    # Agent produced a plan; guardrails evaluated
    BLOCKED = "blocked"                      # Guardrails denied -- terminal
    AWAITING_APPROVAL = "awaiting_approval"  # Waiting on a human decision
    REJECTED = "rejected"                    # Human declined -- terminal
    APPROVED = "approved"                    # Human said yes; nothing charged yet
    EXECUTING = "executing"                  # Creating the Razorpay order
    AWAITING_PAYMENT = "awaiting_payment"    # Order live, customer has the link
    RECOVERED = "recovered"                  # Payment captured + signature verified -- terminal
    FAILED = "failed"                        # Recovery attempt failed
    EXPIRED = "expired"                      # Recovery link timed out -- terminal
    NO_ACTION = "no_action"                  # Agent judged that nothing should be done -- terminal
    ESCALATED = "escalated"                  # Needs a human to handle manually, off-platform -- terminal

    @property
    def is_terminal(self) -> bool:
        return self in (
            RecoveryStatus.BLOCKED,
            RecoveryStatus.REJECTED,
            RecoveryStatus.RECOVERED,
            RecoveryStatus.EXPIRED,
            RecoveryStatus.NO_ACTION,
            RecoveryStatus.ESCALATED,
        )


#: The only state changes the system permits. Anything else raises
#: ``InvalidStateTransition``. Keeping this as data (rather than scattered ``if``
#: statements) means the audit trail and the frontend can both render the machine.
ALLOWED_TRANSITIONS: dict[RecoveryStatus, tuple[RecoveryStatus, ...]] = {
    RecoveryStatus.PROPOSED: (
        RecoveryStatus.BLOCKED,
        RecoveryStatus.AWAITING_APPROVAL,
        RecoveryStatus.NO_ACTION,
        RecoveryStatus.ESCALATED,
    ),
    RecoveryStatus.AWAITING_APPROVAL: (
        RecoveryStatus.APPROVED,
        RecoveryStatus.REJECTED,
        # Guardrails are re-evaluated at approval time, so a case can be refused
        # here and not only at proposal. BLOCKED means "a rule refused" whenever
        # that happened; it is not specific to the first evaluation.
        #
        # Without this edge the only terminal state reachable from here is
        # REJECTED, which is worse than useless: REJECTED means "a human
        # declined" and carries ``rejected_by``, so recording a machine's refusal
        # there would attribute a decision to a person who never made it. The
        # remaining option -- leaving the case in AWAITING_APPROVAL and trusting
        # the stored DENY verdict to keep the button dead -- means the status
        # column lies, and every queue and count built on it lies too.
        RecoveryStatus.BLOCKED,
        RecoveryStatus.EXPIRED,
    ),
    RecoveryStatus.APPROVED: (
        RecoveryStatus.EXECUTING,
    ),
    RecoveryStatus.EXECUTING: (
        RecoveryStatus.AWAITING_PAYMENT,
        RecoveryStatus.FAILED,
    ),
    RecoveryStatus.AWAITING_PAYMENT: (
        RecoveryStatus.RECOVERED,
        RecoveryStatus.FAILED,
        RecoveryStatus.EXPIRED,
    ),
    # A failed attempt may be re-proposed, but only if the attempt guardrail
    # (R1_MAX_ATTEMPTS) still has budget left. The guardrail -- not this table --
    # is what stops an infinite retry loop.
    RecoveryStatus.FAILED: (
        RecoveryStatus.PROPOSED,
    ),
    RecoveryStatus.BLOCKED: (),
    RecoveryStatus.REJECTED: (),
    RecoveryStatus.RECOVERED: (),
    RecoveryStatus.EXPIRED: (),
    RecoveryStatus.NO_ACTION: (),
    RecoveryStatus.ESCALATED: (),
}


#: Attempt statuses that mean "an order is live and payable right now".
#:
#: Lives here rather than in a service module because two independent callers
#: need it -- the guardrail context the service builds, and the read-only preview
#: the agent's ``check_recovery_eligibility`` tool builds -- and a service-level
#: home forced the second to keep its own copy. The copies drifted: the agent's
#: included ``APPROVED``, a status no ``RecoveryAttempt`` row is ever assigned, so
#: it was dead weight that looked meaningful.
#:
#: Proposing a second recovery while one is still open is how a customer gets
#: charged twice, so the definition of "still open" has to be one definition.
OPEN_ATTEMPT_STATUSES: tuple[str, ...] = (
    RecoveryStatus.EXECUTING.value,
    RecoveryStatus.AWAITING_PAYMENT.value,
)


class GuardrailDecision(str, Enum):
    """
    Verdict of a single guardrail rule, and of the engine as a whole.

    Ordering matters when aggregating many rules into one verdict: DENY beats
    REQUIRE_APPROVAL beats ALLOW. That "most restrictive wins" rule is what makes
    the engine safe to extend -- adding a rule can only ever make the system more
    conservative, never less.
    """

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"

    @property
    def severity(self) -> int:
        return {"allow": 0, "require_approval": 1, "deny": 2}[self.value]


class ActorType(str, Enum):
    """Who caused an audit event. Answers "who approved it?" in the audit trail."""

    AGENT = "agent"      # The AI, acting within its read-only tool budget
    HUMAN = "human"      # A merchant operator clicking approve/reject
    SYSTEM = "system"    # Scheduled/internal transitions (expiry sweeps, seeding)
    WEBHOOK = "webhook"  # Razorpay calling us back


class AuditEventType(str, Enum):
    """
    Every entry that can appear in the tamper-evident ledger.

    The set is closed on purpose: an auditor should be able to read this enum and
    know the complete list of things the system can do to money.
    """

    PAYMENT_FAILED = "payment_failed"
    ANALYSIS_STARTED = "analysis_started"
    FAILURE_CLASSIFIED = "failure_classified"
    PROPENSITY_SCORED = "propensity_scored"
    STRATEGY_PROPOSED = "strategy_proposed"
    GUARDRAILS_EVALUATED = "guardrails_evaluated"
    RECOVERY_BLOCKED = "recovery_blocked"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    RECOVERY_ORDER_CREATED = "recovery_order_created"
    RECOVERY_LINK_SENT = "recovery_link_sent"
    PAYMENT_VERIFIED = "payment_verified"
    RECOVERY_SUCCEEDED = "recovery_succeeded"
    RECOVERY_FAILED = "recovery_failed"
    RECOVERY_EXPIRED = "recovery_expired"
    WEBHOOK_RECEIVED = "webhook_received"
    AGENT_DEGRADED = "agent_degraded"          # LLM unavailable -> rule engine took over
    GATEWAY_SIMULATED = "gateway_simulated"    # No Razorpay keys -> simulated gateway used


class ToolCapability(str, Enum):
    """
    Security classification for agent tools.

    ``FINANCIAL`` exists so that it can be asserted *never* to appear in the
    agent's toolset. ``tests/test_agent_tool_safety.py`` fails the build if a
    financial tool is ever registered for the LLM -- the safety property is
    enforced by a test, not by a code review convention.
    """

    READ_ONLY = "read_only"            # Pure lookups; no writes at all
    WRITE_PROPOSAL = "write_proposal"  # Persists a *recommendation*; moves no money
    FINANCIAL = "financial"            # Moves money -- never exposed to the LLM


class AgentMode(str, Enum):
    """Which reasoning engine produced a plan. Surfaced in the UI for honesty."""

    LLM = "llm"                # Google Gemini with function calling
    RULE_BASED = "rule_based"  # Deterministic fallback (no API key, or LLM failed)


class GatewayMode(str, Enum):
    """Which payment backend is live. Surfaced in the UI so a demo never lies."""

    RAZORPAY_TEST = "razorpay_test"  # Real Razorpay Test Mode API calls
    SIMULATED = "simulated"          # In-process simulator (no credentials needed)
