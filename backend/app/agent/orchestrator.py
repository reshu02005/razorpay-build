"""
The orchestrator: turns one failed payment into one recovery plan, and always
produces one.

This module is where the project's "zero-credential operation" promise is kept.
Analysis has two engines behind a single method:

*   **LLM path** -- Gemini reasons over the read-only toolset and submits a plan.
*   **Rule-based path** -- a deterministic planner built on the failure taxonomy
    and the playbook.

The second is not a stub. It runs whenever no API key is configured, whenever the
operator explicitly asks for it, and whenever the LLM path fails for any reason
at all. A merchant's recovery pipeline must not stop working because a
third-party API is having an afternoon, so an LLM failure here is a *labelled
degradation*, never a failed request: the case is stamped ``AgentMode.RULE_BASED``
with a human-readable ``degraded_reason``, and the caller writes an
``AGENT_DEGRADED`` audit event. The UI says so out loud. A demo that quietly
substitutes if/else for "AI" and does not mention it is lying to its reviewer.

Both paths leave the same artefacts behind: a validated ``AgentRecoveryPlan``, a
propensity score that matches the strategy actually proposed, and a populated
``agent_tool_calls`` trace. The rule-based path records *synthetic* steps -- real
tool invocations with real arguments and real results -- so the explainability
view is never an empty panel, and so a reader can tell at a glance which engine
produced the decision.

What this module deliberately does **not** do: create the case row, evaluate the
binding guardrails, move money, or commit. It analyses and returns. The
transaction belongs to the service that called it.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.agent import prompts as prompt_copy
from app.agent.llm import GeminiClient, LLMStep, LLMUnavailable
from app.agent.rule_planner import plan_from_rules
from app.agent.taxonomy import PLAYBOOK, TaxonomyMatch, classify_error
from app.agent.tools import TERMINAL_TOOL, ToolRegistry
from app.config import Settings
from app.db.models import AgentToolCall, Customer, Payment
from app.domain.enums import (
    AgentMode,
    FailureCategory,
    PaymentMethod,
    RecoveryStrategy,
    ToolCapability,
)
from app.domain.errors import NotFoundError
from app.domain.schemas import AgentRecoveryPlan, AgentRunOut, PropensityResultOut
from app.ml.features import build_feature_row
from app.ml.predictor import get_predictor

logger = logging.getLogger(__name__)


#: Used only if ``app/agent/prompts.py`` cannot supply one. Short on purpose: it
#: is a safety net, not the product's prompt copy.
_FALLBACK_SYSTEM_PROMPT = (
    "You are RecoverAI, a payment-recovery analyst for an Indian merchant. "
    "Investigate one failed payment using the read-only tools you are given, then "
    f"call {TERMINAL_TOOL} exactly once with your recommendation. You propose; a "
    "human approves. You cannot move money and you cannot change the amount."
)


@dataclass
class AnalysisResult:
    """
    Everything one analysis produced, in the shape the service layer needs.

    ``taxonomy`` is carried alongside ``plan`` rather than folded into it because
    they can legitimately disagree: the taxonomy is the deterministic reading of
    the gateway's error fields, while ``plan.failure_category`` is the agent's
    conclusion. Keeping both means a reviewer can see when the model overrode the
    rules and judge whether it was right to.

    Attributes:
        plan: The validated recommendation. Never ``None`` -- if every path
            failed, an exception would have been raised instead.
        run: Metadata about the run: which engine, how many steps, how long, and
            why it degraded if it did.
        propensity: P(success) for the strategy the plan actually proposes.
        taxonomy: The deterministic classification of the raw failure fields.
    """

    plan: AgentRecoveryPlan
    run: AgentRunOut
    propensity: PropensityResultOut
    taxonomy: TaxonomyMatch


class RecoveryAgent:
    """
    Runs one analysis of one failed payment.

    Constructed per request rather than held as a singleton: it carries a
    database session, and a session outliving the request it was opened for is
    how stale reads and cross-request leakage begin.
    """

    def __init__(self, db: Session, settings: Settings) -> None:
        """
        Args:
            db: Session for the current request. Rows are added and flushed, never
                committed -- see :meth:`analyze`.
            settings: Active configuration, forwarded to the toolset and the LLM
                client so both observe the same limits.
        """
        self._db = db
        self._settings = settings
        self._llm = GeminiClient(settings)

    # -----------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------

    def analyze(self, payment: Payment, *, force_rule_based: bool = False) -> AnalysisResult:
        """
        Produce a recovery plan for a failed payment.

        Args:
            payment: The failed payment to analyse. Assumed to have been loaded
                and validated by the caller.
            force_rule_based: Skip the LLM entirely. Used by tests, and to
                demonstrate the degraded path on demand without deleting an API
                key from the environment.

        Returns:
            An :class:`AnalysisResult`. The rows written to ``agent_tool_calls``
            are flushed but **not committed**: the service that called this owns
            the transaction, so an analysis and the case it produces either both
            land or neither does.

        Raises:
            NotFoundError: if the payment references a customer that does not
                exist. This is genuinely unrecoverable -- every guardrail and
                every feature needs the customer -- so unlike an LLM failure it is
                not degraded around.
        """
        started = time.monotonic()
        run_id = f"run_{uuid.uuid4().hex[:16]}"

        customer = self._db.get(Customer, payment.customer_id)
        if customer is None:
            raise NotFoundError(
                f"Customer {payment.customer_id} for payment {payment.id} was not found",
                detail={"payment_id": payment.id, "customer_id": payment.customer_id},
            )

        registry = ToolRegistry(self._db, self._settings, payment)

        # Computed up front because both paths need them: the rule planner takes
        # them as inputs, and the LLM path needs them to write a briefing prompt
        # worth reading. The propensity needs a candidate action to score, so the
        # playbook's primary strategy for the matched category is the opening
        # hypothesis -- it is re-scored below against whatever the plan settles on.
        match = classify_error(
            error_code=payment.error_code,
            error_reason=payment.error_reason,
            error_description=payment.error_description,
            error_source=payment.error_source,
            error_step=payment.error_step,
            method=payment.method,
        )
        opening_strategy = PLAYBOOK[match.category].primary_strategy
        propensity = self._score(registry, category=match.category, strategy=opening_strategy)

        steps: list[LLMStep] = []
        degraded_reason: str | None = None
        mode = AgentMode.RULE_BASED
        model: str | None = None
        plan: AgentRecoveryPlan | None = None

        if force_rule_based:
            degraded_reason = "Deterministic planner requested explicitly for this analysis."
        elif not self._llm.available:
            degraded_reason = self._llm.unavailable_reason()
        else:
            try:
                plan = self._llm.run_tool_loop(
                    system_prompt=self._system_prompt(),
                    user_prompt=self._user_prompt(
                        payment=payment, customer=customer, match=match, propensity=propensity
                    ),
                    registry=registry,
                    max_steps=self._settings.agent_max_steps,
                    on_step=steps.append,
                )
                mode = AgentMode.LLM
                model = self._llm.model
            except LLMUnavailable as exc:
                # The only LLM failure mode there is. Whatever went wrong -- quota,
                # network, a model that would not commit to an answer -- the
                # merchant still gets a plan. Note that `steps` keeps whatever the
                # model did manage to do before failing: a partial trace is
                # evidence of what was tried, and discarding it would make a
                # degraded run look like it never started.
                logger.info("Falling back to the rule-based planner for %s: %s", payment.id, exc)
                degraded_reason = str(exc)

        if plan is None:
            plan, propensity = self._run_rule_based(
                registry=registry,
                steps=steps,
                payment=payment,
                customer=customer,
                match=match,
                opening_strategy=opening_strategy,
                propensity=propensity,
            )
            mode = AgentMode.RULE_BASED
            model = None
        else:
            # The returned score has to describe the strategy that is actually on
            # the table, because the service feeds it straight into
            # R10_PROPENSITY_FLOOR. The model may well have scored three
            # candidates and then proposed the one it scored second; re-scoring
            # here is what stops the case row from carrying a number that belongs
            # to a different plan. Done silently -- this is the system computing a
            # canonical figure, not the agent reasoning, so it is not a trace step.
            propensity = self._rescore_for_plan(registry, plan, match, opening_strategy, propensity)

        self._persist_trace(run_id=run_id, payment=payment, steps=steps, registry=registry)

        run = AgentRunOut(
            run_id=run_id,
            mode=mode,
            model=model,
            steps=len(steps),
            # Wall clock for the whole analysis, not the sum of the tool
            # latencies. On the LLM path most of the time is the model thinking
            # between calls, so summing tool durations would report a 12-second
            # run as 40 milliseconds and make the cost of the AI path invisible.
            total_latency_ms=int((time.monotonic() - started) * 1000),
            degraded_reason=degraded_reason,
        )
        return AnalysisResult(plan=plan, run=run, propensity=propensity, taxonomy=match)

    # -----------------------------------------------------------------
    # The deterministic path
    # -----------------------------------------------------------------

    def _run_rule_based(
        self,
        *,
        registry: ToolRegistry,
        steps: list[LLMStep],
        payment: Payment,
        customer: Customer,
        match: TaxonomyMatch,
        opening_strategy: RecoveryStrategy,
        propensity: PropensityResultOut,
    ) -> tuple[AgentRecoveryPlan, PropensityResultOut]:
        """
        Plan deterministically, recording the reasoning as real tool calls.

        The synthetic steps are not decoration. They are genuine invocations of
        the same registry the LLM would have used, with the same arguments and
        the same results, appended to whatever the LLM already managed. That has
        three consequences worth having:

        *   The explainability trace is populated on every case, so the UI never
            shows an empty panel and quietly implies the decision came from
            nowhere.
        *   The deterministic planner's output is validated through
            ``submit_recovery_plan`` -- the identical gate the model faces. If the
            playbook ever produced a rationale under ten characters, the rule path
            would fail validation exactly as a model would.
        *   The trace is honest about which engine ran: three tidy steps ending in
            a submission reads very differently from a model's exploration, and
            ``AgentRunOut.mode`` names it explicitly.

        Args:
            registry: The pinned toolset.
            steps: Accumulator, possibly already holding failed LLM steps.
            payment: The payment under analysis.
            customer: Its customer.
            match: Deterministic classification.
            opening_strategy: The strategy ``propensity`` was scored against.
            propensity: The opening score.

        Returns:
            The plan, and the propensity score for the strategy it proposes.
        """
        self._record(
            steps,
            registry,
            "classify_failure_code",
            {
                "error_code": payment.error_code,
                "error_reason": payment.error_reason,
                "error_description": payment.error_description,
                "error_source": payment.error_source,
                "error_step": payment.error_step,
                "method": payment.method,
            },
        )
        self._record(
            steps,
            registry,
            "score_recovery_propensity",
            {
                "failure_category": match.category.value,
                "proposed_strategy": opening_strategy.value,
            },
        )

        plan = plan_from_rules(
            payment=payment, customer=customer, match=match, propensity=propensity
        )

        final = self._rescore_for_plan(registry, plan, match, opening_strategy, propensity)
        if final is not propensity:
            # The planner downgraded the strategy (insufficient funds becomes
            # retry-later, an unclassifiable failure becomes manual review). The
            # re-score is recorded rather than swapped in silently, so the trace
            # shows both the strategy that was considered and the one that shipped.
            self._record(
                steps,
                registry,
                "score_recovery_propensity",
                {
                    "failure_category": plan.failure_category.value,
                    "proposed_strategy": plan.strategy.value,
                },
            )

        self._record(
            steps,
            registry,
            TERMINAL_TOOL,
            {
                "failure_category": plan.failure_category.value,
                "confidence": plan.confidence,
                "strategy": plan.strategy.value,
                "rationale": plan.rationale,
                "customer_message": plan.customer_message,
                "evidence": list(plan.evidence),
            },
        )
        return plan, final

    def _record(
        self,
        steps: list[LLMStep],
        registry: ToolRegistry,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Invoke a tool and append the resulting step to the trace.

        Goes through :meth:`ToolRegistry.call` rather than the underlying function
        so that the synthetic path inherits the same never-raises behaviour: a
        failure inside the deterministic trace becomes a recorded step with
        ``ok=False``, not an exception that costs the merchant a plan they already
        have.
        """
        started = time.monotonic()
        result = registry.call(tool_name, arguments)
        error = result.get("error")
        steps.append(
            LLMStep(
                step=len(steps) + 1,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                ok=error is None,
                error=error if isinstance(error, str) else None,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        )
        return result

    # -----------------------------------------------------------------
    # Scoring
    # -----------------------------------------------------------------

    def _score(
        self, registry: ToolRegistry, *, category: FailureCategory, strategy: RecoveryStrategy
    ) -> PropensityResultOut:
        """
        Predict P(recovery succeeds) for one (category, strategy) pair.

        The amount, the customer's history and the payment's age all come from the
        pinned record via the registry, never from an argument -- the same
        restriction the model's scoring tool operates under, for the same reason.
        ``PropensityPredictor.predict`` never raises; a missing model artefact
        returns a heuristic score flagged ``is_fallback``.
        """
        payment = registry.payment
        customer = registry.customer
        features = build_feature_row(
            failure_category=category.value,
            payment_method=_method_value(payment.method),
            proposed_strategy=strategy.value,
            amount_paise=payment.amount_paise,
            attempt_number=registry.next_attempt_number(),
            # Guarded because `analyze` already proved the customer exists; this
            # keeps the helper usable without re-raising a domain error deep in a
            # feature builder.
            customer_prior_success_rate=customer.prior_success_rate if customer else 0.5,
            customer_total_payments=customer.total_payments if customer else 0,
            hours_since_failure=registry.payment_age_hours,
        )
        return get_predictor().predict(features)

    def _rescore_for_plan(
        self,
        registry: ToolRegistry,
        plan: AgentRecoveryPlan,
        match: TaxonomyMatch,
        scored_strategy: RecoveryStrategy,
        current: PropensityResultOut,
    ) -> PropensityResultOut:
        """
        Return a score that describes the plan as submitted.

        Returns ``current`` unchanged -- by identity, which the caller uses to
        decide whether a re-score is worth recording -- when the plan matches what
        was already scored. Otherwise re-runs the model for the plan's own
        category and strategy.
        """
        if plan.failure_category == match.category and plan.strategy == scored_strategy:
            return current
        return self._score(registry, category=plan.failure_category, strategy=plan.strategy)

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------

    def _persist_trace(
        self,
        *,
        run_id: str,
        payment: Payment,
        steps: list[LLMStep],
        registry: ToolRegistry,
    ) -> None:
        """
        Write one ``agent_tool_calls`` row per step.

        ``case_id`` is left null: the case does not exist yet -- it is created by
        the service from the result of this run. The service backfills it by
        ``run_id``, which is precisely why ``run_id`` is exposed on
        ``AgentRunOut`` rather than kept private here. ``payment_id`` is set
        unconditionally, so even a run whose case was never created stays
        attributable.

        Rows are added and flushed, never committed: the analysis and the case it
        produced must land in the same transaction or not at all.
        """
        rows = []
        for step in steps:
            rows.append(
                AgentToolCall(
                    run_id=run_id,
                    case_id=None,
                    payment_id=payment.id,
                    step=step.step,
                    tool_name=step.tool_name,
                    capability=_capability_of(registry, step.tool_name),
                    arguments=step.arguments,
                    result=step.result,
                    ok=step.ok,
                    error=step.error,
                    latency_ms=step.latency_ms,
                )
            )
        if rows:
            self._db.add_all(rows)
            # Flush, not commit. Gives the rows their identity so the caller can
            # relate them, while leaving the decision to commit where it belongs.
            self._db.flush()

    # -----------------------------------------------------------------
    # Prompt copy
    # -----------------------------------------------------------------
    # The wording lives in app/agent/prompts.py so it can be tuned without
    # touching orchestration logic. It is bound by name at call time and every
    # lookup has a fallback: a rename over there degrades this run to a minimal
    # built-in brief instead of breaking every analysis in the product.

    def _system_prompt(self) -> str:
        """The agent's standing instructions."""
        text = getattr(prompt_copy, "SYSTEM_PROMPT", None)
        if isinstance(text, str) and text.strip():
            return text
        builder = getattr(prompt_copy, "build_system_prompt", None)
        if callable(builder):
            try:
                return str(builder())
            except Exception:  # noqa: BLE001
                logger.warning("build_system_prompt failed; using the built-in prompt", exc_info=True)
        return _FALLBACK_SYSTEM_PROMPT

    def _user_prompt(
        self,
        *,
        payment: Payment,
        customer: Customer,
        match: TaxonomyMatch,
        propensity: PropensityResultOut,
    ) -> str:
        """The case-specific brief for this one payment."""
        builder: Callable[..., Any] | None = getattr(prompt_copy, "build_user_prompt", None)
        if callable(builder):
            try:
                return str(
                    builder(
                        payment=payment, customer=customer, match=match, propensity=propensity
                    )
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "build_user_prompt did not accept the analysis context; using the built-in brief",
                    exc_info=True,
                )
        return (
            f"Analyse failed payment {payment.id}.\n"
            f"Customer: {customer.name} ({customer.id}).\n"
            f"Amount: {payment.amount_paise} paise ({payment.currency}), "
            f"method {payment.method}.\n"
            f"Gateway error: code={payment.error_code!r}, reason={payment.error_reason!r}, "
            f"description={payment.error_description!r}.\n"
            f"Preliminary classification: {match.category.value} "
            f"(confidence {match.confidence:.2f}, matched on {match.matched_on}).\n"
            f"Preliminary propensity for the playbook strategy: {propensity.score:.2f}.\n"
            "Verify this with the tools, consider the alternatives, then call "
            f"{TERMINAL_TOOL} once."
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _method_value(raw: str | None) -> str:
    """
    Normalise a stored payment method into a known feature value.

    The ML pipeline was trained on the ``PaymentMethod`` vocabulary. A row holding
    anything else -- a hand-edited fixture, a method a future gateway adds --
    becomes ``"unknown"``, which the model has seen, rather than an unseen
    category that would quietly shift the one-hot encoding.
    """
    try:
        return PaymentMethod(raw or "").value
    except ValueError:
        return PaymentMethod.UNKNOWN.value


def _capability_of(registry: ToolRegistry, tool_name: str) -> str:
    """
    Resolve a tool's security classification for the audit row.

    Recorded per step so the "no financial tool was ever in the loop" claim is
    checkable from the stored data, not only from the source. An unresolvable
    name -- a tool the model hallucinated -- is recorded as ``read_only``, which
    is accurate rather than convenient: the call was rejected before it executed,
    so nothing was read and nothing was written.
    """
    try:
        return registry.get(tool_name).capability.value
    except KeyError:
        return ToolCapability.READ_ONLY.value
