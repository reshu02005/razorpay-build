"""
The agent's tool surface -- and the security boundary of the entire product.

**The LLM has no tool that moves money.** Not "the prompt tells it not to", not
"we check the output afterwards": there is no callable in this file that creates
a Razorpay order, captures a payment, issues a refund or writes an amount. The
most powerful thing the model can do here is *submit a recommendation*, which
lands in a Pydantic object with no money field on it at all.

How that claim is enforced, in four layers:

1.  **Capability typing.** Every tool declares a :class:`ToolCapability`. Six are
    ``READ_ONLY``; exactly one is ``WRITE_PROPOSAL``. ``FINANCIAL`` exists in the
    enum solely so it can be asserted never to appear here.
2.  **A runtime assertion.** :func:`assert_no_financial_tools` runs on every call
    to :meth:`ToolRegistry.specs` -- i.e. every time a toolset is handed to the
    model. It is the line that stops a future contributor from "helpfully" adding
    a ``create_order`` tool because the demo would flow better.
3.  **An independent test.** ``tests/test_agent_tool_safety.py`` re-checks the
    property from outside this module, so deleting the assertion above does not
    silently delete the guarantee.
4.  **Structural impossibility.** ``AgentRecoveryPlan`` (see
    ``app/domain/schemas.py``) has no amount field and forbids extra keys, so a
    model that invents ``{"amount_override": 1}`` fails validation rather than
    being quietly ignored.

The registry is also *pinned*: it is constructed for one specific ``Payment`` and
will not read any other. A tool that accepted an arbitrary ``payment_id`` would
let a prompt-injected model enumerate the merchant's entire book through what
looks like a harmless lookup.

Tool results are plain JSON-serialisable dicts, because they are fed straight
back to Gemini as function responses and persisted verbatim into
``agent_tool_calls`` for the explainability trace.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.taxonomy import PLAYBOOK, classify_error
from app.config import Settings
from app.db.models import utc_day_start, Customer, Payment, RecoveryAttempt, RecoveryCase, utcnow
from app.domain.enums import (
    OPEN_ATTEMPT_STATUSES,
    FailureCategory,
    PaymentMethod,
    RecoveryStatus,
    RecoveryStrategy,
    ToolCapability,
)
from app.domain.schemas import AgentRecoveryPlan, paise_to_rupees
from app.ml.features import build_feature_row
from app.ml.predictor import get_predictor
from app.policy.engine import PolicyEngine
from app.policy.rules import GuardrailContext

logger = logging.getLogger(__name__)

#: The only tool that ends the reasoning loop. Named once here so that llm.py and
#: orchestrator.py cannot drift from tools.py on the spelling of it.
TERMINAL_TOOL = "submit_recovery_plan"

#: How many of the customer's previous payments a single history lookup returns.
#: Five is enough to see a pattern ("three declines in a row") without turning a
#: tool result into a data dump that crowds out the model's context window.
_RECENT_PAYMENT_LIMIT = 5



# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_utc(value: datetime | None) -> datetime | None:
    """
    Return ``value`` as a timezone-aware UTC datetime.

    SQLite has no native timestamp type: SQLAlchemy serialises
    ``DateTime(timezone=True)`` columns to a string that carries no offset, so
    values read back are *naive* even though they were written aware. Comparing
    one of those against ``utcnow()`` raises ``TypeError: can't compare offset-naive
    and offset-aware datetimes`` -- in the middle of a guardrail evaluation.

    Normalising here, at the point where persisted data re-enters the program, is
    cheaper and more honest than teaching every downstream caller to be paranoid.

    Args:
        value: A datetime read from the database, or ``None``.

    Returns:
        The same instant with ``tzinfo=UTC`` attached, or ``None``.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 rendering for tool results, which must be JSON-serialisable."""
    aware = _as_utc(value)
    return aware.isoformat() if aware is not None else None


def _coerce_stored_enum(enum_cls: Any, raw: str | None, default: Any) -> Any:
    """
    Convert a value *we* wrote to the database back into its enum member.

    Deliberately lenient, and deliberately asymmetric with how model-supplied
    values are handled: a legacy or hand-edited row holding an unrecognised
    string should degrade to a sane default rather than crash an analysis, while
    a hallucinated enum coming *from the LLM* must fail loudly so the model gets
    a correctable error. Trusting our own data more than the model's is the whole
    point of the boundary.
    """
    if raw is None:
        return default
    try:
        return enum_cls(raw)
    except ValueError:
        logger.warning("Unrecognised %s value in database: %r", enum_cls.__name__, raw)
        return default


def _require_enum(enum_cls: Any, raw: Any, field: str) -> Any:
    """
    Convert a *model-supplied* string into an enum member, or raise.

    Raises:
        ValueError: with the full list of legal values. The message is written
            for the model, not for a developer: :meth:`ToolRegistry.call` turns
            it into an observation, and a model told exactly which tokens are
            legal will usually fix itself on the next step.
    """
    if isinstance(raw, enum_cls):
        return raw
    try:
        return enum_cls(str(raw))
    except ValueError:
        legal = ", ".join(m.value for m in enum_cls)
        raise ValueError(f"Invalid {field}={raw!r}. Must be one of: {legal}") from None


# ---------------------------------------------------------------------------
# Tool specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """
    One callable exposed to the model, plus the metadata that governs it.

    Attributes:
        name: Function name the model calls. Stable -- it appears in the audit
            trace and in the trained prompt copy.
        description: The model's *only* documentation for this tool. Written for
            the model, not for a human reader of this file.
        capability: Security classification. Asserted never to be ``FINANCIAL``.
        parameters: JSON Schema object, passed through to Gemini as a function
            declaration.
        fn: The implementation. Must return a JSON-serialisable dict.
    """

    name: str
    description: str
    capability: ToolCapability
    parameters: dict[str, Any]
    fn: Callable[..., dict[str, Any]]


def assert_no_financial_tools(specs: list[ToolSpec]) -> None:
    """
    Fail loudly if a money-moving tool is ever registered for the LLM.

    This is enforcement, not decoration. The project's central claim is that the
    model cannot move money; that claim is worth exactly as much as the mechanism
    that keeps it true when someone later adds a tool in a hurry. Called on every
    :meth:`ToolRegistry.specs` invocation -- that is, every time a toolset is
    about to be handed to Gemini -- so the failure happens at the moment of
    exposure rather than during a code review that might not happen.

    ``AssertionError`` is raised explicitly rather than via the ``assert``
    statement: ``assert`` is stripped by ``python -O``, and a safety invariant
    that disappears under an optimisation flag is not an invariant. The
    alternative considered was ``ConfigurationError`` (a 500 to the merchant),
    but this is a programming error found at start-up, not a runtime condition a
    merchant can act on, so the exception type that matches the function's name
    won.

    Args:
        specs: The tool specifications about to be exposed to the model.

    Raises:
        AssertionError: if any spec declares ``ToolCapability.FINANCIAL``.
    """
    offenders = [s.name for s in specs if s.capability == ToolCapability.FINANCIAL]
    if offenders:
        raise AssertionError(
            "Financial-capability tools must never be exposed to the LLM. "
            f"Offending tools: {', '.join(sorted(offenders))}. "
            "Money-moving actions belong behind human approval in "
            "app/services/recovery_service.py, not in the agent loop."
        )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """
    The toolset for one analysis of one payment.

    Constructed per analysis rather than shared, for two reasons:

    *   **Pinning.** Holding the ``Payment`` as instance state is what makes
        "the agent can only read the payment it was asked about" a structural
        property instead of a validation rule someone could forget.
    *   **Terminal state.** ``submitted_plan`` records the plan the model
        proposed, so the loop in ``llm.py`` has an unambiguous stop condition
        that does not depend on parsing free text.
    """

    def __init__(self, db: Session, settings: Settings, payment: Payment) -> None:
        """
        Args:
            db: Live session. The registry reads through it and never writes;
                persistence of the trace is the orchestrator's job.
            settings: Active configuration, so ``get_recovery_policy`` reports
                the limits actually in force rather than a hard-coded copy.
            payment: The single failed payment under analysis.
        """
        self._db = db
        self._settings = settings
        self.payment = payment

        # Resolved once. A tool that re-queried the customer on every call would
        # let a chatty model turn one analysis into N round trips to SQLite.
        self.customer: Customer | None = db.get(Customer, payment.customer_id)

        self._policy = PolicyEngine(settings)

        #: Set by ``submit_recovery_plan`` on successful validation. ``None``
        #: until then, which is precisely the loop's termination signal.
        self.submitted_plan: AgentRecoveryPlan | None = None

        self._specs: tuple[ToolSpec, ...] = self._build_specs()

    # -- Derived values shared by tools and by the orchestrator ------------

    @property
    def payment_age_hours(self) -> float:
        """
        Hours since the payment failed.

        Exposed as a property because three separate consumers need it -- the
        payment-details tool, the ML feature row and the freshness guardrail --
        and three independent subtractions is three chances to disagree about
        which "now" and which timezone were used.
        """
        created = _as_utc(self.payment.created_at) or utcnow()
        return max(0.0, (utcnow() - created).total_seconds() / 3600.0)

    def _require_customer(self) -> Customer:
        """
        Return the pinned payment's customer, or raise.

        A payment row whose customer is missing is a data-integrity fault. It is
        raised as a plain ``LookupError`` rather than a domain error because
        :meth:`call` converts every exception into an observation, so the type
        never reaches the HTTP layer -- only the message reaches the model.
        """
        if self.customer is None:
            raise LookupError(
                f"Customer {self.payment.customer_id} referenced by payment "
                f"{self.payment.id} does not exist."
            )
        return self.customer

    # -- Public surface ----------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        """
        Return the tool specifications to expose to the model.

        Returns:
            A fresh list of :class:`ToolSpec`, safety-checked.

        Raises:
            AssertionError: if a financial-capability tool has been registered.
        """
        specs = list(self._specs)
        # Checked here, on the way out, because this is the exact moment the
        # toolset becomes reachable by the model.
        assert_no_financial_tools(specs)
        return specs

    def get(self, name: str) -> ToolSpec:
        """
        Look up one tool by name.

        Args:
            name: Tool name as declared to the model.

        Returns:
            The matching :class:`ToolSpec`.

        Raises:
            KeyError: if no such tool exists. Unlike :meth:`call`, this is a
                programmer-facing lookup, so an unknown name is a bug worth
                surfacing rather than an observation worth returning.
        """
        for spec in self._specs:
            if spec.name == name:
                return spec
        raise KeyError(f"No such tool: {name!r}")

    def call(self, name: str, arguments: dict) -> dict:
        """
        Execute a tool by name and return its result.

        **This method never raises.** Any exception -- an unknown tool, a bad
        enum from the model, a dead database connection -- is caught and returned
        as ``{"error": "..."}``. That is deliberate: inside an agent loop an
        exception is not a failure of the request, it is an *observation*. A
        model that is told "Invalid strategy='refund_customer'. Must be one of:
        ..." fixes itself on the next step. A model that instead sees the whole
        analysis 500 leaves the merchant with a broken button and no recovery.

        The trade-off is that a genuine infrastructure fault looks, from the
        model's side, like a tool that politely declined. That is why the
        exception is also logged with a stack trace here, and why every step
        (including failed ones) is persisted to ``agent_tool_calls`` with
        ``ok=False``: the failure is invisible to the model, never to the auditor.

        Args:
            name: Tool name requested by the model.
            arguments: Keyword arguments, as parsed from the model's function call.

        Returns:
            The tool's result dict, or ``{"error": <message>}``.
        """
        try:
            spec = self.get(name)
        except KeyError:
            available = ", ".join(s.name for s in self._specs)
            return {"error": f"Unknown tool {name!r}. Available tools: {available}"}

        try:
            return spec.fn(**(arguments or {}))
        except Exception as exc:  # noqa: BLE001 -- see the docstring: this is the point
            logger.exception("Tool %s failed for payment %s", name, self.payment.id)
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -----------------------------------------------------------------
    # Tool 1 -- payment details (READ_ONLY)
    # -----------------------------------------------------------------

    def _tool_get_payment_details(self, payment_id: str | None = None) -> dict[str, Any]:
        """Return every field of the pinned payment, including raw failure detail."""
        # The parameter exists because a model naturally addresses a resource by
        # id, and a tool that silently ignored the argument would teach it that
        # ids do not matter. But the registry is pinned to one payment: asking
        # for a different one is refused rather than served. Without this, a
        # prompt-injected model could walk the merchant's whole payment table
        # through what looks like a read-only lookup.
        if payment_id and payment_id != self.payment.id:
            return {
                "error": (
                    f"This analysis is scoped to payment {self.payment.id}. "
                    f"Payment {payment_id} is not readable from here."
                )
            }

        p = self.payment
        return {
            "payment_id": p.id,
            "customer_id": p.customer_id,
            "amount_paise": p.amount_paise,
            # Rupees are included alongside paise purely for the model's benefit.
            # Every comparison in the codebase stays in integer paise; this field
            # is never read by a guardrail. The alternative -- paise only -- was
            # tried in reasoning and lost: the model's rationale is shown verbatim
            # to a merchant, and a model handed only paise writes "Rs 250000"
            # when it means Rs 2,500.
            "amount_rupees": paise_to_rupees(p.amount_paise),
            "currency": p.currency,
            "method": p.method,
            "status": p.status,
            "description": p.description,
            "error_code": p.error_code,
            "error_source": p.error_source,
            "error_step": p.error_step,
            "error_reason": p.error_reason,
            "error_description": p.error_description,
            "created_at": _iso(p.created_at),
            "age_hours": round(self.payment_age_hours, 2),
            "is_recovery_attempt": p.is_recovery_attempt,
            "parent_payment_id": p.parent_payment_id,
        }

    # -----------------------------------------------------------------
    # Tool 2 -- customer history (READ_ONLY)
    # -----------------------------------------------------------------

    def _tool_get_customer_history(self, customer_id: str | None = None) -> dict[str, Any]:
        """Return the pinned customer's aggregate history and recent outcomes."""
        customer = self._require_customer()
        if customer_id and customer_id != customer.id:
            # Same pinning rationale as get_payment_details: one payment, one
            # customer, no lateral movement.
            return {
                "error": (
                    f"This analysis is scoped to customer {customer.id}. "
                    f"Customer {customer_id} is not readable from here."
                )
            }

        recent = (
            self._db.execute(
                select(Payment)
                .where(Payment.customer_id == customer.id)
                .order_by(Payment.created_at.desc())
                .limit(_RECENT_PAYMENT_LIMIT)
            )
            .scalars()
            .all()
        )

        return {
            "customer_id": customer.id,
            "name": customer.name,
            "total_payments": customer.total_payments,
            "successful_payments": customer.successful_payments,
            # Note the neutral 0.5 prior for a customer with no history, computed
            # on the model (see Customer.prior_success_rate). "No data" is not
            # "always fails", and scoring a first-time buyer as hopeless would
            # suppress recovery on exactly the payment worth recovering.
            "prior_success_rate": round(customer.prior_success_rate, 4),
            "lifetime_value_paise": customer.lifetime_value_paise,
            "lifetime_value_rupees": paise_to_rupees(customer.lifetime_value_paise),
            # Surfaced to the model as context, but it is not the model's decision:
            # R12_CUSTOMER_RISK_FLAG denies recovery for a flagged customer no
            # matter how persuasive the rationale.
            "risk_flagged": customer.risk_flagged,
            "recent_payments": [
                {
                    "payment_id": rp.id,
                    "amount_paise": rp.amount_paise,
                    "amount_rupees": paise_to_rupees(rp.amount_paise),
                    "method": rp.method,
                    "status": rp.status,
                    "error_code": rp.error_code,
                    "created_at": _iso(rp.created_at),
                    "is_payment_under_analysis": rp.id == self.payment.id,
                }
                for rp in recent
            ],
        }

    # -----------------------------------------------------------------
    # Tool 3 -- failure classification (READ_ONLY)
    # -----------------------------------------------------------------

    def _tool_classify_failure_code(
        self,
        error_code: str | None = None,
        error_reason: str | None = None,
        error_description: str | None = None,
        error_source: str | None = None,
        error_step: str | None = None,
        method: str | None = None,
    ) -> dict[str, Any]:
        """Map raw gateway failure fields onto a recovery-relevant category."""
        match = classify_error(
            error_code=error_code,
            error_reason=error_reason,
            error_description=error_description,
            error_source=error_source,
            error_step=error_step,
            method=method,
        )
        playbook = PLAYBOOK[match.category]

        return {
            "failure_category": match.category.value,
            "confidence": round(match.confidence, 4),
            "matched_on": match.matched_on,
            "evidence": list(match.evidence),
            # Recoverability is a property of the taxonomy, not a judgement call.
            # Handing it to the model saves it from proposing a retry on a
            # risk-blocked card and then being denied by R3 a moment later.
            "is_recoverable": match.category.is_recoverable,
            # The playbook is the house view: what an experienced payments
            # operator would do for this category. It is advisory -- the model may
            # depart from it, but it then has to justify the departure in the
            # rationale a human reads before approving.
            "playbook": {
                "primary_strategy": playbook.primary_strategy.value,
                "alternate_strategy": playbook.alternate_strategy.value,
                "reasoning": playbook.reasoning,
                "suggested_customer_message": playbook.customer_message,
                "typical_success_rate": playbook.typical_success_rate,
            },
        }

    # -----------------------------------------------------------------
    # Tool 4 -- propensity scoring (READ_ONLY)
    # -----------------------------------------------------------------

    def _tool_score_recovery_propensity(
        self,
        failure_category: str,
        proposed_strategy: str,
        payment_method: str | None = None,
        attempt_number: int | None = None,
    ) -> dict[str, Any]:
        """Predict P(this recovery attempt succeeds) for a candidate strategy."""
        category = _require_enum(FailureCategory, failure_category, "failure_category")
        strategy = _require_enum(RecoveryStrategy, proposed_strategy, "proposed_strategy")

        # Method and attempt number default to the live record rather than being
        # required, because the payment row owns the method and the database owns
        # the attempt count. Letting the model restate them is a convenience; if
        # it omits them we use the truth instead of failing.
        method = (
            _require_enum(PaymentMethod, payment_method, "payment_method")
            if payment_method is not None
            else _coerce_stored_enum(PaymentMethod, self.payment.method, PaymentMethod.UNKNOWN)
        )
        attempt = int(attempt_number) if attempt_number is not None else self.next_attempt_number()

        customer = self._require_customer()

        # The amount is read from the pinned payment and is NOT a parameter of
        # this tool. That is the single most important line in this function.
        # Amount is one of the model's features, so an amount parameter would let
        # it probe "what if this were a smaller payment?" and then write a
        # rationale built on a number that is not the real one. The agent may
        # reason about the amount; it may not vary it.
        features = build_feature_row(
            failure_category=category.value,
            payment_method=method.value,
            proposed_strategy=strategy.value,
            amount_paise=self.payment.amount_paise,
            attempt_number=attempt,
            customer_prior_success_rate=customer.prior_success_rate,
            customer_total_payments=customer.total_payments,
            hours_since_failure=self.payment_age_hours,
        )
        result = get_predictor().predict(features)

        return {
            "score": round(result.score, 4),
            "model_version": result.model_version,
            "top_factors": list(result.top_factors),
            # Surfaced, not hidden: a heuristic score dressed up as a model
            # prediction is the kind of thing that erodes trust in every other
            # number on the screen.
            "is_fallback": result.is_fallback,
            # Echo the inputs the score was actually computed from. The trace has
            # to show which amount and which attempt number entered the model,
            # otherwise "the model said 0.71" is unfalsifiable.
            "scored_inputs": {
                "failure_category": category.value,
                "payment_method": method.value,
                "proposed_strategy": strategy.value,
                "attempt_number": attempt,
                "amount_paise": self.payment.amount_paise,
            },
            "minimum_required_by_policy": self._settings.min_propensity_score,
        }

    # -----------------------------------------------------------------
    # Tool 5 -- policy limits (READ_ONLY)
    # -----------------------------------------------------------------

    def _tool_get_recovery_policy(self) -> dict[str, Any]:
        """Return the guardrail limits currently in force."""
        s = self._settings
        # Showing the model the limits is not a weakening of them -- the engine
        # runs server-side either way. It is a quality improvement: an agent that
        # knows the ceiling is Rs 50,000 escalates a Rs 80,000 payment to manual
        # review with a sensible rationale, instead of proposing a retry and
        # being denied with a rationale that no longer matches the outcome.
        return {
            "limits": {
                "max_recovery_attempts": s.max_recovery_attempts,
                "recovery_cooldown_seconds": s.recovery_cooldown_seconds,
                "high_value_review_threshold_paise": s.high_value_review_threshold_paise,
                "max_recovery_amount_paise": s.max_recovery_amount_paise,
                "daily_recovery_budget_paise": s.daily_recovery_budget_paise,
                "max_cases_per_customer_per_day": s.max_cases_per_customer_per_day,
                "min_propensity_score": s.min_propensity_score,
                "max_payment_age_hours": s.max_payment_age_hours,
                "recovery_link_ttl_minutes": s.recovery_link_ttl_minutes,
            },
            "approval": {
                "require_human_approval": s.require_human_approval,
                "auto_approve_enabled": s.auto_approve_enabled,
                "auto_approve_max_paise": s.auto_approve_max_paise,
                "auto_approve_min_propensity": s.auto_approve_min_propensity,
            },
            "non_recoverable_categories": [
                c.value for c in FailureCategory if not c.is_recoverable
            ],
            "rules": self._policy.rule_catalogue(),
        }

    # -----------------------------------------------------------------
    # Tool 6 -- guardrail dry run (READ_ONLY)
    # -----------------------------------------------------------------

    def _tool_check_recovery_eligibility(self, proposed_strategy: str) -> dict[str, Any]:
        """Preview what the guardrail engine would say about a candidate strategy."""
        strategy = _require_enum(RecoveryStrategy, proposed_strategy, "proposed_strategy")
        ctx = self._build_guardrail_context(strategy)
        verdict = self._policy.evaluate(ctx)

        return {
            # PREVIEW ONLY. This is a read-only dry run against the state of the
            # world right now. The binding evaluation happens server-side in
            # RecoveryService.approve(), at approval time, against state that will
            # have moved: budgets are consumed, attempts accumulate, cases expire.
            # The agent seeing "allow" here changes nothing by itself -- it grants
            # no permission and reserves no budget. It exists so the model can
            # choose a strategy that will actually survive the real check, rather
            # than proposing something that gets denied thirty seconds later.
            "preview": True,
            "binding": False,
            "note": (
                "Preview only. Guardrails are re-evaluated at approval time and "
                "may reach a different decision."
            ),
            "decision": verdict.decision.value,
            "summary": verdict.summary,
            "blocking_rules": list(verdict.blocking_rules),
            "approval_rules": list(verdict.approval_rules),
            "evaluations": [
                {
                    "rule_id": e.rule_id,
                    "name": e.name,
                    "decision": e.decision.value,
                    "passed": e.passed,
                    "reason": e.reason,
                    "observed": e.observed,
                    "limit": e.limit,
                }
                for e in verdict.evaluations
            ],
        }

    # -----------------------------------------------------------------
    # Tool 7 -- submit the plan (WRITE_PROPOSAL) -- terminal
    # -----------------------------------------------------------------

    def _tool_submit_recovery_plan(
        self,
        failure_category: str,
        confidence: float,
        strategy: str,
        rationale: str,
        customer_message: str,
        evidence: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record the agent's final recommendation and end the reasoning loop."""
        try:
            plan = AgentRecoveryPlan(
                failure_category=failure_category,
                confidence=confidence,
                strategy=strategy,
                rationale=rationale,
                customer_message=customer_message,
                evidence=list(evidence or []),
            )
        except ValidationError as exc:
            # Returned rather than raised, and returned in a shape the model can
            # act on. A model that submits strategy="issue_refund" or a 3,000
            # character rationale gets told exactly what was wrong and submits
            # again; the alternative -- letting the exception kill the run -- costs
            # the merchant a whole analysis over a typo in an enum.
            return {
                "accepted": False,
                "validation_error": exc.errors(include_url=False),
                "hint": (
                    "Fix the listed fields and call submit_recovery_plan again. "
                    "Note there is no amount field: the recovery amount is always "
                    "the original payment amount and is set server-side."
                ),
            }

        self.submitted_plan = plan
        return {
            "accepted": True,
            "failure_category": plan.failure_category.value,
            "strategy": plan.strategy.value,
            "confidence": plan.confidence,
            # Said out loud in the result so the model's last observation is the
            # truth about what it just did: it proposed, it did not execute.
            "next_step": (
                "Proposal recorded. It now goes through the guardrail engine and, "
                "if permitted, waits for a human to approve before any money moves."
            ),
        }

    # -----------------------------------------------------------------
    # Guardrail-context construction (shared by the eligibility preview)
    # -----------------------------------------------------------------

    def _existing_case(self) -> RecoveryCase | None:
        """The open case for this payment, if one has already been created."""
        # At most one, guaranteed by the uq_case_per_payment constraint. During a
        # first analysis there is none yet -- the service creates the case from
        # the result of this run.
        return self._db.execute(
            select(RecoveryCase).where(RecoveryCase.original_payment_id == self.payment.id)
        ).scalar_one_or_none()

    def _attempts(self) -> list[RecoveryAttempt]:
        """Every attempt already made on this payment's case, oldest first."""
        case = self._existing_case()
        if case is None:
            return []
        return list(
            self._db.execute(
                select(RecoveryAttempt)
                .where(RecoveryAttempt.case_id == case.id)
                .order_by(RecoveryAttempt.created_at)
            )
            .scalars()
            .all()
        )

    def next_attempt_number(self) -> int:
        """
        The attempt number a new recovery on this payment would take (1-based).

        Public because the orchestrator needs the identical figure when it builds
        the ML feature row outside the tool loop, and two independent "count the
        attempts" implementations would eventually disagree about whether a
        failed attempt still counts.
        """
        return len(self._attempts()) + 1

    def _build_guardrail_context(self, strategy: RecoveryStrategy) -> GuardrailContext:
        """
        Assemble the live inputs the guardrail rules need.

        The counts are queried here rather than borrowed from ``RecoveryService``
        because importing the service would be circular (service -> orchestrator ->
        tools -> service). The service remains the *binding* evaluator; this reads
        the same tables to build a preview of it.
        """
        now = utcnow()
        # Budgets are daily and denominated in UTC days. Uses the shared helper
        # rather than re-deriving the boundary here, because the binding evaluator
        # uses that helper and a preview computed from a different midnight is a
        # preview that lies.
        day_start = utc_day_start(now)

        customer = self._require_customer()
        attempts = self._attempts()

        daily_total = self._db.execute(
            select(func.coalesce(func.sum(RecoveryAttempt.amount_paise), 0)).where(
                RecoveryAttempt.created_at >= day_start
            )
        ).scalar_one()

        # The case under analysis is excluded, exactly as the binding evaluator
        # excludes it. `analyze_payment` creates and flushes the case *before* the
        # agent runs, so counting it here made the preview off by one: R8 fires on
        # `>=`, so a limit of 3 was previewed as a limit of 2. The model was then
        # told "this customer already has 3 cases today" on the third case, which
        # policy explicitly permits -- and a model that believes its own preview
        # downgrades a permitted recovery to an escalation.
        existing_case = self._existing_case()
        cases_today_stmt = (
            select(func.count())
            .select_from(RecoveryCase)
            .where(
                RecoveryCase.customer_id == customer.id,
                RecoveryCase.created_at >= day_start,
            )
        )
        if existing_case is not None:
            cases_today_stmt = cases_today_stmt.where(RecoveryCase.id != existing_case.id)
        cases_today = self._db.execute(cases_today_stmt).scalar_one()

        # The category comes from the deterministic taxonomy, not from anything
        # the model supplied. Otherwise the eligibility preview would accept a
        # category argument and the model could shop for a friendlier one until
        # R3_RECOVERABLE_CATEGORY stopped complaining.
        match = classify_error(
            error_code=self.payment.error_code,
            error_reason=self.payment.error_reason,
            error_description=self.payment.error_description,
            error_source=self.payment.error_source,
            error_step=self.payment.error_step,
            method=self.payment.method,
        )

        features = build_feature_row(
            failure_category=match.category.value,
            payment_method=_coerce_stored_enum(
                PaymentMethod, self.payment.method, PaymentMethod.UNKNOWN
            ).value,
            proposed_strategy=strategy.value,
            amount_paise=self.payment.amount_paise,
            attempt_number=len(attempts) + 1,
            customer_prior_success_rate=customer.prior_success_rate,
            customer_total_payments=customer.total_payments,
            hours_since_failure=self.payment_age_hours,
        )
        propensity = get_predictor().predict(features)

        return GuardrailContext(
            payment=self.payment,
            customer=customer,
            strategy=strategy,
            failure_category=match.category,
            propensity_score=propensity.score,
            # Read from the payment. R9_AMOUNT_INTEGRITY compares this against
            # payment.amount_paise, so sourcing it anywhere else would turn a
            # guardrail into a tautology.
            amount_paise=self.payment.amount_paise,
            attempt_number=len(attempts) + 1,
            now=now,
            last_attempt_at=_as_utc(attempts[-1].created_at) if attempts else None,
            open_attempt_exists=any(a.status in OPEN_ATTEMPT_STATUSES for a in attempts),
            daily_recovery_total_paise=int(daily_total or 0),
            customer_cases_today=int(cases_today or 0),
            settings=self._settings,
        )

    # -----------------------------------------------------------------
    # Declarations
    # -----------------------------------------------------------------

    def _build_specs(self) -> tuple[ToolSpec, ...]:
        """
        Declare the seven tools.

        Every ``description`` here is written for the model, because it is the
        model's only documentation: there is no README in its context window. The
        JSON Schemas are equally load-bearing -- a property without a description
        is a property the model will fill in with a plausible guess.
        """
        category_values = [c.value for c in FailureCategory]
        strategy_values = [s.value for s in RecoveryStrategy]
        method_values = [m.value for m in PaymentMethod]

        return (
            ToolSpec(
                name="get_payment_details",
                description=(
                    "Read the full record of the failed payment under analysis: amount, "
                    "method, status, every raw error field returned by the gateway, and "
                    "how many hours ago it failed. Start here."
                ),
                capability=ToolCapability.READ_ONLY,
                parameters={
                    "type": "object",
                    "properties": {
                        "payment_id": {
                            "type": "string",
                            "description": (
                                "Optional. The id of the payment under analysis. This "
                                "analysis is scoped to a single payment; any other id "
                                "is refused."
                            ),
                        }
                    },
                    "required": [],
                },
                fn=self._tool_get_payment_details,
            ),
            ToolSpec(
                name="get_customer_history",
                description=(
                    "Read the paying customer's track record: how many payments they "
                    "have made, how many succeeded, their lifetime value, whether the "
                    "merchant's risk process has flagged them, and their most recent "
                    "payment outcomes. Use it to judge whether this failure is an "
                    "anomaly or a pattern."
                ),
                capability=ToolCapability.READ_ONLY,
                parameters={
                    "type": "object",
                    "properties": {
                        "customer_id": {
                            "type": "string",
                            "description": (
                                "Optional. The customer who made the payment under "
                                "analysis. Any other id is refused."
                            ),
                        }
                    },
                    "required": [],
                },
                fn=self._tool_get_customer_history,
            ),
            ToolSpec(
                name="classify_failure_code",
                description=(
                    "Map raw gateway failure fields onto one of the recovery "
                    "categories, with a confidence, the evidence behind the match, and "
                    "the house playbook for that category (recommended strategy and "
                    "typical success rate). Pass the error fields exactly as "
                    "get_payment_details returned them."
                ),
                capability=ToolCapability.READ_ONLY,
                parameters={
                    "type": "object",
                    "properties": {
                        "error_code": {
                            "type": "string",
                            "description": "Gateway error code, e.g. 'BAD_REQUEST_ERROR'.",
                        },
                        "error_reason": {
                            "type": "string",
                            "description": (
                                "Gateway machine-readable reason, e.g. "
                                "'payment_failed_insufficient_funds'."
                            ),
                        },
                        "error_description": {
                            "type": "string",
                            "description": "Human-readable failure text from the gateway.",
                        },
                        "error_source": {
                            "type": "string",
                            "description": (
                                "Where the failure originated: 'bank', 'gateway', "
                                "'customer' or 'internal'."
                            ),
                        },
                        "error_step": {
                            "type": "string",
                            "description": (
                                "Stage the payment reached, e.g. 'payment_authorization'."
                            ),
                        },
                        "method": {
                            "type": "string",
                            "description": "Instrument used for the payment.",
                            "enum": method_values,
                        },
                    },
                    "required": [],
                },
                fn=self._tool_classify_failure_code,
            ),
            ToolSpec(
                name="score_recovery_propensity",
                description=(
                    "Predict the probability that a recovery attempt would actually "
                    "succeed, using the trained propensity model. Call it for each "
                    "strategy you are considering and prefer the one that scores "
                    "highest. The amount is taken from the payment under analysis and "
                    "cannot be supplied."
                ),
                capability=ToolCapability.READ_ONLY,
                parameters={
                    "type": "object",
                    "properties": {
                        "failure_category": {
                            "type": "string",
                            "description": "Category from classify_failure_code.",
                            "enum": category_values,
                        },
                        "payment_method": {
                            "type": "string",
                            "description": (
                                "Optional. Instrument of the original payment. Defaults "
                                "to the method on the payment record."
                            ),
                            "enum": method_values,
                        },
                        "proposed_strategy": {
                            "type": "string",
                            "description": "The recovery strategy you want scored.",
                            "enum": strategy_values,
                        },
                        "attempt_number": {
                            "type": "integer",
                            "description": (
                                "Optional. Which attempt this would be, 1-based. "
                                "Defaults to the true next attempt number."
                            ),
                            "minimum": 1,
                        },
                    },
                    "required": ["failure_category", "proposed_strategy"],
                },
                fn=self._tool_score_recovery_propensity,
            ),
            ToolSpec(
                name="get_recovery_policy",
                description=(
                    "Read the merchant's guardrail policy: attempt limits, cooldowns, "
                    "amount ceilings, daily budget, the minimum acceptable propensity "
                    "score and the approval rules. Your proposal is evaluated against "
                    "these, so reason inside them rather than against them."
                ),
                capability=ToolCapability.READ_ONLY,
                parameters={"type": "object", "properties": {}, "required": []},
                fn=self._tool_get_recovery_policy,
            ),
            ToolSpec(
                name="check_recovery_eligibility",
                description=(
                    "Dry-run the guardrail engine against a candidate strategy and see "
                    "which rules would pass, which would deny it and which would demand "
                    "human approval. This is a preview against current state only; the "
                    "binding evaluation happens server-side when a human approves, and "
                    "may differ. Use it to avoid proposing something that will be "
                    "denied."
                ),
                capability=ToolCapability.READ_ONLY,
                parameters={
                    "type": "object",
                    "properties": {
                        "proposed_strategy": {
                            "type": "string",
                            "description": "The strategy you want checked.",
                            "enum": strategy_values,
                        }
                    },
                    "required": ["proposed_strategy"],
                },
                fn=self._tool_check_recovery_eligibility,
            ),
            ToolSpec(
                name=TERMINAL_TOOL,
                description=(
                    "Submit your final recommendation and end the analysis. Call this "
                    "exactly once, after you have classified the failure and scored at "
                    "least one strategy. This records a PROPOSAL only: no money moves "
                    "until a human approves it. There is no amount field -- the recovery "
                    "amount is always the original payment amount."
                ),
                capability=ToolCapability.WRITE_PROPOSAL,
                parameters={
                    "type": "object",
                    "properties": {
                        "failure_category": {
                            "type": "string",
                            "description": "Your final classification of why the payment failed.",
                            "enum": category_values,
                        },
                        "confidence": {
                            "type": "number",
                            "description": (
                                "How confident you are in that classification, 0.0 to 1.0."
                            ),
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "strategy": {
                            "type": "string",
                            "description": (
                                "The recovery action you recommend. Use 'manual_review' "
                                "when a human should look at it and 'no_recovery' when "
                                "the right answer is to do nothing."
                            ),
                            "enum": strategy_values,
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "Why, in plain language, written for the merchant "
                                "operator who will approve or reject this. Cite the "
                                "evidence you used. 10 to 1200 characters."
                            ),
                        },
                        "customer_message": {
                            "type": "string",
                            "description": (
                                "What the customer would be told. Polite, specific, no "
                                "blame, no jargon, no gateway error codes. 10 to 500 "
                                "characters."
                            ),
                        },
                        "evidence": {
                            "type": "array",
                            "description": (
                                "Up to 8 short signals that support your conclusion, "
                                "e.g. 'error_reason=payment_failed_insufficient_funds'."
                            ),
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "failure_category",
                        "confidence",
                        "strategy",
                        "rationale",
                        "customer_message",
                    ],
                },
                fn=self._tool_submit_recovery_plan,
            ),
        )
