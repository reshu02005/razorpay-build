"""
The deterministic recovery planner: a full recommendation with no language model.

This module is a **first-class path, not a stub.** It is what runs when there is
no ``GEMINI_API_KEY``, when the Gemini call times out, when the model returns
something that fails validation, and whenever a caller passes
``force_rule_based=True``. On a laptop with no internet and no credentials, this
is the *only* thing standing between a reviewer and an empty screen -- so it has
to produce a recommendation a payments person would actually agree with,
complete with real evidence and a real rationale.

Two consequences of taking that seriously:

*   Everything the LLM path can produce, this path produces too: a category, a
    calibrated confidence, a strategy, an operator-facing rationale that cites
    the specific gateway fields it reasoned from, a customer-facing message, and
    an evidence list. Same ``AgentRecoveryPlan``, same downstream handling. The
    only visible difference is ``AgentMode.RULE_BASED`` on the run, which the UI
    states out loud rather than hiding.
*   The planner is a *planner*, not a second guardrail engine. It recommends;
    ``app.policy.engine`` decides. Where the two could overlap -- customer risk
    flags, attempt counts, amount ceilings -- this module deliberately stays
    silent and lets the guardrail fire, so that a refusal is recorded once, by
    the component that owns it, with the rule id attached.

The design also makes the LLM path honestly evaluable: because the deterministic
answer is always available for the same input, "did the model add anything?" is a
question you can answer by comparing two plans, rather than a matter of faith.
"""

from __future__ import annotations

from app.agent.taxonomy import PLAYBOOK, Playbook, TaxonomyMatch
from app.db.models import Customer, Payment
from app.domain.enums import FailureCategory, PaymentMethod, RecoveryStrategy
from app.domain.schemas import AgentRecoveryPlan, PropensityResultOut

# ---------------------------------------------------------------------------
# Bounds imposed by AgentRecoveryPlan
# ---------------------------------------------------------------------------
# Mirrored here as named constants so the text-assembly arithmetic below is
# readable. The schema remains the authority: if these drifted, validation would
# reject the plan immediately and loudly, which is the correct failure mode.
_MAX_RATIONALE_CHARS = 1200
_MAX_CUSTOMER_MESSAGE_CHARS = 500
_MAX_EVIDENCE_ITEMS = 8

#: Below this predicted success probability the planner recommends a human look
#: at the case instead of proposing an attempt.
#:
#: This is deliberately set *below* ``settings.min_propensity_score`` (0.15 by
#: default), which is the guardrail R10 floor, and the gap is the whole point:
#:
#:   * score in [0.10, 0.15) -- the planner still proposes a real strategy, R10
#:     denies it, and the case lands in BLOCKED with the rule id recorded. The
#:     policy limit did its job and the audit trail shows it doing so.
#:   * score below 0.10 -- the odds are so poor that proposing an attempt only
#:     generates a denial for someone to read. The planner escalates instead, and
#:     the case lands in ESCALATED, where a human can decide whether to chase the
#:     customer by other means.
#:
#: Setting this equal to (or above) the guardrail floor was the obvious
#: alternative and was rejected: it would make R10 unreachable from this path,
#: quietly relocating a policy limit into planner code where nobody would look
#: for it. This threshold is a heuristic about wasting a reviewer's time, not a
#: policy limit, which is why it is a module constant rather than a setting.
LOW_PROPENSITY_ESCALATION_FLOOR = 0.10

#: Which payment method each "switch" strategy targets. Used only to detect a
#: recommendation that would switch to the rails that just failed.
_SWITCH_TARGET: dict[RecoveryStrategy, str] = {
    RecoveryStrategy.SWITCH_TO_UPI: PaymentMethod.UPI.value,
    RecoveryStrategy.SWITCH_TO_CARD: PaymentMethod.CARD.value,
    RecoveryStrategy.SWITCH_TO_NETBANKING: PaymentMethod.NETBANKING.value,
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _clamp(text: str, limit: int) -> str:
    """
    Trim ``text`` to ``limit`` characters, marking the cut.

    Args:
        text: Text to trim.
        limit: Maximum length of the result, in characters.

    Returns:
        The stripped text if it already fits, otherwise a truncated copy ending
        in ``"..."`` so a reader can see the text was cut rather than the
        sentence merely ending abruptly.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _is_noop_switch(strategy: RecoveryStrategy, method: str) -> bool:
    """
    Would this strategy "switch" the customer to the method they already used?

    The playbook is written per failure category, and a category says nothing
    about which rails the payment was on. "Bank declined -- move to UPI" is
    excellent advice for a card, and meaningless for a payment that was already
    UPI: it re-presents to the same issuer that just refused, while telling the
    customer we have changed something.

    Args:
        strategy: Candidate recovery strategy.
        method: The failed payment's method, already lowercased.

    Returns:
        True when acting on ``strategy`` would put the payment back on the rails
        that just failed.
    """
    return _SWITCH_TARGET.get(strategy) == method


def _select_strategy(playbook: Playbook, method: str) -> tuple[RecoveryStrategy, str | None]:
    """
    Choose the playbook strategy that actually applies to this payment.

    Tries the primary, falls back to the alternate, and finally to
    ``RETRY_SAME_METHOD``. Only "switch to the failed rails" disqualifies a
    candidate -- nothing else here second-guesses the playbook.

    Args:
        playbook: The entry for the classified failure category.
        method: The failed payment's method, already lowercased.

    Returns:
        ``(strategy, note)`` where ``note`` is a sentence for the rationale when
        the primary strategy had to be replaced, or ``None`` when the playbook's
        first choice was used as written. Substitutions are surfaced rather than
        made silently: an operator comparing the case against the published
        playbook should be able to see why they differ.
    """
    # RETRY_SAME_METHOD is never a "switch", so the last candidate is always
    # acceptable and the default below is unreachable by construction. It is
    # stated anyway so a reader does not have to prove that to themselves.
    candidates = (
        playbook.primary_strategy,
        playbook.alternate_strategy,
        RecoveryStrategy.RETRY_SAME_METHOD,
    )
    chosen = next(
        (c for c in candidates if not _is_noop_switch(c, method)),
        RecoveryStrategy.RETRY_SAME_METHOD,
    )
    if chosen is playbook.primary_strategy:
        return chosen, None
    note = (
        f"The playbook's first choice for this category is "
        f"{playbook.primary_strategy.value}, but this payment was already on "
        f"{method or 'that method'}, so switching to it would re-present to the same "
        f"rails that just failed. Using {chosen.value} instead."
    )
    return chosen, note


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


def plan_from_rules(
    *,
    payment: Payment,
    customer: Customer,
    match: TaxonomyMatch,
    propensity: PropensityResultOut,
) -> AgentRecoveryPlan:
    """
    Produce a complete recovery recommendation without calling a language model.

    The logic, in order:

    1.  Look up the merchant's playbook entry for the classified category.
    2.  Pick the playbook strategy that applies to *this* payment's rails
        (see :func:`_select_strategy`).
    3.  Apply the three category overrides that the playbook alone cannot
        express, plus the low-propensity escalation.
    4.  Compose a rationale that cites the real evidence from ``match`` and the
        real score from ``propensity``, and take the customer-facing wording from
        the playbook.

    Args:
        payment: The failed payment. Read for its method and its identifiers
            only -- the amount is deliberately never touched here, because the
            service layer copies it from this same row and the returned plan has
            no field that could express one.
        customer: The payer. Used for context in the rationale (history, risk
            flag), never to make or block a decision: attempt limits, velocity
            and risk flags are guardrail territory.
        match: Output of ``taxonomy.classify_error`` for this payment.
        propensity: Output of the ML propensity predictor, which never raises and
            may be a documented heuristic fallback.

    Returns:
        A validated :class:`AgentRecoveryPlan`. Every bound the schema imposes
        (rationale 10-1200 chars, customer message 10-500, at most 8 evidence
        items, confidence in [0, 1]) is satisfied here rather than left to
        chance, so the caller never has to handle a validation error from the
        deterministic path.
    """
    category = match.category
    playbook = PLAYBOOK[category]  # Total by construction; see taxonomy's import-time check.
    method = (payment.method or "").strip().lower()

    strategy, substitution_note = _select_strategy(playbook, method)
    override_note: str | None = None

    # -- Category overrides -------------------------------------------------
    # These three are stated explicitly rather than folded into the playbook
    # because they are safety properties of the *system*, not preferences of the
    # merchant. Writing them here means a future edit to a playbook entry cannot
    # accidentally make an unexplained or fraud-blocked failure recoverable.
    if category is FailureCategory.UNKNOWN:
        strategy = RecoveryStrategy.MANUAL_REVIEW
        override_note = (
            "The gateway detail did not match anything in the taxonomy, so the failure is "
            "unexplained; this is routed to a human rather than guessed at."
        )
    elif category is FailureCategory.RISK_BLOCKED:
        strategy = RecoveryStrategy.NO_RECOVERY
        override_note = (
            "A risk control blocked this transaction. No automated recovery is offered for "
            "this category under any circumstances."
        )
    elif category is FailureCategory.INSUFFICIENT_FUNDS:
        strategy = RecoveryStrategy.RETRY_LATER
        override_note = (
            "The instrument itself worked, so the only variable worth changing is time; an "
            "immediate retry would fail for exactly the same reason."
        )
    elif strategy.moves_money and propensity.score < LOW_PROPENSITY_ESCALATION_FLOOR:
        # Guarded on ``moves_money`` so this can never downgrade one of the three
        # overrides above -- each of them already resolves to a strategy that
        # creates no payment attempt.
        strategy = RecoveryStrategy.MANUAL_REVIEW
        override_note = (
            f"Predicted success probability is {propensity.score:.0%}, below the "
            f"{LOW_PROPENSITY_ESCALATION_FLOOR:.0%} floor at which proposing an automated "
            "attempt stops being worth the customer friction. Escalated for a human view."
        )

    # -- Rationale ----------------------------------------------------------
    # Assembled head-first so that if anything has to be cut, it is the general
    # playbook prose and never the specific evidence or the recommendation. The
    # operator reading this in the approval screen needs to know what we saw and
    # what we propose; the textbook explanation is the expendable part.
    cited = "; ".join(match.evidence[:3]) if match.evidence else "no gateway detail was supplied"
    fallback_note = ", heuristic fallback -- no trained model artefact" if propensity.is_fallback else ""

    head = (
        f"Deterministic planner (no language model was used for this analysis). "
        f"Classified as {category.value} at {match.confidence:.0%} classification confidence, "
        f"matched on {match.matched_on}: {cited}."
    )

    tail_parts: list[str] = [
        f"Predicted recovery propensity is {propensity.score:.0%} "
        f"(model {propensity.model_version}{fallback_note}).",
        f"Customer history: {customer.successful_payments} of {customer.total_payments} "
        f"prior payments succeeded ({customer.prior_success_rate:.0%})"
        + (", and this customer carries a risk flag." if customer.risk_flagged else "."),
    ]
    if substitution_note:
        tail_parts.append(substitution_note)
    if override_note:
        tail_parts.append(override_note)
    tail_parts.append(f"Recommended action: {strategy.value}.")
    tail = " ".join(tail_parts)

    # One space joins head/body and one joins body/tail, hence the -2.
    body_budget = _MAX_RATIONALE_CHARS - len(head) - len(tail) - 2
    body = _clamp(playbook.reasoning, body_budget) if body_budget > 40 else ""
    rationale = _clamp(" ".join(part for part in (head, body, tail) if part), _MAX_RATIONALE_CHARS)

    # -- Evidence -----------------------------------------------------------
    # The taxonomy's evidence comes first because it is what justifies the
    # classification; the model signals follow. Capped at the schema's limit so
    # the list stays scannable in the UI rather than becoming a log dump.
    evidence: list[str] = list(match.evidence[:5])
    evidence.append(f"propensity={propensity.score:.2f} (model {propensity.model_version})")
    if propensity.is_fallback:
        evidence.append("propensity_source='heuristic_fallback'")
    if override_note:
        evidence.append(f"override applied for category '{category.value}'")

    return AgentRecoveryPlan(
        failure_category=category,
        # Clamped because the schema requires [0, 1] and a bad table entry should
        # surface as an odd-looking confidence in the UI, not as a 500 that hides
        # the recommendation the operator was waiting for.
        confidence=min(max(match.confidence, 0.0), 1.0),
        strategy=strategy,
        rationale=rationale,
        customer_message=_clamp(playbook.customer_message, _MAX_CUSTOMER_MESSAGE_CHARS),
        evidence=evidence[:_MAX_EVIDENCE_ITEMS],
    )
