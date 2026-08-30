"""
The domain-knowledge layer: Razorpay's error vocabulary -> our failure taxonomy,
and our failure taxonomy -> a merchant-level recovery playbook.

Why this module exists
----------------------
A payment gateway tells you *what* went wrong in its own words, spread across
five loosely-coupled fields (``error_code``, ``error_reason``,
``error_description``, ``error_source``, ``error_step``). None of those fields
tells you *what to do about it*, and they disagree with each other often enough
that you cannot just read one and stop.

This module does two jobs, kept deliberately separate:

1.  ``classify_error()`` -- normalise the gateway's vocabulary into exactly one
    :class:`~app.domain.enums.FailureCategory`, with a confidence score and an
    evidence list naming which field drove the decision. The evidence list is
    what the audit trail renders to justify the classification: "we called this
    an insufficient-funds failure *because* ``error_reason`` said so", not
    "because the AI said so".

2.  ``PLAYBOOK`` -- for each category, the recovery action a payments person
    would take, and why. This is the encoded domain expertise of the product.
    Both planners consult it: the deterministic one in
    ``app.agent.rule_planner`` follows it directly, and the LLM one reads it
    through the ``get_recovery_policy`` tool.

Separating classification from response matters: correcting a mis-mapped error
code must never require touching the recovery strategy, and changing a strategy
must never require re-mapping error codes.

Nothing here touches the database, the network or the clock. It is pure data
plus pure functions, which is what makes it directly unit-testable and what
lets the agent's tools call it without a session.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.enums import FailureCategory, PaymentMethod, RecoveryStrategy

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaxonomyMatch:
    """
    The outcome of classifying one payment failure.

    Frozen because a classification is a finding about a past event. Once the
    case row has been written, mutating the object that produced it would leave
    the audit trail describing a decision that no longer exists in memory.

    Attributes:
        category: The normalised failure category.
        confidence: How much the *classification* (not the recovery odds) can be
            trusted, in ``[0.0, 1.0]``. Driven purely by which field matched --
            see the tier table in :func:`classify_error`.
        matched_on: Which tier produced the answer. One of ``"reason"``,
            ``"error_code"``, ``"description"``, ``"source"``, ``"default"``.
        evidence: Short human-readable strings naming the fields that drove and
            corroborated the decision, e.g.
            ``["error_reason='insufficient_funds' -> insufficient_funds",
            "error_source='bank'"]``. Rendered verbatim in the approval UI.
    """

    category: FailureCategory
    confidence: float
    matched_on: str
    evidence: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Confidence tiers
# ---------------------------------------------------------------------------
# These constants are named rather than inlined so that the tier a match came
# from is greppable, and so a reviewer can see the whole ladder at once.
#
# The ordering below is the heart of the classifier. It runs most-specific
# first and stops at the first hit, because a weaker signal must never be able
# to overrule a stronger one:
#
#   * ``error_reason`` is Razorpay's most specific machine-readable field. It is
#     a closed vocabulary written for programs, so an exact hit is close to
#     ground truth -- hence the highest confidence.
#   * ``error_code`` is a three-value bucket (BAD_REQUEST_ERROR / GATEWAY_ERROR
#     / SERVER_ERROR). On its own it is nearly useless, but paired with
#     ``error_step`` (which stage of the flow died) it becomes genuinely
#     informative: a bad request at *authentication* is an OTP problem, the same
#     code at *authorization* is an issuer decline.
#   * ``error_description`` is free text intended for humans. It is accurate but
#     unstable -- wording changes between gateway releases and substring
#     matching is inherently fuzzy -- so it sits below the machine fields.
#   * ``error_source`` has four possible values. It narrows the failure to a
#     participant (bank / gateway / customer) but says nothing about the fault
#     itself. It is a last resort before giving up.
#   * Nothing matched is not a soft failure. ``UNKNOWN`` is deliberately
#     non-recoverable (see ``FailureCategory.is_recoverable``), so an unmatched
#     error is routed to a human instead of being guessed at.
CONFIDENCE_REASON = 0.92
CONFIDENCE_CODE = 0.80
CONFIDENCE_DESCRIPTION = 0.70
CONFIDENCE_SOURCE = 0.55
CONFIDENCE_DEFAULT = 0.30


# ---------------------------------------------------------------------------
# Tier 1 -- error.reason
# ---------------------------------------------------------------------------
#: Razorpay's ``error.reason`` values, mapped to our categories.
#:
#: Keys are lowercase because that is what the gateway sends, but every lookup
#: normalises first -- see the note on casing above ``_norm``. The list is
#: intentionally long: this is the tier that does most of the real work, and
#: every reason we can name here is one fewer case that falls through to fuzzy
#: description matching.
REASON_MAP: dict[str, FailureCategory] = {
    # -- Balance ----------------------------------------------------------
    "insufficient_funds": FailureCategory.INSUFFICIENT_FUNDS,
    "insufficient_balance": FailureCategory.INSUFFICIENT_FUNDS,
    "wallet_insufficient_balance": FailureCategory.INSUFFICIENT_FUNDS,
    # -- Issuer / bank decline --------------------------------------------
    # ``payment_failed`` is Razorpay's catch-all. It is mapped to BANK_DECLINE
    # rather than UNKNOWN because in practice it is overwhelmingly an issuer
    # refusal, and the BANK_DECLINE playbook (switch rails) is the safe answer
    # even when the true cause was something else on the issuer side.
    "payment_failed": FailureCategory.BANK_DECLINE,
    "payment_declined_by_bank": FailureCategory.BANK_DECLINE,
    "declined_by_issuer": FailureCategory.BANK_DECLINE,
    "issuer_declined": FailureCategory.BANK_DECLINE,
    "card_blocked": FailureCategory.BANK_DECLINE,
    "account_blocked": FailureCategory.BANK_DECLINE,
    "transaction_limit_exceeded": FailureCategory.BANK_DECLINE,
    "payment_limit_exceeded": FailureCategory.BANK_DECLINE,
    "daily_limit_exceeded": FailureCategory.BANK_DECLINE,
    "international_transaction_not_allowed": FailureCategory.BANK_DECLINE,
    # -- Instrument itself is wrong ---------------------------------------
    "invalid_card_number": FailureCategory.INVALID_INSTRUMENT,
    "incorrect_card_details": FailureCategory.INVALID_INSTRUMENT,
    "card_expired": FailureCategory.INVALID_INSTRUMENT,
    "expired_card": FailureCategory.INVALID_INSTRUMENT,
    "invalid_expiry": FailureCategory.INVALID_INSTRUMENT,
    "invalid_cvv": FailureCategory.INVALID_INSTRUMENT,
    "incorrect_cvv": FailureCategory.INVALID_INSTRUMENT,
    "card_not_supported": FailureCategory.INVALID_INSTRUMENT,
    "unsupported_card": FailureCategory.INVALID_INSTRUMENT,
    "invalid_vpa": FailureCategory.INVALID_INSTRUMENT,
    "invalid_account": FailureCategory.INVALID_INSTRUMENT,
    "invalid_bank_account": FailureCategory.INVALID_INSTRUMENT,
    # -- Authentication (OTP / 3-D Secure / UPI PIN) -----------------------
    "authentication_failed": FailureCategory.AUTHENTICATION_FAILED,
    "3ds_authentication_failed": FailureCategory.AUTHENTICATION_FAILED,
    "invalid_otp": FailureCategory.AUTHENTICATION_FAILED,
    "incorrect_otp": FailureCategory.AUTHENTICATION_FAILED,
    "otp_expired": FailureCategory.AUTHENTICATION_FAILED,
    "otp_attempts_exceeded": FailureCategory.AUTHENTICATION_FAILED,
    "invalid_upi_pin": FailureCategory.AUTHENTICATION_FAILED,
    # -- UPI collect flow --------------------------------------------------
    "upi_collect_expired": FailureCategory.UPI_TIMEOUT,
    "upi_collect_request_expired": FailureCategory.UPI_TIMEOUT,
    "collect_request_timed_out": FailureCategory.UPI_TIMEOUT,
    "upi_app_not_responding": FailureCategory.UPI_TIMEOUT,
    "payment_pending": FailureCategory.UPI_TIMEOUT,
    # -- Checkout session --------------------------------------------------
    "session_expired": FailureCategory.SESSION_EXPIRED,
    "checkout_session_expired": FailureCategory.SESSION_EXPIRED,
    "order_expired": FailureCategory.SESSION_EXPIRED,
    # -- Gateway / infrastructure ------------------------------------------
    # Includes issuer-side OUTAGES. A bank that is down is not a bank that
    # declined: the instrument is fine and the customer is fine, the rail is
    # temporarily unavailable. Categorising these as BANK_DECLINE would send the
    # customer to a different payment method to solve a problem that fixes
    # itself, which is worse advice than doing nothing. The categories in this
    # taxonomy are named for the action they imply, and the right action here is
    # GATEWAY_ERROR's: retry the same method shortly.
    "issuer_not_available": FailureCategory.GATEWAY_ERROR,
    "issuer_down": FailureCategory.GATEWAY_ERROR,
    "issuer_unavailable": FailureCategory.GATEWAY_ERROR,
    "bank_technical_error": FailureCategory.GATEWAY_ERROR,
    "netbanking_down": FailureCategory.GATEWAY_ERROR,
    "gateway_technical_error": FailureCategory.GATEWAY_ERROR,
    "gateway_error": FailureCategory.GATEWAY_ERROR,
    "gateway_timeout": FailureCategory.GATEWAY_ERROR,
    "server_error": FailureCategory.GATEWAY_ERROR,
    "service_unavailable": FailureCategory.GATEWAY_ERROR,
    "network_error": FailureCategory.NETWORK_ERROR,
    "connection_error": FailureCategory.NETWORK_ERROR,
    # -- Customer walked away ----------------------------------------------
    "payment_cancelled": FailureCategory.CUSTOMER_ABANDONED,
    "payment_cancelled_by_user": FailureCategory.CUSTOMER_ABANDONED,
    "payment_abandoned": FailureCategory.CUSTOMER_ABANDONED,
    "user_dropped_off": FailureCategory.CUSTOMER_ABANDONED,
    # -- Risk ---------------------------------------------------------------
    "risk_threshold_breached": FailureCategory.RISK_BLOCKED,
    "fraud_suspected": FailureCategory.RISK_BLOCKED,
    "suspected_fraud": FailureCategory.RISK_BLOCKED,
    "stolen_card": FailureCategory.RISK_BLOCKED,
    "lost_card": FailureCategory.RISK_BLOCKED,
    "payment_blocked_by_risk": FailureCategory.RISK_BLOCKED,
}


#: Reasons whose correct category depends on which rail the payment used.
#:
#: Razorpay reuses some reason tokens across instruments, and a few of them mean
#: genuinely different things depending on the flow. ``payment_timed_out`` is the
#: clear case: on UPI it is a collect request that expired unanswered in the
#: customer's payment app, while on a card or netbanking flow it is the hosted
#: checkout session elapsing. Both are timeouts; only one is a UPI timeout.
#:
#: The distinction earns its keep because the two categories carry different
#: propensity base rates and different customer messages -- telling a card user
#: to "approve the request in your UPI app" would be nonsense.
#:
#: These are consulted *before* :data:`REASON_MAP`, at the same confidence tier:
#: the reason field is still what drove the decision, the method only disambiguates
#: it. ``__default__`` covers the case where no method was recorded, which happens
#: when a payment fails before an instrument was ever chosen.
METHOD_CONDITIONAL_REASONS: dict[str, dict[str, FailureCategory]] = {
    "payment_timed_out": {
        "upi": FailureCategory.UPI_TIMEOUT,
        "__default__": FailureCategory.SESSION_EXPIRED,
    },
}


# ---------------------------------------------------------------------------
# Tier 2 -- error.code (optionally refined by error.step)
# ---------------------------------------------------------------------------
#: Razorpay's ``error.code`` values. Uppercased keys, matching the wire format.
#:
#: There are only three of them, which is exactly why this tier sits *below*
#: ``error.reason``: on its own the code separates "the request was wrong" from
#: "our side broke", and no more than that.
ERROR_CODE_MAP: dict[str, FailureCategory] = {
    "BAD_REQUEST_ERROR": FailureCategory.BANK_DECLINE,
    "GATEWAY_ERROR": FailureCategory.GATEWAY_ERROR,
    "SERVER_ERROR": FailureCategory.GATEWAY_ERROR,
}

#: ``(error_code, error_step)`` pairs, consulted before ``ERROR_CODE_MAP``.
#:
#: ``error_step`` names the stage of the payment flow that died, and that is
#: what turns a useless code into a usable one. The same ``BAD_REQUEST_ERROR``
#: means three different things depending on where it happened:
#:
#:   * ``payment_initiation``    -- the instrument was rejected before anyone
#:                                  was asked to authorise anything, so the card
#:                                  or VPA details are the problem.
#:   * ``payment_authentication`` -- the customer failed the OTP / 3-D Secure /
#:                                  UPI PIN challenge.
#:   * ``payment_authorization``  -- the issuer was asked and said no.
#:
#: The alternative -- one flat map on the code alone -- was rejected because it
#: would classify every card failure as a generic bank decline and therefore
#: recommend switching rails even for an OTP mistype, where the same card would
#: have worked on a second try.
CODE_STEP_MAP: dict[tuple[str, str], FailureCategory] = {
    ("BAD_REQUEST_ERROR", "payment_initiation"): FailureCategory.INVALID_INSTRUMENT,
    ("BAD_REQUEST_ERROR", "payment_authentication"): FailureCategory.AUTHENTICATION_FAILED,
    ("BAD_REQUEST_ERROR", "payment_authorization"): FailureCategory.BANK_DECLINE,
    ("BAD_REQUEST_ERROR", "payment_capture"): FailureCategory.BANK_DECLINE,
    ("GATEWAY_ERROR", "payment_initiation"): FailureCategory.GATEWAY_ERROR,
    ("GATEWAY_ERROR", "payment_authentication"): FailureCategory.GATEWAY_ERROR,
    ("GATEWAY_ERROR", "payment_authorization"): FailureCategory.GATEWAY_ERROR,
    ("SERVER_ERROR", "payment_initiation"): FailureCategory.GATEWAY_ERROR,
    ("SERVER_ERROR", "payment_authorization"): FailureCategory.GATEWAY_ERROR,
}


# ---------------------------------------------------------------------------
# Tier 3 -- error.description
# ---------------------------------------------------------------------------
#: Lowercase substrings checked against ``error_description``, in order.
#:
#: A tuple of pairs rather than a dict, because **order is part of the meaning**:
#: the first hit wins, so the most specific phrase has to be tested before the
#: general one. "insufficient balance in your account" contains "account", and
#: "expired" appears in both "card has expired" and "the collect request
#: expired" -- a dict would give whichever the interpreter happened to iterate
#: first, which is not a specification.
DESCRIPTION_PATTERNS: tuple[tuple[str, FailureCategory], ...] = (
    # Balance -- checked first because these phrases also contain generic words
    # like "card" and "account" that later patterns would otherwise claim.
    ("insufficient balance", FailureCategory.INSUFFICIENT_FUNDS),
    ("insufficient funds", FailureCategory.INSUFFICIENT_FUNDS),
    ("not enough balance", FailureCategory.INSUFFICIENT_FUNDS),
    ("low balance", FailureCategory.INSUFFICIENT_FUNDS),
    # Risk -- second, because a fraud phrase must never be captured by a
    # generic "declined" pattern below. Mis-filing a fraud block as a plain
    # decline would send it down a recoverable path.
    ("suspected fraud", FailureCategory.RISK_BLOCKED),
    ("fraudulent", FailureCategory.RISK_BLOCKED),
    ("security reasons", FailureCategory.RISK_BLOCKED),
    ("risk", FailureCategory.RISK_BLOCKED),
    ("stolen", FailureCategory.RISK_BLOCKED),
    ("blocked by the bank", FailureCategory.RISK_BLOCKED),
    # Authentication
    ("otp", FailureCategory.AUTHENTICATION_FAILED),
    ("3d secure", FailureCategory.AUTHENTICATION_FAILED),
    ("3ds", FailureCategory.AUTHENTICATION_FAILED),
    ("authentication", FailureCategory.AUTHENTICATION_FAILED),
    ("upi pin", FailureCategory.AUTHENTICATION_FAILED),
    ("password", FailureCategory.AUTHENTICATION_FAILED),
    # UPI collect flow -- "collect request" before the bare "timed out" below,
    # so a UPI expiry is not swallowed by the generic gateway-timeout pattern.
    ("collect request", FailureCategory.UPI_TIMEOUT),
    ("upi app", FailureCategory.UPI_TIMEOUT),
    ("psp", FailureCategory.UPI_TIMEOUT),
    # Instrument
    ("card has expired", FailureCategory.INVALID_INSTRUMENT),
    ("expired card", FailureCategory.INVALID_INSTRUMENT),
    ("invalid card", FailureCategory.INVALID_INSTRUMENT),
    ("incorrect card", FailureCategory.INVALID_INSTRUMENT),
    ("invalid cvv", FailureCategory.INVALID_INSTRUMENT),
    ("card is not supported", FailureCategory.INVALID_INSTRUMENT),
    ("invalid vpa", FailureCategory.INVALID_INSTRUMENT),
    ("virtual payment address", FailureCategory.INVALID_INSTRUMENT),
    ("invalid account", FailureCategory.INVALID_INSTRUMENT),
    # Session
    ("session expired", FailureCategory.SESSION_EXPIRED),
    ("session has expired", FailureCategory.SESSION_EXPIRED),
    ("checkout closed", FailureCategory.SESSION_EXPIRED),
    ("order expired", FailureCategory.SESSION_EXPIRED),
    # Customer intent
    ("cancelled by the user", FailureCategory.CUSTOMER_ABANDONED),
    ("cancelled by user", FailureCategory.CUSTOMER_ABANDONED),
    ("customer cancelled", FailureCategory.CUSTOMER_ABANDONED),
    ("abandoned", FailureCategory.CUSTOMER_ABANDONED),
    # Infrastructure -- last, because these words ("error", "timed out",
    # "unavailable") appear inside more specific messages above.
    ("network", FailureCategory.NETWORK_ERROR),
    ("connection", FailureCategory.NETWORK_ERROR),
    ("timed out", FailureCategory.GATEWAY_ERROR),
    ("timeout", FailureCategory.GATEWAY_ERROR),
    ("unavailable", FailureCategory.GATEWAY_ERROR),
    ("try again", FailureCategory.GATEWAY_ERROR),
    ("technical error", FailureCategory.GATEWAY_ERROR),
    ("gateway", FailureCategory.GATEWAY_ERROR),
    # Generic decline -- the floor of this tier. Anything that reached here and
    # says "declined" is an issuer refusal we could not characterise further.
    ("declined", FailureCategory.BANK_DECLINE),
    ("do not honour", FailureCategory.BANK_DECLINE),
    ("do not honor", FailureCategory.BANK_DECLINE),
    ("limit exceeded", FailureCategory.BANK_DECLINE),
    ("not permitted", FailureCategory.BANK_DECLINE),
)


# ---------------------------------------------------------------------------
# Tier 4 -- error.source
# ---------------------------------------------------------------------------
#: Which participant reported the failure. Four values, no detail -- this is the
#: weakest signal we accept before declaring the failure unknown.
SOURCE_MAP: dict[str, FailureCategory] = {
    "bank": FailureCategory.BANK_DECLINE,
    "issuer": FailureCategory.BANK_DECLINE,
    "gateway": FailureCategory.GATEWAY_ERROR,
    "razorpay": FailureCategory.GATEWAY_ERROR,
    "internal": FailureCategory.GATEWAY_ERROR,
    "business": FailureCategory.GATEWAY_ERROR,
    "network": FailureCategory.NETWORK_ERROR,
    "customer": FailureCategory.CUSTOMER_ABANDONED,
}

#: ``(error_source, method)`` refinements, consulted before ``SOURCE_MAP``.
#:
#: The payment method is allowed to influence the answer **at this tier only**.
#: The instrument tells you how a payment was attempted, not why it failed, so
#: letting it colour a machine-readable reason would be a weak signal overruling
#: a strong one. But when all we have left is "the customer's side reported
#: it", the rails genuinely change the interpretation: on UPI that is almost
#: always a collect request nobody approved, whereas on a card page it is a
#: person closing the tab. Those two facts lead to different recovery actions,
#: so the distinction is worth encoding.
#:
#: Only entries that actually differ from ``SOURCE_MAP`` are listed; duplicating
#: the default here would be noise that later drifts out of sync.
SOURCE_METHOD_MAP: dict[tuple[str, str], FailureCategory] = {
    ("customer", PaymentMethod.UPI.value): FailureCategory.UPI_TIMEOUT,
    ("customer", PaymentMethod.NETBANKING.value): FailureCategory.SESSION_EXPIRED,
}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _norm(value: str | None) -> str:
    """
    Normalise a gateway string for matching: strip whitespace, lowercase.

    Gateway strings arrive with inconsistent casing and stray whitespace --
    ``"BAD_REQUEST_ERROR"``, ``"payment_authorization "``, ``"Bank"`` -- because
    they pass through webhooks, dashboards, CSV exports and our own seed data
    before reaching here. Every lookup normalises rather than trusting the
    caller, so a capitalisation difference can never silently drop a match into
    the ``UNKNOWN`` bucket and, from there, into a human's queue.

    Args:
        value: Raw field from the gateway, possibly ``None``.

    Returns:
        The trimmed, lowercased string, or ``""`` when the input was ``None``.
    """
    if value is None:
        return ""
    return value.strip().lower()


def _norm_code(value: str | None) -> str:
    """
    Normalise an ``error_code`` to the uppercase form used by ``ERROR_CODE_MAP``.

    Args:
        value: Raw error code, possibly ``None``.

    Returns:
        The trimmed, uppercased code, or ``""`` when the input was ``None``.
    """
    if value is None:
        return ""
    return value.strip().upper()


def _corroborating(
    *,
    driver: str,
    code: str,
    reason: str,
    source: str,
    step: str,
    method: str,
) -> list[str]:
    """
    Build the supporting half of the evidence list.

    The tier that decided the category writes its own headline entry (``field=
    'value' -> category``). This function adds the *other* populated fields as
    plain context, so a reviewer reading the audit trail can see what else was
    on the record -- including anything that looks inconsistent with the chosen
    category, which is precisely the signal a human reviewer needs.

    ``error_description`` is deliberately excluded: it is a paragraph of free
    text and would drown the short, scannable list the approval UI renders.

    Args:
        driver: Name of the field that decided the category, so it is not
            repeated.
        code: Normalised ``error_code``.
        reason: Normalised ``error_reason``.
        source: Normalised ``error_source``.
        step: Normalised ``error_step``.
        method: Normalised payment method.

    Returns:
        Zero to four short strings, in a stable field order.
    """
    pairs = (
        ("error_reason", reason),
        ("error_code", code),
        ("error_step", step),
        ("error_source", source),
        ("method", method),
    )
    return [f"{name}={value!r}" for name, value in pairs if value and name != driver]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_error(
    *,
    error_code: str | None = None,
    error_reason: str | None = None,
    error_description: str | None = None,
    error_source: str | None = None,
    error_step: str | None = None,
    method: str | None = None,
) -> TaxonomyMatch:
    """
    Map a Razorpay failure onto one :class:`FailureCategory`.

    Resolution runs most-specific-first and stops at the first hit. See the
    commentary on the ``CONFIDENCE_*`` constants above for why the tiers are
    ordered this way; in short, a weaker field must never overrule a stronger
    one, so the search is a ladder rather than a vote.

    ======  =========================  ==========  ================
    Tier    Signal                     Confidence  ``matched_on``
    ======  =========================  ==========  ================
    1       ``error_reason`` exact     0.92        ``"reason"``
    2       ``error_code`` + step      0.80        ``"error_code"``
    3       ``error_description``      0.70        ``"description"``
    4       ``error_source`` (+method) 0.55        ``"source"``
    5       nothing matched            0.30        ``"default"``
    ======  =========================  ==========  ================

    Every argument is optional and every argument is normalised before use --
    the function is given whatever the gateway happened to send, which for an
    older webhook or a hand-seeded demo row may be almost nothing.

    Args:
        error_code: Razorpay ``error.code`` (e.g. ``"BAD_REQUEST_ERROR"``).
        error_reason: Razorpay ``error.reason`` (e.g. ``"insufficient_funds"``).
        error_description: Human-readable failure text from the gateway.
        error_source: Who reported it: ``bank`` | ``gateway`` | ``customer`` |
            ``internal``.
        error_step: Stage of the flow that failed, e.g.
            ``"payment_authorization"``.
        method: Instrument used, e.g. ``"card"`` or ``"upi"``. Influences the
            result at the ``source`` tier only.

    Returns:
        A :class:`TaxonomyMatch`. Never raises and never returns ``None``: an
        unclassifiable failure is a real outcome with its own category
        (``UNKNOWN``, which ``FailureCategory.is_recoverable`` treats as
        non-recoverable), not an error condition.
    """
    code = _norm_code(error_code)
    reason = _norm(error_reason)
    description = _norm(error_description)
    source = _norm(error_source)
    step = _norm(error_step)
    method_norm = _norm(method)

    # -- Tier 1a: reasons whose meaning depends on the payment rail --------
    # Checked before the flat map so a rail-specific reading always wins over the
    # generic one. Still tier 1 confidence: `error_reason` is what identified the
    # failure, `method` only disambiguated which sense of it applies.
    if reason in METHOD_CONDITIONAL_REASONS:
        by_method = METHOD_CONDITIONAL_REASONS[reason]
        category = by_method.get(method_norm, by_method["__default__"])
        return TaxonomyMatch(
            category=category,
            confidence=CONFIDENCE_REASON,
            matched_on="reason",
            evidence=[
                f"error_reason={reason!r} on method={method_norm or 'unknown'!r} -> {category.value}",
                *_corroborating(
                    driver="error_reason",
                    code=code,
                    reason=reason,
                    source=source,
                    step=step,
                    method=method_norm,
                ),
            ],
        )

    # -- Tier 1b: the machine-readable reason ------------------------------
    if reason in REASON_MAP:
        category = REASON_MAP[reason]
        return TaxonomyMatch(
            category=category,
            confidence=CONFIDENCE_REASON,
            matched_on="reason",
            evidence=[
                f"error_reason={reason!r} -> {category.value}",
                *_corroborating(
                    driver="error_reason",
                    code=code,
                    reason=reason,
                    source=source,
                    step=step,
                    method=method_norm,
                ),
            ],
        )

    # -- Tier 2: code, refined by the step it failed at --------------------
    if code:
        # Explicit ``is None`` rather than ``a or b``: the values are enum
        # members, and relying on their truthiness would couple this lookup to
        # the fact that no category string is empty.
        category = CODE_STEP_MAP.get((code, step))
        if category is None:
            category = ERROR_CODE_MAP.get(code)
        if category is not None:
            # Naming the step in the headline when it participated makes the
            # audit entry self-explanatory: the reader can see that
            # "authentication" is what turned a generic code into an OTP
            # failure, rather than having to know the mapping table.
            driver_text = f"error_code={code!r}"
            if (code, step) in CODE_STEP_MAP:
                driver_text = f"error_code={code!r} + error_step={step!r}"
            return TaxonomyMatch(
                category=category,
                confidence=CONFIDENCE_CODE,
                matched_on="error_code",
                evidence=[
                    f"{driver_text} -> {category.value}",
                    *_corroborating(
                        driver="error_code",
                        code=code,
                        reason=reason,
                        source=source,
                        step="" if (code, step) in CODE_STEP_MAP else step,
                        method=method_norm,
                    ),
                ],
            )

    # -- Tier 3: fuzzy match on the human-readable description -------------
    if description:
        for pattern, category in DESCRIPTION_PATTERNS:
            if pattern in description:
                return TaxonomyMatch(
                    category=category,
                    confidence=CONFIDENCE_DESCRIPTION,
                    matched_on="description",
                    evidence=[
                        f"error_description contains {pattern!r} -> {category.value}",
                        *_corroborating(
                            driver="error_description",
                            code=code,
                            reason=reason,
                            source=source,
                            step=step,
                            method=method_norm,
                        ),
                    ],
                )

    # -- Tier 4: who reported it, plus the rails it happened on ------------
    if source:
        category = SOURCE_METHOD_MAP.get((source, method_norm))
        if category is None:
            category = SOURCE_MAP.get(source)
        if category is not None:
            driver_text = f"error_source={source!r}"
            if (source, method_norm) in SOURCE_METHOD_MAP:
                driver_text = f"error_source={source!r} + method={method_norm!r}"
            return TaxonomyMatch(
                category=category,
                confidence=CONFIDENCE_SOURCE,
                matched_on="source",
                evidence=[
                    f"{driver_text} -> {category.value}",
                    *_corroborating(
                        driver="error_source",
                        code=code,
                        reason=reason,
                        source=source,
                        step=step,
                        method="" if (source, method_norm) in SOURCE_METHOD_MAP else method_norm,
                    ),
                ],
            )

    # -- Tier 5: give up honestly ------------------------------------------
    # The evidence list records what we *did* have, so the human who picks this
    # case up is not left guessing what the classifier saw.
    observed = _corroborating(
        driver="",
        code=code,
        reason=reason,
        source=source,
        step=step,
        method=method_norm,
    )
    if description:
        # The description is excluded from ``_corroborating`` because it is long,
        # but at this tier it is the most useful thing a human can be shown: an
        # unmatched description is a concrete candidate for a new entry in
        # DESCRIPTION_PATTERNS. A short excerpt is enough to act on.
        excerpt = description if len(description) <= 80 else description[:77] + "..."
        observed.append(f"error_description={excerpt!r} matched no pattern")
    return TaxonomyMatch(
        category=FailureCategory.UNKNOWN,
        confidence=CONFIDENCE_DEFAULT,
        matched_on="default",
        evidence=["no field matched the taxonomy -> unknown", *observed]
        if observed
        else ["no failure detail supplied by the gateway -> unknown"],
    )


# ---------------------------------------------------------------------------
# The recovery playbook
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Playbook:
    """
    What a payments specialist would do about one failure category, and why.

    Attributes:
        category: The failure category this entry answers.
        primary_strategy: The default recommendation.
        alternate_strategy: The fallback, used when the primary is inapplicable
            -- most often because it would "switch" to the rails that just
            failed. The planner picks it; it is not a second opinion.
        reasoning: Merchant-facing explanation, written for the operator who has
            to approve the action. Reused verbatim inside agent rationales.
        customer_message: What the customer would be told. Short, blame-free and
            free of gateway jargon, because it is read by someone who just
            wanted to buy something.
        typical_success_rate: Industry-shaped prior for "does this recovery
            work?". Documentation and a sanity anchor for the ML model's output
            -- it is **not** used as a prediction. The trained propensity model
            in ``app.ml`` is the only thing that scores a specific case.
    """

    category: FailureCategory
    primary_strategy: RecoveryStrategy
    alternate_strategy: RecoveryStrategy
    reasoning: str
    customer_message: str
    typical_success_rate: float


#: The recovery playbook: one entry per failure category, no exceptions.
#:
#: This dict is the product's domain expertise written down. The thesis of
#: RecoverAI is that the *correct* recovery action differs by failure reason, so
#: a single "retry the payment" answer -- the naive alternative, and what most
#: dunning tools actually do -- is exactly what this table exists to replace.
PLAYBOOK: dict[FailureCategory, Playbook] = {
    FailureCategory.BANK_DECLINE: Playbook(
        category=FailureCategory.BANK_DECLINE,
        primary_strategy=RecoveryStrategy.SWITCH_TO_UPI,
        alternate_strategy=RecoveryStrategy.RETRY_LATER,
        reasoning=(
            "The issuing bank refused this transaction. Re-presenting the same card to "
            "the same issuer that just declined it is the single most common wasted retry "
            "in payments: the issuer's answer is a decision about the card, not a transient "
            "fault, so an immediate second attempt normally earns a second decline plus "
            "another gateway fee. Moving the customer to UPI routes the money through a "
            "different set of rails and a different risk decision, which is why it recovers "
            "a meaningful share of these failures."
        ),
        customer_message=(
            "Your bank did not approve that payment. Paying by UPI usually goes through "
            "straight away -- here is a fresh link for the same amount."
        ),
        typical_success_rate=0.34,
    ),
    FailureCategory.INSUFFICIENT_FUNDS: Playbook(
        category=FailureCategory.INSUFFICIENT_FUNDS,
        primary_strategy=RecoveryStrategy.RETRY_LATER,
        alternate_strategy=RecoveryStrategy.SWITCH_TO_UPI,
        reasoning=(
            "The instrument works; the balance does not. Nothing about the card, the "
            "gateway or the checkout needs changing, so retrying in thirty seconds fails "
            "for exactly the same reason and simply tells the customer twice that they are "
            "short of money. Waiting -- ideally past a salary date -- is the only change "
            "that affects the outcome, so this case is scheduled rather than retried."
        ),
        customer_message=(
            "That payment could not be completed because the account did not have enough "
            "balance. We will send you a fresh payment link shortly -- nothing has been charged."
        ),
        typical_success_rate=0.41,
    ),
    FailureCategory.UPI_TIMEOUT: Playbook(
        category=FailureCategory.UPI_TIMEOUT,
        primary_strategy=RecoveryStrategy.RETRY_SAME_METHOD,
        alternate_strategy=RecoveryStrategy.SWITCH_TO_CARD,
        reasoning=(
            "Nothing was declined here. A UPI collect request was sent and simply expired "
            "unanswered -- the customer was away from their phone, missed the notification, "
            "or the PSP app was slow to deliver it. There is no evidence of any problem with "
            "the account or the mandate, so the correct action is to send the same request "
            "again while the purchase intent is still fresh. This is the highest-yield "
            "category in the whole taxonomy."
        ),
        customer_message=(
            "Your UPI request expired before it was approved. We have sent a new one -- "
            "please approve it in your UPI app to complete the payment."
        ),
        typical_success_rate=0.62,
    ),
    FailureCategory.SESSION_EXPIRED: Playbook(
        category=FailureCategory.SESSION_EXPIRED,
        primary_strategy=RecoveryStrategy.RETRY_SAME_METHOD,
        alternate_strategy=RecoveryStrategy.SWITCH_TO_UPI,
        reasoning=(
            "The checkout window closed before the payment finished -- a tab was left open "
            "too long, or a bank redirect took longer than the session allowed. The "
            "instrument was never actually refused, so the recovery is simply a new session "
            "on the same rails. Switching methods here would add friction without addressing "
            "anything that went wrong."
        ),
        customer_message=(
            "Your checkout session timed out before the payment completed. Here is a new "
            "link for the same amount -- it takes under a minute."
        ),
        typical_success_rate=0.58,
    ),
    FailureCategory.GATEWAY_ERROR: Playbook(
        category=FailureCategory.GATEWAY_ERROR,
        primary_strategy=RecoveryStrategy.RETRY_SAME_METHOD,
        alternate_strategy=RecoveryStrategy.RETRY_LATER,
        reasoning=(
            "The failure came from the payment infrastructure, not from the customer or "
            "their bank. Transient gateway and acquirer faults clear on their own, usually "
            "within minutes, and the customer's instrument was never in question. Retrying "
            "the same method after the cooldown is both the cheapest and the most likely "
            "recovery; changing rails would be treating a symptom that has already gone away."
        ),
        customer_message=(
            "That payment failed because of a temporary technical problem on our payment "
            "provider's side, not with your card. Please try again using this link."
        ),
        typical_success_rate=0.66,
    ),
    FailureCategory.NETWORK_ERROR: Playbook(
        category=FailureCategory.NETWORK_ERROR,
        primary_strategy=RecoveryStrategy.RETRY_SAME_METHOD,
        alternate_strategy=RecoveryStrategy.RETRY_LATER,
        reasoning=(
            "A transport-level failure: the request never completed a round trip, so no "
            "authorisation decision was ever made. Nobody refused anything. The same "
            "instrument on the same rails is the correct retry, and because no decision "
            "exists there is nothing to work around."
        ),
        customer_message=(
            "The connection dropped while your payment was being processed, so it did not "
            "go through. Nothing was charged -- please use this link to try again."
        ),
        typical_success_rate=0.68,
    ),
    FailureCategory.AUTHENTICATION_FAILED: Playbook(
        category=FailureCategory.AUTHENTICATION_FAILED,
        primary_strategy=RecoveryStrategy.SWITCH_TO_UPI,
        alternate_strategy=RecoveryStrategy.RETRY_SAME_METHOD,
        reasoning=(
            "The customer never got past the OTP or 3-D Secure challenge -- the message did "
            "not arrive, arrived late, or was mistyped until the attempt limit was hit. The "
            "card itself is fine, so this is a friction problem rather than a money problem. "
            "UPI replaces an SMS round trip with a PIN the customer already knows inside an "
            "app they already have open, which removes the step that failed. Retrying the "
            "same card is the reasonable fallback when UPI is not available, since OTP "
            "delivery often succeeds on a second attempt."
        ),
        customer_message=(
            "Your payment could not be verified in time -- the OTP step did not complete. "
            "Paying by UPI skips the OTP entirely; here is a link for the same amount."
        ),
        typical_success_rate=0.45,
    ),
    FailureCategory.INVALID_INSTRUMENT: Playbook(
        category=FailureCategory.INVALID_INSTRUMENT,
        primary_strategy=RecoveryStrategy.SWITCH_TO_UPI,
        alternate_strategy=RecoveryStrategy.SWITCH_TO_NETBANKING,
        reasoning=(
            "The card itself is the problem: expired, mistyped, unsupported, or a VPA that "
            "does not resolve. Retrying the identical details cannot succeed, because "
            "nothing about them will have changed by the second attempt. Recovery therefore "
            "means giving the customer a different way to pay rather than another go at the "
            "same one -- UPI first because it needs no card details at all, netbanking as "
            "the fallback."
        ),
        customer_message=(
            "We could not process those card details. You can complete the same payment by "
            "UPI or netbanking using this link -- no card required."
        ),
        typical_success_rate=0.29,
    ),
    FailureCategory.CUSTOMER_ABANDONED: Playbook(
        category=FailureCategory.CUSTOMER_ABANDONED,
        primary_strategy=RecoveryStrategy.RETRY_SAME_METHOD,
        alternate_strategy=RecoveryStrategy.SWITCH_TO_UPI,
        reasoning=(
            "The customer closed checkout without completing the payment. No instrument was "
            "refused and no system failed, so there is nothing technical to work around -- "
            "this is a re-engagement problem. A prompt link on the method they already chose "
            "is the lowest-friction nudge; the value of the recovery decays quickly as the "
            "original intent fades, which is why this is worth doing at once rather than "
            "scheduling for later."
        ),
        customer_message=(
            "You left before your payment finished, and your order is still waiting. "
            "Here is a link to complete it -- the amount is unchanged."
        ),
        typical_success_rate=0.37,
    ),
    FailureCategory.RISK_BLOCKED: Playbook(
        category=FailureCategory.RISK_BLOCKED,
        primary_strategy=RecoveryStrategy.NO_RECOVERY,
        # The alternate is deliberately identical rather than MANUAL_REVIEW.
        # Routing a risk block to a human review queue sounds harmless, but it
        # creates a path by which a flagged transaction can be argued back into
        # a recovery attempt, and it puts an operator under commercial pressure
        # to overrule a fraud engine they have less information than. There is
        # no fallback here on purpose: the answer is no.
        alternate_strategy=RecoveryStrategy.NO_RECOVERY,
        reasoning=(
            "A risk or fraud control blocked this transaction. This is the one category "
            "where the correct action is to do nothing at all. Re-presenting a transaction "
            "that a risk engine refused is, at best, a wasted gateway call, and at worst it "
            "helps push a stolen instrument through -- so the outcome is non-negotiable and "
            "is enforced by the guardrail engine independently of anything the agent "
            "recommends. If the block was wrong, that is a case for the merchant's risk "
            "team, not for an automated retry."
        ),
        customer_message=(
            "This payment could not be completed. Please contact our support team, who can "
            "look into it with you directly."
        ),
        typical_success_rate=0.0,
    ),
    FailureCategory.UNKNOWN: Playbook(
        category=FailureCategory.UNKNOWN,
        primary_strategy=RecoveryStrategy.MANUAL_REVIEW,
        alternate_strategy=RecoveryStrategy.NO_RECOVERY,
        reasoning=(
            "The gateway's failure detail did not match anything in the taxonomy, so we "
            "cannot say why this payment failed. We do not automate around a failure we "
            "could not explain: absence of evidence is not evidence that a retry is safe, "
            "and the cost of a wrong guess is charged to a real customer. A human decides "
            "this one, and the unmapped error is worth adding to the taxonomy so the next "
            "occurrence classifies cleanly."
        ),
        customer_message=(
            "Your payment did not go through. Our team is looking into it and will contact "
            "you shortly -- nothing has been charged."
        ),
        typical_success_rate=0.20,
    ),
}


def _assert_playbook_is_total() -> None:
    """
    Fail at import time if any :class:`FailureCategory` has no playbook entry.

    A missing entry is not a cosmetic gap. ``rule_planner.plan_from_rules``
    indexes ``PLAYBOOK`` directly, so an unmapped category would raise a
    ``KeyError`` deep inside an analysis run -- in production that means a real
    failed payment produces a 500 instead of a recommendation, and it would
    surface only when that particular failure first occurred in the wild.
    Checking here means adding a category to the enum without extending the
    playbook breaks the very first import, including at test collection.

    A bare ``assert`` was the obvious alternative and was rejected: ``python -O``
    strips assert statements, and a safety guarantee must not depend on which
    optimisation flag the process happened to start with.

    Raises:
        RuntimeError: If one or more categories are missing from ``PLAYBOOK``.
    """
    missing = [c.value for c in FailureCategory if c not in PLAYBOOK]
    if missing:
        raise RuntimeError(
            "PLAYBOOK is missing an entry for: "
            + ", ".join(sorted(missing))
            + ". Every FailureCategory must have a documented recovery action."
        )


_assert_playbook_is_total()
