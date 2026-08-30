"""
The thirteen guardrail rules -- the limits the AI cannot argue its way past.

**Why this module exists.** The agent recommends; these rules decide. Everything
upstream of this file (the LLM, the taxonomy, the propensity model) produces an
*opinion*. Nothing here reads an opinion as authority: each rule re-derives its
own verdict from facts and from ``Settings``, and the most restrictive verdict
wins in ``app/policy/engine.py``.

**Why every rule is a pure function.** A rule takes a fully-populated
``GuardrailContext`` and returns a ``GuardrailEvaluation``. It performs no
database query, no HTTP call, no clock read and no config lookup of its own --
even "now" is passed in. The service layer precomputes every input.

    The pay-off is testability. A financial control that can only be exercised by
    standing up a database, seeding rows and manufacturing a clock is a control
    nobody actually tests at the edges, and the edges are where money is lost. As
    pure functions, all thirteen rules can be driven through their boundary
    conditions -- one paise under a ceiling, one second inside a cooldown -- in
    plain unit tests with no fixtures at all. The rejected alternative was to let
    rules query the session directly, which reads more naturally but makes
    exhaustive testing expensive enough that it stops happening.

**Why a passing rule still returns an evaluation.** A rule that stays silent on
success cannot be rendered. The approval screen shows the operator the *whole*
checklist -- the twelve checks that passed as well as the one that fired --
because a human asked to authorise money movement needs to see what was verified,
not only what went wrong. Silence would also make "was this rule even evaluated?"
unanswerable months later in the audit trail.

**Why ``observed`` and ``limit`` are strings.** They are display values
("Rs 25,000.00" vs "Rs 10,000.00"), formatted here where the rule's units are
known. Handing the frontend raw numbers would force it to know that R4 is money,
R2 is seconds and R10 is a probability -- i.e. it would have to re-implement the
policy in TypeScript to render it, and a second copy of a policy is a second
policy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import Settings
from app.db.models import Customer, Payment
from app.domain.enums import FailureCategory, GuardrailDecision, RecoveryStrategy

# ---------------------------------------------------------------------------
# Public data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardrailEvaluation:
    """
    The result of running one rule.

    Frozen because an evaluation is evidence: once produced it is serialised onto
    the case row and hashed into the audit ledger. A mutable verdict could be
    edited after the fact by any later code holding a reference, which would make
    the stored decision unfalsifiable.

    Attributes:
        rule_id: Stable machine identifier, e.g. ``"R1_MAX_ATTEMPTS"``.
        name: Short human label, shown as the checklist row title.
        description: One sentence explaining what the rule protects against.
        decision: ``ALLOW``, ``REQUIRE_APPROVAL`` or ``DENY``.
        passed: True only when ``decision is ALLOW``; see ``_evaluated``.
        reason: Complete sentence, written for a merchant operator.
        observed: What this case actually shows, as a display string.
        limit: What the policy permits, in the same units as ``observed``.
        applicable: False when the rule had nothing to constrain -- the proposed
            strategy creates no payment attempt at all, so every money rule is
            vacuously satisfied.

            This is a separate field rather than something a reader infers from
            ``reason``, because the two states look identical on the wire
            otherwise: both carry ``decision=ALLOW, passed=True``. Rendering a
            fraud case as thirteen green ticks would tell an operator that
            thirteen checks examined it and approved, when in fact none of them
            were consulted. Making the distinction explicit lets the checklist
            show those rows greyed out, and means the frontend never has to
            string-match a human-readable sentence to decide how to draw a row.
    """

    rule_id: str
    name: str
    description: str
    decision: GuardrailDecision
    passed: bool
    reason: str
    observed: str | None = None
    limit: str | None = None
    applicable: bool = True


@dataclass(frozen=True)
class GuardrailContext:
    """
    Every fact the thirteen rules are allowed to see.

    This is the seam that keeps the rules pure. The service layer runs the
    queries -- how many attempts exist, how much budget today has already
    committed, when the last attempt happened -- and freezes the answers here
    before any rule executes. Two consequences follow, both deliberate:

    *   All thirteen rules judge the *same* instant and the *same* counts. If
        rules queried independently, a slow evaluation could straddle a midnight
        boundary or a concurrent write and produce a self-inconsistent verdict.
    *   The evaluation is reproducible. Given the stored context, the exact
        verdict can be recomputed later, which is what makes the audit record
        worth keeping.

    Attributes:
        payment: The original failed payment. Read only for its amount and age.
        customer: The payer. Read only for the risk flag.
        strategy: What the agent proposed. Drives the not-applicable short-circuit.
        failure_category: Classified reason for the original failure.
        propensity_score: ML-predicted P(recovery succeeds), in [0, 1].
        amount_paise: The amount that would be charged. Integer paise, always.
        attempt_number: Which attempt this would be, counting from 1.
        now: The single instant the whole evaluation is judged against.
        last_attempt_at: When the previous attempt on this case was created.
        open_attempt_exists: True when an unresolved order is already live.
        daily_recovery_total_paise: Value of recovery orders created today.
        customer_cases_today: Recovery cases opened for this customer today.
        settings: The policy limits in force.
    """

    payment: Payment
    customer: Customer
    strategy: RecoveryStrategy
    failure_category: FailureCategory
    propensity_score: float
    amount_paise: int
    attempt_number: int
    now: datetime
    last_attempt_at: datetime | None
    open_attempt_exists: bool
    daily_recovery_total_paise: int
    customer_cases_today: int
    settings: Settings


#: Signature every rule implements. Declared as a type alias so the registry
#: below is checkable: a helper that forgets to return an evaluation is a type
#: error rather than a rule that silently contributes nothing to the verdict.
RuleFn = Callable[[GuardrailContext], GuardrailEvaluation]


@dataclass(frozen=True)
class Rule:
    """
    One registered rule: its identity, its explanation and its implementation.

    Keeping identity and implementation together as data (rather than as a
    ``if/elif`` ladder inside the engine) is what lets ``GET /api/policy``
    publish the catalogue and the UI render a checklist without either of them
    knowing a single rule's internals.
    """

    rule_id: str
    name: str
    description: str
    fn: RuleFn


#: Returned by every rule when the proposed strategy moves no money. The engine
#: -- not the individual rules -- applies this; see ``PolicyEngine.evaluate``.
#: Exported as a constant so the engine and its tests cannot drift apart on
#: wording.
NOT_APPLICABLE_REASON = "Not applicable: this strategy creates no payment attempt."


# ---------------------------------------------------------------------------
# Formatting helpers
#
# These exist so that units are formatted exactly once, next to the rule that
# owns them. They are private: nothing outside the policy package should be
# rendering guardrail values, because the display string *is* the contract with
# the frontend.
# ---------------------------------------------------------------------------


def _rupees(paise: int) -> str:
    """
    Render integer paise as an Indian-grouped rupee string.

    ``1234567 -> "Rs 12,345.67"``, ``20000000 -> "Rs 2,00,000.00"``. Indian
    grouping puts the last three digits together and then groups the remainder in
    pairs, which is what a merchant in India expects to read.

    The grouping is done by hand rather than with ``locale.format_string`` and
    ``en_IN``. ``locale`` depends on the operating system having that locale
    installed and on a process-global ``setlocale`` call; on Windows the locale
    name differs and the call frequently fails, and a process-global mutation to
    format a number is a poor trade in any case. Twelve lines of arithmetic are
    deterministic on every machine.

    Args:
        paise: Amount in integer paise. Negative values are rendered with a
            leading minus; they should not occur, but silently dropping a sign in
            a money formatter would be worse than showing one.

    Returns:
        A display string such as ``"Rs 10,000.00"``.
    """
    sign = "-" if paise < 0 else ""
    whole, fraction = divmod(abs(paise), 100)
    digits = str(whole)

    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        groups.append(tail)
        grouped = ",".join(groups)

    return f"Rs {sign}{grouped}.{fraction:02d}"


def _plural(count: int, singular: str) -> str:
    """Render ``count`` with a naively pluralised noun (``"1 attempt"``)."""
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def _duration(seconds: float) -> str:
    """
    Render a span of seconds in the largest unit that stays readable.

    Cooldowns are configured in seconds but read badly that way: "900 seconds"
    makes an operator do arithmetic during an incident, "15 minutes" does not.
    """
    total = int(seconds)
    if total < 60:
        return _plural(total, "second")
    if total < 3600:
        return _plural(total // 60, "minute")
    return _plural(total // 3600, "hour")


def _percent(probability: float) -> str:
    """
    Render a probability as a percentage with one decimal place.

    One decimal, not zero: the propensity floor is 0.15, and rounding 0.149 and
    0.151 both to "15%" would make a denial look identical to an approval on the
    screen that justifies it.
    """
    return f"{probability * 100:.1f}%"


def _as_utc(moment: datetime) -> datetime:
    """
    Return ``moment`` as a timezone-aware UTC datetime.

    SQLite has no native timestamp type, so a value written as aware UTC comes
    back from the database naive on some drivers. Subtracting a naive datetime
    from an aware one raises ``TypeError``, and a guardrail that raises is a
    guardrail that does not run. Every timestamp this application writes comes
    from ``app.db.models.utcnow``, so attaching UTC to a naive value restores
    known information rather than guessing at it.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _hours_between(earlier: datetime, later: datetime) -> float:
    """Whole and fractional hours from ``earlier`` to ``later``, both normalised to UTC."""
    return (_as_utc(later) - _as_utc(earlier)).total_seconds() / 3600.0


def _category_label(category: FailureCategory) -> str:
    """``FailureCategory.RISK_BLOCKED -> "risk blocked"`` for prose."""
    return category.value.replace("_", " ")


@dataclass(frozen=True)
class _RuleMeta:
    """
    A rule's identity, declared immediately above its implementation.

    The alternative was to write the id, name and description once in the
    ``RULES`` registry and again inside each function's return value. That
    duplication is exactly how a UI ends up labelling rule R7's failure with
    rule R8's text, so the strings are declared once here and both the registry
    and the evaluation read from the same object.
    """

    rule_id: str
    name: str
    description: str


def _evaluated(
    meta: _RuleMeta,
    decision: GuardrailDecision,
    reason: str,
    *,
    observed: str | None = None,
    limit: str | None = None,
) -> GuardrailEvaluation:
    """
    Build a ``GuardrailEvaluation`` from a rule's metadata and its verdict.

    ``passed`` is *derived* from ``decision`` rather than passed in. That closes
    a whole class of bug by construction: no rule can report ``passed=True``
    alongside ``DENY``, so the tick marks on the approval screen can never
    disagree with the decision that actually gated the money. The meaning of
    ``passed`` is therefore precise -- "this rule imposes no restriction" -- and
    a ``REQUIRE_APPROVAL`` rule is correctly *not* passed, because it has not
    yet been satisfied; a human still has to satisfy it.

    Args:
        meta: The rule's stable id, label and description.
        decision: This rule's verdict for the case at hand.
        reason: A complete sentence explaining the verdict to an operator.
        observed: What the case shows, as a display string.
        limit: What policy permits, in the same units as ``observed``.

    Returns:
        A frozen ``GuardrailEvaluation`` ready to be stored and rendered.
    """
    return GuardrailEvaluation(
        rule_id=meta.rule_id,
        name=meta.name,
        description=meta.description,
        decision=decision,
        passed=decision is GuardrailDecision.ALLOW,
        reason=reason,
        observed=observed,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# The rules
#
# Read them as a list of the ways an automated recovery can be wrong:
#   R1, R2   -- we are pestering the customer
#   R3, R12  -- we should never have tried this one at all
#   R4, R5   -- the amount is large enough that a human should look
#   R6, R9   -- we are about to charge the wrong thing, or charge twice
#   R7, R8   -- the blast radius of a systemic mistake must stay bounded
#   R10, R11 -- the attempt is not worth the friction it costs
#   R13      -- a person, named in the audit trail, said yes
# ---------------------------------------------------------------------------


_R1 = _RuleMeta(
    "R1_MAX_ATTEMPTS",
    "Maximum recovery attempts",
    "Caps how many recovery attempts a single failed payment may ever generate.",
)


def _r1_max_attempts(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny once a case has used its attempt budget.

    The budget is counted per case, not per customer and not per day, because
    the thing being bounded is how many times *one* failed payment is
    re-presented. The second attempt catches most transient failures; the third
    mostly annoys the customer and burns gateway calls.
    """
    limit = ctx.settings.max_recovery_attempts
    observed = _plural(ctx.attempt_number, "attempt")
    allowed = _plural(limit, "attempt")

    if ctx.attempt_number > limit:
        return _evaluated(
            _R1,
            GuardrailDecision.DENY,
            f"Maximum recovery attempts ({limit}) already used.",
            observed=observed,
            limit=allowed,
        )
    return _evaluated(
        _R1,
        GuardrailDecision.ALLOW,
        f"Attempt {ctx.attempt_number} of {limit} is within the cap.",
        observed=observed,
        limit=allowed,
    )


_R2 = _RuleMeta(
    "R2_COOLDOWN",
    "Attempt cooldown",
    "Requires a minimum quiet period between two attempts on the same case.",
)


def _r2_cooldown(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny a retry that arrives too soon after the previous one.

    Two purposes, one rule: it stops a retry storm from a double-clicked button
    or a replayed webhook, and it gives an issuer-side transient fault time to
    clear -- retrying a bank outage after four seconds simply reproduces it.

    A case with no previous attempt passes rather than being skipped, so the
    checklist still shows the operator that the cooldown was considered.
    """
    cooldown = _duration(ctx.settings.recovery_cooldown_seconds)

    if ctx.last_attempt_at is None:
        return _evaluated(
            _R2,
            GuardrailDecision.ALLOW,
            "No previous attempt on this case, so no cooldown applies.",
            observed="no previous attempt",
            limit=cooldown,
        )

    elapsed_seconds = (_as_utc(ctx.now) - _as_utc(ctx.last_attempt_at)).total_seconds()
    elapsed = _duration(elapsed_seconds)

    if elapsed_seconds < ctx.settings.recovery_cooldown_seconds:
        return _evaluated(
            _R2,
            GuardrailDecision.DENY,
            f"Only {elapsed} since the previous attempt; {cooldown} must pass first.",
            observed=elapsed,
            limit=cooldown,
        )
    return _evaluated(
        _R2,
        GuardrailDecision.ALLOW,
        f"{elapsed} since the previous attempt, so the {cooldown} cooldown is satisfied.",
        observed=elapsed,
        limit=cooldown,
    )


_R3 = _RuleMeta(
    "R3_RECOVERABLE_CATEGORY",
    "Recoverable failure category",
    "Blocks automated recovery for failure categories that must never be retried.",
)


def _r3_recoverable_category(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny categories that are structurally not worth retrying.

    The list lives on ``FailureCategory.is_recoverable``, not here, so the
    taxonomy owns its own semantics and this rule cannot drift from it.
    ``RISK_BLOCKED`` is excluded because re-presenting a transaction the risk
    engine stopped is at best a wasted call and at worst helps a stolen
    instrument through. ``UNKNOWN`` is excluded because we cannot reason about a
    failure we could not classify -- absence of evidence is not evidence of
    safety.
    """
    label = _category_label(ctx.failure_category)

    if not ctx.failure_category.is_recoverable:
        return _evaluated(
            _R3,
            GuardrailDecision.DENY,
            f"Failure category '{label}' is on the never-retry list.",
            observed=label,
            limit="recoverable categories only",
        )
    return _evaluated(
        _R3,
        GuardrailDecision.ALLOW,
        f"Failure category '{label}' is recoverable.",
        observed=label,
        limit="recoverable categories only",
    )


_R4 = _RuleMeta(
    "R4_AMOUNT_CEILING",
    "Absolute amount ceiling",
    "Refuses automated recovery above the largest amount the system may ever re-present.",
)


def _r4_amount_ceiling(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny anything above the hard ceiling.

    Distinct from R5: R5 says "a human should look at this", R4 says "this does
    not belong in the automated path at all, take it to manual review". Merging
    them into one graduated rule was rejected because the two outcomes differ --
    one is an approval queue, the other is an escalation -- and an operator
    reading the checklist should be able to tell which one fired.
    """
    ceiling = ctx.settings.max_recovery_amount_paise
    observed = _rupees(ctx.amount_paise)
    allowed = _rupees(ceiling)

    if ctx.amount_paise > ceiling:
        return _evaluated(
            _R4,
            GuardrailDecision.DENY,
            f"Amount {observed} is above the automated recovery ceiling of {allowed}.",
            observed=observed,
            limit=allowed,
        )
    return _evaluated(
        _R4,
        GuardrailDecision.ALLOW,
        f"Amount {observed} is within the automated recovery ceiling of {allowed}.",
        observed=observed,
        limit=allowed,
    )


_R5 = _RuleMeta(
    "R5_HIGH_VALUE_REVIEW",
    "High-value review",
    "Escalates large recoveries to an operator even when every other check is green.",
)


def _r5_high_value_review(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Require sign-off once the amount reaches the review threshold.

    The comparison is ``>=``, not ``>``: a threshold described to a merchant as
    "Rs 10,000 needs review" must include Rs 10,000 itself. Off-by-one on a
    money boundary is the kind of defect that is only ever found in production.
    """
    threshold = ctx.settings.high_value_review_threshold_paise
    observed = _rupees(ctx.amount_paise)
    allowed = _rupees(threshold)

    if ctx.amount_paise >= threshold:
        return _evaluated(
            _R5,
            GuardrailDecision.REQUIRE_APPROVAL,
            f"Amount {observed} is at or above the high-value review threshold of {allowed}, "
            "so an operator must sign it off.",
            observed=observed,
            limit=allowed,
        )
    return _evaluated(
        _R5,
        GuardrailDecision.ALLOW,
        f"Amount {observed} is below the high-value review threshold of {allowed}.",
        observed=observed,
        limit=allowed,
    )


_R6 = _RuleMeta(
    "R6_DUPLICATE_ORDER",
    "No duplicate open order",
    "Prevents a second live recovery order while one is already outstanding.",
)


def _r6_duplicate_order(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny while an unresolved recovery order is already live.

    This is the application-level half of a two-part guarantee. The database
    half is the unique constraint on ``recovery_attempts.idempotency_key``,
    which is what actually makes a double charge impossible. This rule exists so
    the operator gets a sentence explaining why, rather than an integrity error
    surfaced as a 500.
    """
    if ctx.open_attempt_exists:
        return _evaluated(
            _R6,
            GuardrailDecision.DENY,
            "An open recovery order already exists for this case.",
            observed="1 open attempt",
            limit="0 open attempts",
        )
    return _evaluated(
        _R6,
        GuardrailDecision.ALLOW,
        "No open recovery order exists for this case.",
        observed="0 open attempts",
        limit="0 open attempts",
    )


_R7 = _RuleMeta(
    "R7_DAILY_BUDGET",
    "Daily recovery budget",
    "Bounds the total value of recovery orders this system may create in one day.",
)


def _r7_daily_budget(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny once today's committed recovery value would exceed the budget.

    This is a blast-radius cap, not a business target. If the agent, the seed
    data or an operator goes wrong, the worst case is bounded and known in
    advance rather than discovered from a settlement report.

    The projected total -- today's committed value *plus* this amount -- is what
    is compared, because a limit checked only against past spend lets the single
    order that breaches it through.
    """
    budget = ctx.settings.daily_recovery_budget_paise
    projected = ctx.daily_recovery_total_paise + ctx.amount_paise
    observed = _rupees(projected)
    allowed = _rupees(budget)

    if projected > budget:
        return _evaluated(
            _R7,
            GuardrailDecision.DENY,
            f"This recovery would take today's committed total to {observed}, "
            f"above the daily budget of {allowed}.",
            observed=observed,
            limit=allowed,
        )
    return _evaluated(
        _R7,
        GuardrailDecision.ALLOW,
        f"Today's committed total would be {observed}, within the daily budget of {allowed}.",
        observed=observed,
        limit=allowed,
    )


_R8 = _RuleMeta(
    "R8_CUSTOMER_VELOCITY",
    "Per-customer velocity",
    "Limits how many recovery cases one customer may be subjected to in a day.",
)


def _r8_customer_velocity(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny once a customer has already been chased enough times today.

    R7 protects the merchant from a systemic mistake; this protects an
    individual customer from being the person that mistake lands on. Counted in
    cases rather than attempts because the customer experiences one contact per
    case, whatever happens inside it.

    The comparison is ``>=``: with a limit of three, the customer's fourth case
    is the one that must be refused, so the check fires while the count already
    stands at three.
    """
    limit = ctx.settings.max_cases_per_customer_per_day
    observed = f"{ctx.customer_cases_today} cases today"
    allowed = f"{limit} cases per day"

    if ctx.customer_cases_today >= limit:
        return _evaluated(
            _R8,
            GuardrailDecision.DENY,
            f"Customer already has {ctx.customer_cases_today} recovery cases today; "
            f"the daily limit is {limit}.",
            observed=observed,
            limit=allowed,
        )
    return _evaluated(
        _R8,
        GuardrailDecision.ALLOW,
        f"Customer has {ctx.customer_cases_today} recovery cases today, "
        f"within the daily limit of {limit}.",
        observed=observed,
        limit=allowed,
    )


_R9 = _RuleMeta(
    "R9_AMOUNT_INTEGRITY",
    "Amount integrity",
    "Confirms the recovery amount is exactly the original payment amount.",
)


def _r9_amount_integrity(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny any amount that is not the original payment amount, to the paise.

    The agent is already structurally incapable of setting an amount -- its plan
    schema has no money field -- so in a correct system this rule never fires.
    It is kept precisely because it never fires: it converts a structural claim
    ("the AI cannot change the amount") into a checked one, and it would catch a
    future refactor that introduces a partial-recovery feature without thinking
    the policy through.
    """
    expected = ctx.payment.amount_paise
    observed = _rupees(ctx.amount_paise)
    allowed = _rupees(expected)

    if ctx.amount_paise != expected:
        return _evaluated(
            _R9,
            GuardrailDecision.DENY,
            f"Recovery amount {observed} does not match the original payment amount {allowed}.",
            observed=observed,
            limit=allowed,
        )
    return _evaluated(
        _R9,
        GuardrailDecision.ALLOW,
        f"Recovery amount {observed} matches the original payment exactly.",
        observed=observed,
        limit=allowed,
    )


_R10 = _RuleMeta(
    "R10_PROPENSITY_FLOOR",
    "Minimum success likelihood",
    "Refuses recoveries the model expects to fail, since a failed retry still costs the customer friction.",
)


def _r10_propensity_floor(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny when the predicted probability of success is below the floor.

    Note what this rule does *not* do: it does not let a high score approve
    anything. The model can only ever subtract permission here, never add it,
    which is why a mis-calibrated or fallback score cannot widen the system's
    authority -- it can only make it more conservative.
    """
    floor = ctx.settings.min_propensity_score
    observed = _percent(ctx.propensity_score)
    allowed = _percent(floor)

    if ctx.propensity_score < floor:
        return _evaluated(
            _R10,
            GuardrailDecision.DENY,
            f"Predicted success likelihood {observed} is below the {allowed} floor.",
            observed=observed,
            limit=allowed,
        )
    return _evaluated(
        _R10,
        GuardrailDecision.ALLOW,
        f"Predicted success likelihood {observed} clears the {allowed} floor.",
        observed=observed,
        limit=allowed,
    )


_R11 = _RuleMeta(
    "R11_PAYMENT_FRESHNESS",
    "Payment freshness",
    "Refuses to chase a failure that is too old to still reflect the customer's intent.",
)


def _r11_payment_freshness(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny recovery of a stale payment.

    Age is measured from the original payment's creation against ``ctx.now``,
    the same instant every other rule uses. Past the window the card details and
    the customer's intent have both likely moved on, and a payment request for a
    purchase someone abandoned a fortnight ago reads as a billing error, not a
    helpful reminder.
    """
    max_hours = ctx.settings.max_payment_age_hours
    age_hours = _hours_between(ctx.payment.created_at, ctx.now)
    observed = _plural(int(age_hours), "hour")
    allowed = _plural(max_hours, "hour")

    if age_hours > max_hours:
        return _evaluated(
            _R11,
            GuardrailDecision.DENY,
            f"Payment is {observed} old, past the {allowed} freshness window.",
            observed=observed,
            limit=allowed,
        )
    return _evaluated(
        _R11,
        GuardrailDecision.ALLOW,
        f"Payment is {observed} old, within the {allowed} freshness window.",
        observed=observed,
        limit=allowed,
    )


_R12 = _RuleMeta(
    "R12_CUSTOMER_RISK_FLAG",
    "Customer risk flag",
    "Honours the merchant's own risk decision about a customer.",
)


def _r12_customer_risk_flag(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Deny automated recovery for a customer the merchant has flagged.

    The flag is set by the merchant's risk processes, outside this system, and
    this rule deliberately does not interpret or re-score it. A guardrail that
    second-guessed a human risk decision would not be a guardrail.
    """
    if ctx.customer.risk_flagged:
        return _evaluated(
            _R12,
            GuardrailDecision.DENY,
            "Customer is flagged for risk review, so automated recovery is not permitted.",
            observed="risk flagged",
            limit="not flagged",
        )
    return _evaluated(
        _R12,
        GuardrailDecision.ALLOW,
        "Customer carries no risk flag.",
        observed="not flagged",
        limit="not flagged",
    )


_R13 = _RuleMeta(
    "R13_HUMAN_APPROVAL",
    "Explicit human approval",
    "Requires a named person to authorise every money-moving action.",
)


def _r13_human_approval(ctx: GuardrailContext) -> GuardrailEvaluation:
    """
    Require a human to say yes -- unless the narrow auto-approval lane applies.

    This rule is the product. Everything else bounds *what* may be proposed;
    this one insists that a person, named in the audit trail, authorises it.

    The auto-approval lane is present to show that the policy engine can express
    graduated autonomy: a merchant who has watched the system work will
    eventually want small, high-confidence recoveries to go through unattended.
    It is **off by default and off in the submitted demo** -- ``require_human_approval``
    is True and ``auto_approve_enabled`` is False in ``Settings``, so this rule
    returns ``REQUIRE_APPROVAL`` for every rupee. All four conditions must hold
    before it does anything else:

    1.  the lane is switched on, and
    2.  the master "every action needs a human" switch is off, and
    3.  the amount is under the lane's own ceiling, and
    4.  the model is confident enough.

    ``require_human_approval`` is checked as a veto rather than folded into the
    other three because it is the master switch: reading it separately means a
    merchant can disable all autonomy with one flag without having to reason
    about the lane's thresholds at all.
    """
    cfg = ctx.settings
    observed = f"{_rupees(ctx.amount_paise)} at {_percent(ctx.propensity_score)}"
    allowed = f"{_rupees(cfg.auto_approve_max_paise)} at {_percent(cfg.auto_approve_min_propensity)}"

    if cfg.require_human_approval:
        return _evaluated(
            _R13,
            GuardrailDecision.REQUIRE_APPROVAL,
            "Policy requires a human to approve every money-moving action.",
            observed=observed,
            limit=allowed,
        )
    if not cfg.auto_approve_enabled:
        return _evaluated(
            _R13,
            GuardrailDecision.REQUIRE_APPROVAL,
            "The auto-approval lane is disabled, so an operator must approve this recovery.",
            observed=observed,
            limit=allowed,
        )
    if ctx.amount_paise > cfg.auto_approve_max_paise:
        return _evaluated(
            _R13,
            GuardrailDecision.REQUIRE_APPROVAL,
            f"Amount {_rupees(ctx.amount_paise)} is above the auto-approval limit of "
            f"{_rupees(cfg.auto_approve_max_paise)}, so an operator must approve this recovery.",
            observed=observed,
            limit=allowed,
        )
    if ctx.propensity_score < cfg.auto_approve_min_propensity:
        return _evaluated(
            _R13,
            GuardrailDecision.REQUIRE_APPROVAL,
            f"Success likelihood {_percent(ctx.propensity_score)} is below the auto-approval "
            f"floor of {_percent(cfg.auto_approve_min_propensity)}, so an operator must approve "
            "this recovery.",
            observed=observed,
            limit=allowed,
        )
    return _evaluated(
        _R13,
        GuardrailDecision.ALLOW,
        f"Auto-approval lane applies: {_rupees(ctx.amount_paise)} is within "
        f"{_rupees(cfg.auto_approve_max_paise)} and success likelihood "
        f"{_percent(ctx.propensity_score)} clears "
        f"{_percent(cfg.auto_approve_min_propensity)}.",
        observed=observed,
        limit=allowed,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Every rule, in the order they are evaluated and, more importantly, in the
#: order they are shown to the operator. The engine evaluates all of them
#: unconditionally -- it never stops at the first denial -- because a checklist
#: that hides the second problem sends an operator round the loop twice.
RULES: tuple[Rule, ...] = tuple(
    Rule(rule_id=meta.rule_id, name=meta.name, description=meta.description, fn=fn)
    for meta, fn in (
        (_R1, _r1_max_attempts),
        (_R2, _r2_cooldown),
        (_R3, _r3_recoverable_category),
        (_R4, _r4_amount_ceiling),
        (_R5, _r5_high_value_review),
        (_R6, _r6_duplicate_order),
        (_R7, _r7_daily_budget),
        (_R8, _r8_customer_velocity),
        (_R9, _r9_amount_integrity),
        (_R10, _r10_propensity_floor),
        (_R11, _r11_payment_freshness),
        (_R12, _r12_customer_risk_flag),
        (_R13, _r13_human_approval),
    )
)

#: How many checks the approval screen renders. Derived, never typed by hand, so
#: adding a rule cannot leave the UI claiming a stale count.
RULE_COUNT: int = len(RULES)


def _assert_unique_rule_ids() -> None:
    """
    Fail at import time if two rules share a ``rule_id``.

    A duplicated id is silent damage rather than a crash: the evaluations list
    would contain two rows keyed the same, so a UI keying its checklist by
    ``rule_id`` would render one and drop the other -- quite possibly dropping
    the denial. Catching it when the module loads means the application refuses
    to start rather than under-reporting a guardrail.

    Written as an explicit ``raise`` and not an ``assert`` statement: assertions
    are removed when Python runs under ``-O``, and a safety check that vanishes
    under an optimisation flag is not a safety check.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for rule in RULES:
        if rule.rule_id in seen:
            duplicates.append(rule.rule_id)
        seen.add(rule.rule_id)
    if duplicates:
        raise RuntimeError(
            "Duplicate guardrail rule_id(s) registered in app.policy.rules: "
            + ", ".join(sorted(set(duplicates)))
        )


_assert_unique_rule_ids()
