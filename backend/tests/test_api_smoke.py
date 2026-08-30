"""
The HTTP surface: every documented endpoint, its documented status code, and a
body that actually validates against its declared response model.

This file is a contract test, not a behaviour test -- the behaviour is covered by
the service-level and policy tests, which are far better at localising a failure.
What is *only* testable here is drift: a router that returns a hand-built dict
instead of its response model, a field renamed on the Python side but still
mirrored in ``frontend/src/lib/types.ts``, a 201 quietly downgraded to a 200.
None of that shows up in a service test, and all of it breaks the client.

So every assertion is either a status code or ``Model.model_validate(...)``.
Validating rather than spot-checking keys is the point: it fails on a missing
field, a wrong type and an unparseable enum value alike, which is the whole class
of bug that reaches the frontend.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.config import Settings
from app.db.models import Customer, Payment
from app.db.scenarios import SCENARIOS
from app.domain.enums import (
    AgentMode,
    FailureCategory,
    GatewayMode,
    PaymentMethod,
    PaymentStatus,
    RecoveryStatus,
    ToolCapability,
)
from app.domain.schemas import (
    FailureScenarioOut,
    AgentToolCallOut,
    AuditChainVerificationOut,
    AuditEventOut,
    CheckoutSessionOut,
    CustomerOut,
    DashboardMetricsOut,
    ErrorOut,
    FailureBreakdownItem,
    PaymentOut,
    PolicyOut,
    RecoveryCaseOut,
    RecoveryCaseSummaryOut,
    SystemStatusOut,
)
from app.payments.gateway import get_gateway
from app.policy.rules import RULES

OPERATOR = "ops@merchant.test"


def open_case(client, payment_id: str) -> dict:
    """Analyse a failed payment and return the case, asserting the documented 201."""
    response = client.post(
        f"/api/recovery/payments/{payment_id}/analyze", json={"force_rule_based": True}
    )
    assert response.status_code == 201, response.text
    case = RecoveryCaseOut.model_validate(response.json())
    assert case.status is RecoveryStatus.AWAITING_APPROVAL, (
        "the smoke tests need a case a human can act on; "
        f"guardrails returned {case.guardrail_decision}"
    )
    return response.json()


def approve(client, case_id: str) -> dict:
    response = client.post(
        f"/api/recovery/cases/{case_id}/approve", json={"approved_by": OPERATOR}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Health, status and metrics
# ---------------------------------------------------------------------------


def test_health_reports_ok(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_status_admits_it_is_running_simulated_and_rule_based(client) -> None:
    """
    The honesty endpoint.

    With no credentials configured the system is running a deterministic planner
    against an in-process gateway, and it has to say so -- the frontend renders
    these two values as badges. A demo that presented simulated mode as live
    Razorpay would be the single most damaging thing this project could do.
    """
    response = client.get("/api/status")
    assert response.status_code == 200
    status = SystemStatusOut.model_validate(response.json())
    assert status.gateway_mode is GatewayMode.SIMULATED
    assert status.agent_mode is AgentMode.RULE_BASED
    assert status.app and status.version


def test_dashboard_metrics_validate_on_an_empty_database(client) -> None:
    """
    Zero payments is a real state -- it is the state on first run -- and the
    recovery rate is a division whose denominator is zero there. The endpoint must
    report "0% of nothing" rather than crash or return a blank.
    """
    response = client.get("/api/metrics/dashboard")
    assert response.status_code == 200
    metrics = DashboardMetricsOut.model_validate(response.json())
    assert metrics.recovery_rate_pct == 0.0
    assert metrics.daily_budget_limit_paise > 0


def test_failure_breakdown_validates(client, failed_payment: Payment) -> None:
    """
    The breakdown is measured over recovery *cases*, not raw failed payments: a
    failure category does not exist until something has classified it. So the
    payment is analysed first, and an unanalysed failure contributing nothing
    here is the documented behaviour rather than a gap -- the dashboard surfaces
    those separately as ``unanalysed_failures``, where "nobody has looked at
    these" reads as the call to action it is instead of hiding inside an
    "unknown" slice indistinguishable from genuinely unclassifiable failures.
    """
    empty = client.get("/api/metrics/failure-breakdown")
    assert empty.status_code == 200
    assert empty.json() == [], "an unanalysed failure must not appear in the breakdown"

    analysed = client.post(
        f"/api/recovery/payments/{failed_payment.id}/analyze",
        json={"force_rule_based": True},
    )
    assert analysed.status_code == 201

    response = client.get("/api/metrics/failure-breakdown")
    assert response.status_code == 200
    items = [FailureBreakdownItem.model_validate(item) for item in response.json()]
    assert sum(item.count for item in items) >= 1


# ---------------------------------------------------------------------------
# Payments and customers
# ---------------------------------------------------------------------------


def test_payments_list_and_detail(client, failed_payment: Payment) -> None:
    listed = client.get("/api/payments", params={"status": "failed", "limit": 10})
    assert listed.status_code == 200
    payments = [PaymentOut.model_validate(item) for item in listed.json()]
    assert failed_payment.id in {payment.id for payment in payments}

    detail = client.get(f"/api/payments/{failed_payment.id}")
    assert detail.status_code == 200
    payment = PaymentOut.model_validate(detail.json())
    # Rupees appear at this boundary and nowhere else; the conversion being wrong
    # here would misprice every screen in the product.
    assert payment.amount_paise == failed_payment.amount_paise
    assert payment.amount_rupees == round(failed_payment.amount_paise / 100, 2)


def test_simulate_failure_creates_a_failed_payment(client, customer: Customer) -> None:
    """The demo helper: a reviewer cannot make a real card decline on demand."""
    response = client.post(
        "/api/payments/simulate-failure",
        json={"customer_id": customer.id, "amount_paise": 199_900},
    )
    assert response.status_code == 201, response.text
    payment = PaymentOut.model_validate(response.json())
    assert payment.amount_paise == 199_900
    assert payment.status.value == "failed"
    assert payment.error_code or payment.error_reason or payment.error_description


def test_customer_detail(client, customer: Customer) -> None:
    response = client.get(f"/api/customers/{customer.id}")
    assert response.status_code == 200
    body = CustomerOut.model_validate(response.json())
    assert body.id == customer.id
    assert body.prior_success_rate == pytest.approx(0.8)


def test_an_unknown_id_renders_the_shared_error_envelope(client) -> None:
    """
    One error shape for every failure, so the client has one branch to write.

    A FastAPI route that let a bare ``HTTPException`` escape would return
    ``{"detail": ...}`` instead, which parses as neither ``ErrorOut`` nor anything
    the frontend's Zod schemas accept -- and it would do it on the error path,
    where nobody is looking.
    """
    response = client.get("/api/payments/pay_does_not_exist")
    assert response.status_code == 404
    error = ErrorOut.model_validate(response.json())
    assert error.error == "not_found"
    assert error.message


# ---------------------------------------------------------------------------
# Recovery lifecycle
# ---------------------------------------------------------------------------


def test_the_approval_path_from_analysis_to_verified_payment(
    client, settings: Settings, failed_payment: Payment
) -> None:
    """
    Walks analyse -> list -> detail -> trace -> approve -> checkout -> verify in
    one test, because these endpoints are one workflow and testing them apart
    would need the same six-step setup six times over.
    """
    case = open_case(client, failed_payment.id)
    case_id = case["id"]

    listed = client.get("/api/recovery/cases", params={"status": "all", "limit": 50})
    assert listed.status_code == 200
    summaries = [RecoveryCaseSummaryOut.model_validate(item) for item in listed.json()]
    assert case_id in {summary.id for summary in summaries}

    detail = client.get(f"/api/recovery/cases/{case_id}")
    assert detail.status_code == 200
    assert RecoveryCaseOut.model_validate(detail.json()).id == case_id

    trace = client.get(f"/api/recovery/cases/{case_id}/trace")
    assert trace.status_code == 200
    steps = [AgentToolCallOut.model_validate(item) for item in trace.json()]
    # The deterministic planner records synthetic steps precisely so this panel is
    # never empty: "the AI explained nothing" and "the trace view is broken" look
    # identical to a reviewer.
    assert steps, "the reasoning trace must never be empty, even on the rule-based path"
    assert all(step.capability is not ToolCapability.FINANCIAL for step in steps)

    approved = RecoveryCaseOut.model_validate(approve(client, case_id))
    assert approved.status is RecoveryStatus.AWAITING_PAYMENT

    checkout_response = client.get(f"/api/recovery/cases/{case_id}/checkout")
    assert checkout_response.status_code == 200
    checkout = CheckoutSessionOut.model_validate(checkout_response.json())
    assert checkout.gateway_mode is GatewayMode.SIMULATED
    assert checkout.amount_paise == failed_payment.amount_paise

    payment_id, signature = get_gateway(settings).simulate_payment(
        checkout.order_id, amount_paise=checkout.amount_paise, succeed=True
    )
    verified = client.post(
        f"/api/recovery/cases/{case_id}/verify",
        json={
            "razorpay_order_id": checkout.order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
    )
    assert verified.status_code == 200, verified.text
    assert RecoveryCaseOut.model_validate(verified.json()).status is RecoveryStatus.RECOVERED


def test_reject_records_who_declined_and_why(client, failed_payment: Payment) -> None:
    """
    The decline is as much a decision as the approval, and the audit trail has to
    name the person who made it.
    """
    case_id = open_case(client, failed_payment.id)["id"]
    response = client.post(
        f"/api/recovery/cases/{case_id}/reject",
        json={"rejected_by": OPERATOR, "reason": "Customer already paid by bank transfer"},
    )
    assert response.status_code == 200, response.text
    rejected = RecoveryCaseOut.model_validate(response.json())
    assert rejected.status is RecoveryStatus.REJECTED
    assert rejected.rejected_by == OPERATOR
    assert rejected.rejection_reason


def test_simulate_checkout_completes_a_case_without_a_browser(
    client, failed_payment: Payment
) -> None:
    """The endpoint the demo script drives, since there is no real card to type in."""
    case_id = open_case(client, failed_payment.id)["id"]
    approve(client, case_id)

    response = client.post(
        f"/api/recovery/cases/{case_id}/simulate-checkout", params={"succeed": True}
    )
    assert response.status_code == 200, response.text
    assert RecoveryCaseOut.model_validate(response.json()).status is RecoveryStatus.RECOVERED


def test_mark_failed_closes_an_attempt_the_customer_never_completed(
    client, failed_payment: Payment
) -> None:
    case_id = open_case(client, failed_payment.id)["id"]
    approve(client, case_id)

    response = client.post(
        f"/api/recovery/cases/{case_id}/mark-failed",
        json={"reason": "Customer abandoned the recovery link"},
    )
    assert response.status_code == 200, response.text
    failed = RecoveryCaseOut.model_validate(response.json())
    assert failed.status is RecoveryStatus.FAILED
    assert failed.failure_note


# ---------------------------------------------------------------------------
# Audit and policy
# ---------------------------------------------------------------------------


def test_audit_verify_reports_a_clean_ledger(client) -> None:
    """
    On an empty database the honest answer is "valid, nothing to check". Reporting
    invalid on zero events would make the endpoint useless as a health signal --
    it would be red on every fresh install.
    """
    response = client.get("/api/audit/verify")
    assert response.status_code == 200
    result = AuditChainVerificationOut.model_validate(response.json())
    assert result.valid is True
    assert result.broken_at_sequence is None


def test_audit_list_returns_the_events_a_case_generated(client, failed_payment: Payment) -> None:
    """
    Filtering by case is what the case timeline renders. An unfiltered ledger
    would still validate as a list of events, which is exactly why the filter
    itself is asserted.
    """
    case_id = open_case(client, failed_payment.id)["id"]

    response = client.get("/api/audit", params={"case_id": case_id, "limit": 100})
    assert response.status_code == 200
    events = [AuditEventOut.model_validate(item) for item in response.json()]
    assert events, "analysing a payment must leave a trail"
    assert {event.case_id for event in events} == {case_id}


def test_policy_publishes_every_rule_in_force(client) -> None:
    """
    The merchant is held to these limits, so they have to be readable. Read-only
    on purpose: a limit an automated system could raise is not a limit.
    """
    response = client.get("/api/policy")
    assert response.status_code == 200
    policy = PolicyOut.model_validate(response.json())
    assert {rule["rule_id"] for rule in policy.rules} == {rule.rule_id for rule in RULES}
    assert policy.require_human_approval is True


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def test_a_correctly_signed_webhook_is_acknowledged(
    client, settings: Settings, customer: Customer
) -> None:
    """
    Razorpay retries anything it does not get a 2xx for, so an accepted event must
    be acknowledged promptly and unambiguously.

    The body is sent as raw bytes and signed over exactly those bytes -- the same
    discipline the signature verifier requires, and the reason ``test_gateway``
    pins the re-serialisation trap separately.
    """
    event = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_webhook_0001",
                    "order_id": "order_webhook_0001",
                    "amount": 250_000,
                    "currency": "INR",
                    "method": "card",
                    "status": "failed",
                    "email": customer.email,
                    "contact": customer.phone,
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": "payment_failed",
                    "error_description": "The payment was declined by the issuing bank.",
                    "notes": {"customer_id": customer.id},
                }
            }
        },
    }
    body = json.dumps(event).encode("utf-8")
    signature = hmac.new(
        settings.razorpay_webhook_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()

    response = client.post(
        "/api/webhooks/razorpay",
        content=body,
        headers={
            "X-Razorpay-Signature": signature,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"received": True}


def test_a_simulated_failure_records_the_scenario_instrument(client, customer: Customer) -> None:
    """
    The bug this catches: every simulated failure was recorded as an unknown
    instrument.

    ``PaymentMethod`` mixes in ``str``, but ``Enum`` still supplies ``__str__``,
    so ``str(PaymentMethod.UPI)`` is ``"PaymentMethod.UPI"`` rather than
    ``"upi"``. The boundary coercion normalised through ``str()`` and therefore
    failed to match, falling back to ``UNKNOWN`` -- and because the miss is logged
    at INFO rather than raised, nothing anywhere failed. It surfaced only as a
    payments table showing "Unknown" in the method column.

    The ``customer`` fixture is required, not incidental: the endpoint refuses to
    invent a payer, because a fabricated customer would have no payment history
    and the propensity model would silently score it against a neutral prior.

    The method matters beyond cosmetics: it is a model feature, and it decides
    whether ``payment_timed_out`` is read as a UPI collect timeout or a checkout
    session expiry -- two categories with different recovery strategies.
    """
    response = client.post(
        "/api/payments/simulate-failure",
        json={"scenario": "upi_collect_expired"},
    )
    assert response.status_code == 201
    payment = PaymentOut.model_validate(response.json())

    assert payment.method is PaymentMethod.UPI, (
        f"Expected the scenario's own instrument, got '{payment.method.value}'."
    )
    assert payment.status is PaymentStatus.FAILED
    assert payment.error_reason == "upi_collect_request_expired"


def test_the_scenario_catalogue_endpoint_matches_what_simulate_accepts(client) -> None:
    """
    Pins the contract between the picker and the simulator.

    The console's "simulate a failed payment" dropdown used to hard-code its
    scenario keys, and six of the eight it named had never existed -- including
    the fraud case, which is the single most important option in the menu,
    because it is the one the guardrails must refuse outright. Every one of those
    six returned a 404.

    The list is now served from the catalogue, so the drift cannot recur. This
    test asserts the two really are the same set, in both directions.
    """
    response = client.get("/api/payments/failure-scenarios")
    assert response.status_code == 200
    scenarios = [FailureScenarioOut.model_validate(item) for item in response.json()]

    assert {s.key for s in scenarios} == set(SCENARIOS), (
        "The endpoint and the catalogue disagree about which scenarios exist."
    )
    assert any(s.expected_category is FailureCategory.RISK_BLOCKED for s in scenarios), (
        "The fraud scenario must be offerable; it is the demo's hard-refusal case."
    )


def test_every_offered_scenario_can_actually_be_simulated(client, customer: Customer) -> None:
    """
    Each key the picker offers must produce a payment, not a 404.

    Asserting the sets match is not quite enough: a key could exist in the
    catalogue and still be rejected by the simulate endpoint. This walks the
    whole menu, which is cheap and removes the doubt.
    """
    scenarios = client.get("/api/payments/failure-scenarios").json()
    assert scenarios, "the catalogue should not be empty"

    for scenario in scenarios:
        created = client.post(
            "/api/payments/simulate-failure", json={"scenario": scenario["key"]}
        )
        assert created.status_code == 201, (
            f"Scenario '{scenario['key']}' is offered but cannot be simulated: "
            f"{created.status_code} {created.text}"
        )
        payment = PaymentOut.model_validate(created.json())
        assert payment.status is PaymentStatus.FAILED
        assert payment.method.value == scenario["method"], (
            "The simulated payment should use the scenario's own instrument."
        )


def test_the_public_checkout_payload_carries_no_contact_details_in_simulated_mode(
    client, customer: Customer
) -> None:
    """
    Data minimisation on the only endpoint a stranger is meant to reach.

    The customer opens `/checkout/[caseId]` from a link that arrived by email or
    SMS -- channels that get forwarded, screenshotted and read on shared devices.
    Razorpay's hosted Checkout genuinely needs name/email/contact as `prefill`,
    but only when Razorpay is the gateway. In simulated mode the page renders none
    of them, so sending them put three pieces of personal data on the most exposed
    surface in the product for no benefit at all.

    This pins the behaviour so a later refactor cannot quietly put them back.
    """
    created = client.post("/api/payments/simulate-failure", json={"scenario": "incorrect_otp"})
    payment = PaymentOut.model_validate(created.json())

    analysed = client.post(
        f"/api/recovery/payments/{payment.id}/analyze", json={"force_rule_based": True}
    )
    case = RecoveryCaseOut.model_validate(analysed.json())
    if case.status is not RecoveryStatus.AWAITING_APPROVAL:
        pytest.skip(f"scenario routed to {case.status.value}; not an approvable case")

    client.post(f"/api/recovery/cases/{case.id}/approve", json={"approved_by": "Tester"})
    session = CheckoutSessionOut.model_validate(
        client.get(f"/api/recovery/cases/{case.id}/checkout").json()
    )

    assert session.gateway_mode is GatewayMode.SIMULATED
    assert session.customer_name == ""
    assert session.customer_email == ""
    assert session.customer_phone == ""
    # The things the page actually renders must still be there.
    assert session.amount_paise == case.amount_paise
    assert session.description
