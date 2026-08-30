"""
The payment gateway boundary: signatures in, signatures out.

Everything the customer's browser tells this server is a claim. "The payment
succeeded" arriving from a client callback is worth nothing on its own -- anyone
with curl can send it -- so a case is only ever marked recovered against an
HMAC the server computed itself from a secret the client never sees.

These tests cover both directions of that boundary: the signature the simulator
issues on a completed payment, and the signature Razorpay puts on a webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.config import Settings
from app.domain.enums import GatewayMode
from app.payments.gateway import get_gateway, reset_gateway
from app.payments.webhook import verify_webhook_signature

#: Raw bytes exactly as they would arrive on the wire, complete with the
#: whitespace and key order the sender chose. Both are load-bearing below.
WEBHOOK_BODY = (
    b'{"event": "payment.failed",  "account_id": "acc_TEST",\n'
    b'  "payload": {"payment": {"entity": {"id": "pay_wire_0001", "amount": 250000}}}}'
)


def sign(body: bytes, secret: str) -> str:
    """Razorpay's webhook scheme: hex HMAC-SHA256 of the raw request body."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def gateway(settings: Settings):
    """
    The gateway the application would select for this configuration.

    Reached through ``get_gateway`` rather than by constructing ``SimulatedGateway``
    directly, so the selection logic is part of what is under test: picking the
    right backend for the available credentials is the behaviour that matters, and
    a test that instantiated the simulator by hand would still pass if selection
    were broken.
    """
    reset_gateway()
    yield get_gateway(settings)
    reset_gateway()


def test_zero_credentials_select_the_simulator(gateway, settings: Settings) -> None:
    """
    The project's promise that it runs with nothing configured. Silently doing
    nothing, or raising at startup, would both be worse than saying "simulated"
    out loud -- which is what the mode enum is for.
    """
    assert settings.razorpay_enabled is False
    assert gateway.mode is GatewayMode.SIMULATED


def test_a_simulated_payment_produces_a_signature_the_verifier_accepts(gateway) -> None:
    """
    The simulator has to be a real cryptographic round trip, not a stub that
    returns ``True``.

    If the simulated path skipped verification, the demo would exercise a code
    path that does not exist in the Razorpay path, and the one control standing
    between a POST and a case marked recovered would never have been run before it
    mattered.
    """
    order = gateway.create_order(
        amount_paise=250_000,
        currency="INR",
        receipt="rcpt_test_0001",
        notes={"case_id": "case_under_test"},
    )
    assert order.amount_paise == 250_000  # the gateway must not restate the amount
    assert order.id

    payment_id, signature = gateway.simulate_payment(order.id, amount_paise=order.amount_paise, succeed=True)
    assert gateway.verify_payment_signature(
        order_id=order.id, payment_id=payment_id, signature=signature
    )


def test_a_tampered_signature_is_rejected(gateway) -> None:
    """A one-character edit must be enough to fail: this is a hash, not a checksum."""
    order = gateway.create_order(
        amount_paise=250_000, currency="INR", receipt="rcpt_test_0002", notes={}
    )
    payment_id, signature = gateway.simulate_payment(order.id, amount_paise=order.amount_paise, succeed=True)

    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    assert not gateway.verify_payment_signature(
        order_id=order.id, payment_id=payment_id, signature=flipped
    )


def test_a_signature_cannot_be_replayed_onto_a_different_payment(gateway) -> None:
    """
    The signature binds the order to the payment, so a valid signature harvested
    from one successful recovery cannot be pasted onto another case's payment id.
    A scheme that signed only the order id would pass the tamper test above and
    still be replayable.
    """
    order = gateway.create_order(
        amount_paise=250_000, currency="INR", receipt="rcpt_test_0003", notes={}
    )
    payment_id, signature = gateway.simulate_payment(order.id, amount_paise=order.amount_paise, succeed=True)

    assert not gateway.verify_payment_signature(
        order_id=order.id, payment_id=f"{payment_id}_other", signature=signature
    )


# ---------------------------------------------------------------------------
# Webhook signatures
# ---------------------------------------------------------------------------


def test_the_exact_bytes_that_were_signed_verify(settings: Settings) -> None:
    secret = settings.razorpay_webhook_secret
    assert secret, "the suite configures a webhook secret; without one this proves nothing"
    assert verify_webhook_signature(
        body=WEBHOOK_BODY, signature=sign(WEBHOOK_BODY, secret), secret=secret
    )


def test_re_serialising_the_body_destroys_the_signature(settings: Settings) -> None:
    """
    The bug this pins is not in the crypto, it is in the plumbing: reading the
    request as JSON and re-encoding it before verifying.

    ``json.dumps(json.loads(body))`` is semantically identical and byte-wise
    different -- the two spaces and the newline in the original vanish -- so the
    HMAC no longer matches and every genuine webhook is rejected. The fix is to
    verify against the raw body, and this test is what stops a well-meant
    refactor to ``request.json()`` from reintroducing it.
    """
    secret = settings.razorpay_webhook_secret
    signature = sign(WEBHOOK_BODY, secret)
    reserialised = json.dumps(json.loads(WEBHOOK_BODY)).encode("utf-8")

    assert reserialised != WEBHOOK_BODY
    assert not verify_webhook_signature(
        body=reserialised, signature=signature, secret=secret
    )


def test_an_empty_secret_verifies_nothing(settings: Settings) -> None:
    """
    With no webhook secret configured there is no way to tell a genuine callback
    from a forged one, so the only safe answer is "no".

    Returning ``True`` when the secret is blank -- the tempting shortcut, since it
    makes a credential-free demo work end to end -- would mean that forgetting to
    set one variable in production turns the webhook into an unauthenticated
    write endpoint.
    """
    assert not verify_webhook_signature(
        body=WEBHOOK_BODY, signature=sign(WEBHOOK_BODY, ""), secret=""
    )
