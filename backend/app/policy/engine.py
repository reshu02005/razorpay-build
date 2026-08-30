"""
The guardrail engine: turns thirteen independent rule verdicts into one decision.

**Why an engine at all, rather than calling the rules inline?** Three things have
to be true of every guardrail evaluation in the system, and they are true here
because they are implemented exactly once:

1.  *Every* rule runs, every time. The engine never stops at the first denial,
    so the operator sees the complete checklist rather than being sent round the
    loop to discover the second problem after fixing the first.
2.  The most restrictive verdict wins. Aggregation is ``max`` over
    ``GuardrailDecision.severity``, which makes the system monotone: adding a
    rule can only ever make it more conservative, never less. That property is
    what makes the policy safe to extend by someone who has not read all of it.
3.  A strategy that moves no money short-circuits to ALLOW in one place. The
    rejected alternative -- an ``if not ctx.strategy.moves_money`` guard at the
    top of all thirteen rules -- is thirteen chances to forget one, and the one
    you forget denies a case that should simply have been marked "no action".

**Why the verdict carries a written summary.** The consumer of this decision is a
human under time pressure, not a parser. ``blocking_rules`` tells the frontend
what to highlight; ``summary`` tells the merchant what happened in a sentence
they can act on.

Nothing in this module touches the database, the clock or the network either --
the engine is as unit-testable as the rules it drives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.domain.enums import FailureCategory, GuardrailDecision
from app.policy.rules import (
    NOT_APPLICABLE_REASON,
    RULE_COUNT,
    RULES,
    GuardrailContext,
    GuardrailEvaluation,
    Rule,
)


@dataclass(frozen=True)
class GuardrailVerdict:
    """
    The aggregate outcome of one full policy evaluation.

    Attributes:
        decision: The most restrictive decision any rule returned.
        evaluations: Every rule's result, in registry order. Always ``RULE_COUNT``
            entries -- the passes are as much a part of the record as the failures.
        blocking_rules: ``rule_id`` of each rule that returned ``DENY``.
        approval_rules: ``rule_id`` of each rule that returned ``REQUIRE_APPROVAL``.
        summary: One sentence, written for a merchant rather than a developer.
    """

    decision: GuardrailDecision
    evaluations: list[GuardrailEvaluation]
    blocking_rules: list[str]
    approval_rules: list[str]
    summary: str


def _as_clause(reason: str) -> str:
    """
    Turn a rule's stand-alone sentence into a clause that can follow a colon.

    ``"Maximum recovery attempts (2) already used."`` becomes
    ``"maximum recovery attempts (2) already used"``, so the summary reads as one
    sentence instead of two half-sentences glued together.

    The first character is lowered only when the opening word is clearly an
    ordinary capitalised English word: longer than two characters, alphabetic,
    and lowercase after its first letter. That guard is not decoration -- rule
    reasons legitimately begin with ``"Rs 25,000.00 ..."`` and could begin with
    an acronym like ``"UPI ..."``, and "rs 25,000.00" in a financial summary
    would look like a bug to the person reading it.
    """
    head = reason.split(" ", 1)[0]
    if len(head) > 2 and head.isalpha() and head[1:].islower():
        reason = reason[:1].lower() + reason[1:]
    return reason.rstrip(".")


class PolicyEngine:
    """
    Evaluates the guardrail policy for one proposed recovery.

    Stateless between calls: it holds only the ``Settings`` it was constructed
    with, and each ``evaluate`` call is a pure function of its context. It is
    therefore safe to build one per request or to keep one around; neither can
    leak state from one case into another.
    """

    def __init__(self, settings: Settings) -> None:
        """
        Args:
            settings: The policy limits this engine reports in ``policy_snapshot``.

        Note that the *rules* read their limits from ``ctx.settings``, not from
        here. That is deliberate: a rule must be exercisable in a unit test with
        a purpose-built ``Settings`` without anyone having to construct an
        engine. In the running application both are the same cached singleton
        from ``app.config.get_settings``.
        """
        self._settings = settings

    # -- Evaluation --------------------------------------------------------

    def evaluate(self, ctx: GuardrailContext) -> GuardrailVerdict:
        """
        Run the whole policy against one proposed recovery.

        Args:
            ctx: Every fact the rules may see, precomputed by the service layer.

        Returns:
            A ``GuardrailVerdict`` whose ``decision`` is the most restrictive
            outcome across all ``RULE_COUNT`` rules, and whose ``evaluations``
            list always contains one entry per rule, pass or fail.

        Raises:
            Nothing. A guardrail that can raise is a guardrail that can be
            skipped by an exception handler somewhere upstream, so every rule is
            written to return a verdict rather than to signal by exception.
        """
        if not ctx.strategy.moves_money:
            # A strategy such as MANUAL_REVIEW or NO_RECOVERY creates no payment
            # attempt, so there is nothing for a money guardrail to constrain.
            # The checklist is still rendered in full, marked not-applicable, so
            # the operator can see the policy was consulted and simply had
            # nothing to say. The service layer then routes the case to
            # NO_ACTION or ESCALATED -- that routing is a lifecycle decision and
            # deliberately does not live in the policy engine.
            evaluations = [self._not_applicable(rule) for rule in RULES]
            return GuardrailVerdict(
                decision=GuardrailDecision.ALLOW,
                evaluations=evaluations,
                blocking_rules=[],
                approval_rules=[],
                summary=(
                    "No guardrail applies: this strategy creates no payment attempt."
                ),
            )

        evaluations = [rule.fn(ctx) for rule in RULES]

        blocking = [e.rule_id for e in evaluations if e.decision is GuardrailDecision.DENY]
        approvals = [
            e.rule_id for e in evaluations if e.decision is GuardrailDecision.REQUIRE_APPROVAL
        ]

        # Most-restrictive-wins, expressed as a max over the enum's own severity
        # ordering rather than as a chain of ifs. The ordering lives on
        # GuardrailDecision, so a future fourth decision level slots in by
        # editing the enum, not by editing this aggregation.
        decision = max(
            (e.decision for e in evaluations),
            key=lambda d: d.severity,
            default=GuardrailDecision.ALLOW,
        )

        return GuardrailVerdict(
            decision=decision,
            evaluations=evaluations,
            blocking_rules=blocking,
            approval_rules=approvals,
            summary=self._summarise(decision, evaluations, blocking, approvals),
        )

    # -- Reporting ---------------------------------------------------------

    def policy_snapshot(self) -> dict[str, Any]:
        """
        Capture every limit currently in force, as a plain JSON-serialisable dict.

        Returns:
            A flat mapping of limit name to value, plus the identity of the rule
            set that was applied.

        This snapshot is frozen onto the recovery case at decision time, and that
        is the entire point. Guardrail limits are configuration: a merchant will
        raise the daily budget next month, or lower the propensity floor. Without
        a snapshot, every historical decision would silently be re-interpreted
        against today's numbers, and "why was this Rs 40,000 recovery allowed?"
        would be unanswerable -- or worse, answerable but wrong. Storing the
        limits alongside the verdict means a decision can always be re-read
        against the policy that actually produced it.

        ``rule_ids`` is included for the same reason as the numbers: if a
        fourteenth rule is added later, the snapshot still records that only
        thirteen were applied to this case.
        """
        cfg = self._settings
        return {
            # Attempt and timing limits
            "max_recovery_attempts": cfg.max_recovery_attempts,
            "recovery_cooldown_seconds": cfg.recovery_cooldown_seconds,
            "max_payment_age_hours": cfg.max_payment_age_hours,
            "recovery_link_ttl_minutes": cfg.recovery_link_ttl_minutes,
            # Money limits (integer paise, as everywhere upstream of the API edge)
            "high_value_review_threshold_paise": cfg.high_value_review_threshold_paise,
            "max_recovery_amount_paise": cfg.max_recovery_amount_paise,
            "daily_recovery_budget_paise": cfg.daily_recovery_budget_paise,
            "currency": cfg.currency,
            # Velocity and quality limits
            "max_cases_per_customer_per_day": cfg.max_cases_per_customer_per_day,
            "min_propensity_score": cfg.min_propensity_score,
            # Autonomy switches
            "require_human_approval": cfg.require_human_approval,
            "auto_approve_enabled": cfg.auto_approve_enabled,
            "auto_approve_max_paise": cfg.auto_approve_max_paise,
            "auto_approve_min_propensity": cfg.auto_approve_min_propensity,
            # Taxonomy the policy depends on. Serialised as plain strings because
            # this dict is written straight into a JSON column.
            "non_recoverable_categories": [
                category.value for category in FailureCategory if not category.is_recoverable
            ],
            # Identity of the rule set applied
            "rule_count": RULE_COUNT,
            "rule_ids": [rule.rule_id for rule in RULES],
        }

    def rule_catalogue(self) -> list[dict[str, str]]:
        """
        Describe every rule, for ``GET /api/policy``.

        Returns:
            One ``{"rule_id", "name", "description"}`` dict per rule, in
            evaluation order.

        Published so the ``/policy`` page can show a merchant the complete set of
        controls without a single rule being described a second time in
        TypeScript. A policy the operator cannot read is a policy they cannot
        hold the system to.
        """
        return [
            {
                "rule_id": rule.rule_id,
                "name": rule.name,
                "description": rule.description,
            }
            for rule in RULES
        ]

    # -- Internals ---------------------------------------------------------

    @staticmethod
    def _not_applicable(rule: Rule) -> GuardrailEvaluation:
        """
        Build the vacuous evaluation used when the strategy moves no money.

        ALLOW rather than DENY, because the rule did not object -- it had nothing
        to object to. ``applicable=False`` is what stops that reading as approval:
        the checklist greys these rows instead of ticking them, so an operator is
        never told that thirteen checks cleared a case none of them looked at.
        """
        return GuardrailEvaluation(
            rule_id=rule.rule_id,
            name=rule.name,
            description=rule.description,
            decision=GuardrailDecision.ALLOW,
            passed=True,
            reason=NOT_APPLICABLE_REASON,
            applicable=False,
        )

    @staticmethod
    def _summarise(
        decision: GuardrailDecision,
        evaluations: list[GuardrailEvaluation],
        blocking: list[str],
        approvals: list[str],
    ) -> str:
        """
        Write the one-sentence verdict a merchant reads.

        Denials quote the first blocking rule's own reason rather than a generic
        "policy violation", because the specific number is what tells the
        operator whether to wait, escalate or drop the case. Additional denials
        are counted rather than concatenated: the full list is one click away in
        the checklist, and a summary that runs to four clauses is not a summary.
        """
        if decision is GuardrailDecision.DENY:
            first = next(e for e in evaluations if e.decision is GuardrailDecision.DENY)
            count = len(blocking)
            noun = "rule" if count == 1 else "rules"
            extra = f" (+{count - 1} more)" if count > 1 else ""
            return f"Denied by {count} {noun}: {_as_clause(first.reason)}{extra}."

        if decision is GuardrailDecision.REQUIRE_APPROVAL:
            count = len(approvals)
            checks = "check requires" if count == 1 else "checks require"
            return (
                "Allowed, pending explicit approval by an operator "
                f"({count} {checks} sign-off)."
            )

        return (
            f"Allowed: all {len(evaluations)} checks passed and no operator sign-off "
            "is required."
        )
