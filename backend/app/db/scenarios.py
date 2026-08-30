"""
Catalogue of realistic Razorpay payment failures.

Why this file exists
--------------------
A reviewer cannot make a real card decline on demand, and the whole thesis of
RecoverAI is that *the right recovery action differs per failure reason*. That
thesis is untestable without a supply of failures that differ in the ways real
ones do. So the failures are catalogued rather than invented at random: the
seeder draws from here to populate a fresh database, and
``POST /api/payments/simulate-failure`` draws from here when a demo needs one
more failure of a specific kind on stage.

Fidelity to the real gateway
----------------------------
Every field mirrors the shape Razorpay actually returns in its ``error`` object,
because the classifier that consumes these values in production will see exactly
this vocabulary:

*   ``error_code``   -- ``BAD_REQUEST_ERROR`` | ``GATEWAY_ERROR`` | ``SERVER_ERROR``
*   ``error_source`` -- ``bank`` | ``gateway`` | ``customer`` | ``business`` | ``internal``
*   ``error_step``   -- ``payment_initiation`` | ``payment_authentication``
                        | ``payment_authorization`` | ``payment_response``
*   ``error_reason`` -- the machine token, e.g. ``insufficient_funds``
*   ``error_description`` -- the customer-facing sentence

Note how little signal ``error_code`` carries on its own: the majority of real
declines arrive as ``BAD_REQUEST_ERROR``, which is why the taxonomy has to read
``error_reason`` and ``error_step`` rather than keying off the code. Reproducing
that flatness is the point -- a fixture set where every failure had a distinct
code would make the classifier look far cleverer than it is.

The answer key
--------------
``expected_category`` records what a correct classifier *should* conclude. It is
documentation and a test oracle only. It is deliberately never read by
``app/agent/taxonomy.py`` -- a classifier that can see its own answer key proves
nothing, and wiring it in would turn every classification test into a tautology.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from app.domain.enums import FailureCategory, PaymentMethod
from app.domain.errors import NotFoundError


@dataclass(frozen=True)
class FailureScenario:
    """
    One reproducible payment failure.

    Frozen because the catalogue is shared, process-wide, read-only reference
    data. If a caller could mutate a scenario, one demo request could silently
    change what every later request and every test observes.

    Attributes:
        key: Stable identifier used by ``SimulateFailureIn.scenario``.
        label: Short human title for the scenario picker in the UI.
        method: Instrument the failure occurred on. Some failures are only
            possible on some rails -- there is no such thing as an expired card
            on a UPI collect request.
        error_code: Razorpay's top-level error code.
        error_source: Which party's system produced the failure.
        error_step: How far the payment got before it failed.
        error_reason: Razorpay's machine-readable reason token.
        error_description: The sentence a customer would be shown.
        expected_category: The ``FailureCategory`` a correct classifier should
            return. Tests and docs only; never consulted by the classifier.
    """

    key: str
    label: str
    method: PaymentMethod
    error_code: str
    error_source: str
    error_step: str
    error_reason: str
    error_description: str
    expected_category: FailureCategory


#: The catalogue, keyed by ``FailureScenario.key``.
#:
#: Coverage is by *category*, not by count: every member of ``FailureCategory``
#: appears at least once, so the seeded database exercises every branch of the
#: taxonomy, every playbook, and both terminal outcomes the guardrails can
#: produce (a hard DENY on ``suspected_fraud``, a forced manual review on an
#: unclassifiable failure). The categories that dominate real merchant traffic --
#: bank declines, insufficient funds, UPI timeouts, bad instruments -- get more
#: than one entry each, so a seeded dashboard shows a plausible distribution
#: rather than a perfectly flat one.
SCENARIOS: dict[str, FailureScenario] = {
    # ---- BANK_DECLINE ---------------------------------------------------
    "bank_decline_card": FailureScenario(
        key="bank_decline_card",
        label="Card declined by issuing bank",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="payment_failed",
        error_description="Your payment was declined by the bank. Please contact your bank or try a different payment method.",
        expected_category=FailureCategory.BANK_DECLINE,
        # Merchant action: do not re-present the same card immediately -- the
        # issuer already said no. Offer a different rail (UPI) instead.
    ),
    "bank_decline_netbanking": FailureScenario(
        key="bank_decline_netbanking",
        label="Netbanking transaction declined",
        method=PaymentMethod.NETBANKING,
        error_code="BAD_REQUEST_ERROR",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="payment_failed",
        error_description="The bank declined this netbanking transaction.",
        expected_category=FailureCategory.BANK_DECLINE,
        # Merchant action: same as a card decline -- switch rails rather than
        # repeat. Netbanking declines are rarely transient.
    ),
    "international_card_blocked": FailureScenario(
        key="international_card_blocked",
        label="International transactions not enabled on card",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="international_transaction_not_allowed",
        error_description="This card is not enabled for international transactions. Please use a different card or contact your bank.",
        expected_category=FailureCategory.BANK_DECLINE,
        # Merchant action: retrying is pointless until the customer changes a
        # bank setting -- send them to a domestic rail such as UPI.
    ),

    # ---- INSUFFICIENT_FUNDS ---------------------------------------------
    "insufficient_funds_card": FailureScenario(
        key="insufficient_funds_card",
        label="Card has insufficient balance",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="insufficient_funds",
        error_description="Your card does not have sufficient balance to complete this payment.",
        expected_category=FailureCategory.INSUFFICIENT_FUNDS,
        # Merchant action: wait, do not retry now. The single highest-value
        # recovery in Indian e-commerce is the salary-day retry; retrying within
        # the hour just burns a gateway call and annoys the customer.
    ),
    "insufficient_funds_upi": FailureScenario(
        key="insufficient_funds_upi",
        label="UPI account has insufficient balance",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="insufficient_funds",
        error_description="The bank account linked to this UPI ID does not have sufficient balance.",
        expected_category=FailureCategory.INSUFFICIENT_FUNDS,
        # Merchant action: retry later, and prompt the customer to pick a
        # different linked account -- most UPI users have more than one.
    ),

    # ---- UPI_TIMEOUT -----------------------------------------------------
    "upi_collect_expired": FailureScenario(
        key="upi_collect_expired",
        label="UPI collect request expired",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="upi_collect_request_expired",
        error_description="The UPI collect request expired because it was not approved in the payment app in time.",
        expected_category=FailureCategory.UPI_TIMEOUT,
        # Merchant action: re-send the collect request. Nothing was wrong with
        # the instrument -- the customer simply missed the notification.
    ),
    "upi_payment_pending_timeout": FailureScenario(
        key="upi_payment_pending_timeout",
        label="UPI payment stayed pending and timed out",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_source="gateway",
        error_step="payment_response",
        error_reason="payment_pending",
        error_description="The UPI payment remained pending and was not confirmed by the bank within the allowed window.",
        expected_category=FailureCategory.UPI_TIMEOUT,
        # Merchant action: confirm with the gateway before retrying. A pending
        # UPI payment can still settle late, and charging twice is worse than
        # recovering slowly.
    ),

    # ---- INVALID_INSTRUMENT ---------------------------------------------
    "card_expired": FailureScenario(
        key="card_expired",
        label="Card has expired",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_initiation",
        error_reason="card_expired",
        error_description="The card has expired. Please use a different card.",
        expected_category=FailureCategory.INVALID_INSTRUMENT,
        # Merchant action: ask for new details. Retrying the same stored card is
        # guaranteed to fail every single time.
    ),
    "invalid_card_number": FailureScenario(
        key="invalid_card_number",
        label="Invalid card number entered",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_initiation",
        error_reason="invalid_card_number",
        error_description="The card number entered is invalid. Please check the details and try again.",
        expected_category=FailureCategory.INVALID_INSTRUMENT,
        # Merchant action: a data-entry slip. Send the customer back to a fresh
        # checkout rather than replaying the bad number.
    ),
    "invalid_vpa": FailureScenario(
        key="invalid_vpa",
        label="Invalid or inactive UPI ID",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_initiation",
        error_reason="invalid_vpa",
        error_description="The UPI ID entered is not valid or is no longer active.",
        expected_category=FailureCategory.INVALID_INSTRUMENT,
        # Merchant action: collect a new VPA, or switch the customer to a card.
        # A dead VPA does not come back to life on retry.
    ),

    # ---- AUTHENTICATION_FAILED ------------------------------------------
    "incorrect_otp": FailureScenario(
        key="incorrect_otp",
        label="Incorrect OTP during 3-D Secure",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="incorrect_otp",
        error_description="The OTP entered was incorrect, so the payment could not be authenticated.",
        expected_category=FailureCategory.AUTHENTICATION_FAILED,
        # Merchant action: retry the same card. The instrument and the balance
        # are both fine; only the one-time code was mistyped, and this is one of
        # the highest-converting retries there is.
    ),

    # ---- SESSION_EXPIRED -------------------------------------------------
    "checkout_session_expired": FailureScenario(
        key="checkout_session_expired",
        label="Checkout session expired",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_source="gateway",
        error_step="payment_initiation",
        error_reason="payment_timed_out",
        error_description="The checkout session expired before the payment was completed.",
        expected_category=FailureCategory.SESSION_EXPIRED,
        # Merchant action: send a fresh payment link. The customer showed intent
        # and nothing about their instrument is in question.
    ),

    # ---- CUSTOMER_ABANDONED ---------------------------------------------
    "customer_cancelled_upi": FailureScenario(
        key="customer_cancelled_upi",
        label="Customer cancelled the payment",
        method=PaymentMethod.UPI,
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="payment_cancelled",
        error_description="The customer cancelled the payment request in the UPI app.",
        expected_category=FailureCategory.CUSTOMER_ABANDONED,
        # Merchant action: one gentle reminder, then stop. A cancellation is a
        # decision, not a fault, and chasing it repeatedly reads as harassment.
    ),

    # ---- GATEWAY_ERROR ---------------------------------------------------
    "gateway_technical_error": FailureScenario(
        key="gateway_technical_error",
        label="Gateway technical error",
        method=PaymentMethod.CARD,
        error_code="GATEWAY_ERROR",
        error_source="gateway",
        error_step="payment_authorization",
        error_reason="gateway_technical_error",
        error_description="The payment gateway reported a technical error while processing this transaction.",
        expected_category=FailureCategory.GATEWAY_ERROR,
        # Merchant action: retry the same method shortly. Nothing is wrong with
        # the customer or the card -- the rails had a bad minute.
    ),
    "issuer_down": FailureScenario(
        key="issuer_down",
        label="Issuing bank unavailable",
        method=PaymentMethod.NETBANKING,
        error_code="GATEWAY_ERROR",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="issuer_down",
        error_description="The bank's payment system was unavailable at the time of this transaction. Please try again shortly.",
        expected_category=FailureCategory.GATEWAY_ERROR,
        # Merchant action: retry after the outage, or route to UPI immediately --
        # bank downtime is per-institution, so a different rail often works now.
    ),

    # ---- NETWORK_ERROR ---------------------------------------------------
    "network_timeout": FailureScenario(
        key="network_timeout",
        label="Network timeout awaiting bank response",
        method=PaymentMethod.WALLET,
        error_code="GATEWAY_ERROR",
        error_source="gateway",
        error_step="payment_response",
        error_reason="network_error",
        error_description="The connection to the bank timed out before a response was received.",
        expected_category=FailureCategory.NETWORK_ERROR,
        # Merchant action: reconcile before retrying. A lost response means we do
        # not know whether the customer was charged -- assume nothing.
    ),

    # ---- RISK_BLOCKED ----------------------------------------------------
    "suspected_fraud": FailureScenario(
        key="suspected_fraud",
        label="Blocked as suspected fraud",
        method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="suspected_fraud",
        error_description="This transaction was blocked because it was flagged as suspected fraud.",
        expected_category=FailureCategory.RISK_BLOCKED,
        # Merchant action: never automate a retry. Re-presenting a transaction a
        # risk engine rejected is, at best, a wasted call and at worst helps push
        # a stolen card through. This is the scenario the demo uses to show a
        # hard DENY that no amount of AI confidence can override.
    ),

    # ---- UNKNOWN ---------------------------------------------------------
    "unclassified_failure": FailureScenario(
        key="unclassified_failure",
        label="Unclassified gateway failure",
        method=PaymentMethod.EMI,
        error_code="",
        error_source="",
        error_step="payment_response",
        error_reason="",
        error_description="The payment could not be completed.",
        expected_category=FailureCategory.UNKNOWN,
        # Merchant action: send it to a human. This scenario carries no reason
        # token at all, which is what real unmapped failures look like, and it
        # proves the system routes "I do not know" to manual review instead of
        # guessing at somebody's money.
    ),
}


def get_scenario(key: str) -> FailureScenario:
    """
    Look up one scenario by key.

    Args:
        key: A ``FailureScenario.key``, e.g. ``"insufficient_funds_card"``.

    Returns:
        The matching scenario.

    Raises:
        NotFoundError: The key is not in the catalogue. The known keys are
            attached to ``detail`` so an API caller who mistypes a scenario gets
            the valid list back in the same response instead of having to go
            read this file.
    """
    scenario = SCENARIOS.get(key)
    if scenario is None:
        raise NotFoundError(
            f"Unknown failure scenario '{key}'.",
            detail={"known_scenarios": sorted(SCENARIOS)},
        )
    return scenario


def random_scenario(rng: random.Random) -> FailureScenario:
    """
    Pick a scenario at random.

    Args:
        rng: The random source to draw from. Passing the generator in -- rather
            than calling the module-level ``random`` functions -- is what lets the
            seeder produce a byte-identical database from a fixed seed. A
            reviewer who reports "case 4 shows the wrong strategy" and a
            developer reproducing it must be looking at the same data.

    Returns:
        One ``FailureScenario`` from the catalogue.

    The draw is uniform over scenarios, which is *not* a realistic production
    mix: in live traffic, bank declines and insufficient funds together account
    for most failures, and suspected fraud is rare. Weighting was considered and
    rejected for the seeder, because the purpose of the seed data is to
    demonstrate that every branch behaves correctly, and a production-realistic
    distribution could easily generate a hundred payments without ever producing
    the risk-blocked case that shows the guardrails refusing the AI. Realism of
    *proportion* is worth less here than coverage of *behaviour*.

    ``tuple(...)`` over ``SCENARIOS.values()`` is safe to index because Python
    dictionaries preserve insertion order, so a given seed always maps to the
    same scenario.
    """
    return rng.choice(tuple(SCENARIOS.values()))
