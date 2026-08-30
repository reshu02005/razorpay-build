"""
Everything that talks to a payment gateway.

This package is the only part of RecoverAI that knows Razorpay exists. The
recovery service, the policy engine and the agent all work in terms of the
``PaymentGateway`` protocol defined here, so swapping the gateway -- or running
with no gateway credentials at all -- changes nothing above this boundary.

Two entry points matter to the rest of the app:

* ``get_gateway(settings)`` -- the outbound direction: create an order, read a
  payment, verify a checkout signature.
* ``verify_webhook_signature`` / ``parse_payment_failed`` -- the inbound
  direction: authenticate a callback from Razorpay, then normalise it.

The names are re-exported here so callers write ``from app.payments import
get_gateway`` rather than reaching into a module path they would then be coupled
to. ``__all__`` doubles as the list of things this package promises to keep
stable; anything not on it is an internal detail.
"""

from app.payments.gateway import (
    SIMULATED_KEY_SECRET,
    GatewayOrder,
    GatewayPayment,
    PaymentGateway,
    RazorpayGateway,
    SimulatedGateway,
    compute_payment_signature,
    get_gateway,
    reset_gateway,
)
from app.payments.webhook import parse_payment_failed, verify_webhook_signature

__all__ = [
    "SIMULATED_KEY_SECRET",
    "GatewayOrder",
    "GatewayPayment",
    "PaymentGateway",
    "RazorpayGateway",
    "SimulatedGateway",
    "compute_payment_signature",
    "get_gateway",
    "parse_payment_failed",
    "reset_gateway",
    "verify_webhook_signature",
]
