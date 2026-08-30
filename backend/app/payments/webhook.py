"""
Razorpay webhook handling: authenticate the caller, then normalise the payload.

Why this module exists
----------------------
A webhook is the one place where an unauthenticated stranger on the internet can
hand our system a "this payment failed" event and expect us to act on it. Two
separate jobs follow from that, and this module keeps them separate:

1.  **Authentication** -- ``verify_webhook_signature`` proves the body really
    came from Razorpay. Nothing in the payload is trustworthy until it passes.
2.  **Normalisation** -- ``parse_payment_failed`` flattens Razorpay's nested
    event into the field names the rest of RecoverAI uses, treating every key as
    optional because external input is not a contract we control.

Keeping them apart means the route can enforce the ordering (verify, then parse)
and a test can prove the verifier rejects a tampered body without needing a
realistic payload at all.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

#: Razorpay's ``payment.failed`` event carries the payment entity at this path.
#: Written out as a named constant because the two-level nesting is the part of
#: the payload shape most likely to be misremembered when reading the parser.
_ENTITY_PATH = ("payload", "payment", "entity")


def verify_webhook_signature(*, body: bytes, signature: str, secret: str) -> bool:
    """
    Verify Razorpay's ``X-Razorpay-Signature`` header against the request body.

    Args:
        body: The **raw** request bytes, exactly as received.
        signature: The value of the ``X-Razorpay-Signature`` header.
        secret: The webhook secret configured in the Razorpay dashboard.

    Returns:
        True only when the signature is genuine. Never raises.

    The ``body`` argument must be the raw bytes read off the wire -- this is the
    single most common way a webhook integration is broken. It is tempting to
    accept the parsed JSON and re-serialise it here, but ``json.dumps`` will not
    reproduce the original byte-for-byte: key order changes, whitespace and
    separators change, unicode escaping changes, and floats are re-formatted.
    The HMAC is over bytes, so any one of those differences produces a completely
    different digest and *every* signature check fails. In FastAPI that means the
    route must call ``await request.body()`` and pass the result here, before
    anything touches ``request.json()``.

    A missing secret returns False rather than raising, and rather than skipping
    the check. An unconfigured webhook endpoint must reject callers, not trust
    them: "no secret configured" is precisely the state an attacker would hope
    for, so it is the state that must verify nothing.
    """
    # Both the unset-secret and empty-signature cases fail closed. Note that this
    # is not a redundant guard on top of the HMAC: with an empty secret the HMAC
    # is still perfectly computable, so without this check a caller who knew the
    # secret was blank could forge a valid digest.
    if not secret or not signature or not body:
        return False

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    try:
        # compare_digest, not ==. String equality returns as soon as two bytes
        # differ, so its runtime reveals how much of the guess was right and an
        # attacker can extend a forged signature one character at a time.
        # compare_digest takes the same time whatever the inputs.
        return hmac.compare_digest(expected, signature)
    except TypeError:
        # compare_digest refuses str arguments with non-ASCII characters, and the
        # header is entirely attacker-controlled. Turning that into a 500 would
        # hand out a cheap way to fill the error log; returning False treats it
        # as what it is -- an invalid signature.
        return False


def _optional_text(value: Any) -> str | None:
    """
    Coerce an untrusted JSON value to a string, or None when it is absent/unusable.

    Args:
        value: Anything that came out of the webhook body.

    Returns:
        The string, or None. Used for the error fields, whose destination columns
        are nullable -- None means "Razorpay did not tell us", which is a
        different and more useful fact than an empty string.
    """
    # Razorpay sends JSON null for error fields on some methods, and the
    # taxonomy classifier calls ``.upper()`` on the error code, so a non-string
    # slipping through here would crash the classifier rather than this parser.
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def parse_payment_failed(event: dict) -> dict:
    """
    Flatten a Razorpay ``payment.failed`` event into RecoverAI's field names.

    Args:
        event: The decoded webhook body. Its shape is
            ``{"payload": {"payment": {"entity": {...}}}}``.

    Returns:
        A flat dict with the keys ``razorpay_payment_id``, ``razorpay_order_id``,
        ``amount_paise``, ``currency``, ``method``, ``email``, ``contact``,
        ``error_code``, ``error_source``, ``error_step``, ``error_reason`` and
        ``error_description``. Every key is always present; values are empty or
        None when the event did not carry them.

    Never raises. Every lookup is a ``.get()`` chain because this is untrusted
    external input: a ``KeyError`` here would surface as an HTTP 500 to Razorpay,
    and Razorpay responds to a 500 by retrying the delivery with backoff. One
    malformed event would then become a repeating storm of failing requests
    against our own API. Returning a mostly-empty normalised dict lets the route
    answer 200, record what little arrived, and move on.
    """
    # Walk the nesting defensively: any level may be missing or, on a malformed
    # or hostile payload, may be a list or a string rather than a dict.
    node: Any = event
    for segment in _ENTITY_PATH:
        node = node.get(segment) if isinstance(node, dict) else None
    entity: dict[str, Any] = node if isinstance(node, dict) else {}

    # Razorpay sends the amount already in paise, the currency's smallest unit.
    # There is deliberately no division here: paise in, paise stored, paise
    # compared against the guardrail limits.
    try:
        amount_paise = int(entity.get("amount") or 0)
    except (TypeError, ValueError):
        # A non-numeric amount is corrupt input, not a zero-rupee payment. Zero
        # is recorded so the row is still auditable, and the amount-integrity
        # guardrail (which compares against the original payment) is what stops
        # a recovery being built on it.
        amount_paise = 0

    return {
        "razorpay_payment_id": str(entity.get("id") or ""),
        "razorpay_order_id": str(entity.get("order_id") or ""),
        "amount_paise": amount_paise,
        # No "INR" fallback here on purpose. The default currency is declared
        # once, in ``config.Settings``; re-declaring it in this parser would give
        # the system two copies of the same default, free to drift apart. An
        # empty string means "the event did not say", and the layer that owns
        # the default supplies it.
        "currency": str(entity.get("currency") or ""),
        # "unknown" is not a re-defaulted config value -- it is a real member of
        # the PaymentMethod enum, chosen precisely for instruments we could not
        # identify, and the recovery playbook has an entry for it.
        "method": str(entity.get("method") or "unknown"),
        # Contact details land in non-nullable string columns, so they normalise
        # to "" rather than None.
        "email": str(entity.get("email") or ""),
        "contact": str(entity.get("contact") or ""),
        # On a payment.failed event these five live directly on the payment
        # entity, not inside a nested "error" object -- that nesting exists on
        # Razorpay's *API error* responses, which is a different shape entirely.
        # They feed the failure taxonomy, which is why they stay separate fields
        # instead of being merged into one message string: the code, the reason
        # and the free-text description each map to a category with different
        # confidence.
        "error_code": _optional_text(entity.get("error_code")),
        "error_source": _optional_text(entity.get("error_source")),
        "error_step": _optional_text(entity.get("error_step")),
        "error_reason": _optional_text(entity.get("error_reason")),
        "error_description": _optional_text(entity.get("error_description")),
    }
