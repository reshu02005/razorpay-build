"""
The recovery lifecycle: analyse, review, approve or reject, collect, settle.

This is the product. A failed payment is analysed by the agent, the guardrail
engine decides whether a recovery may even be offered, a human approves it, the
gateway creates the order, and the customer pays through a link. Each of those
steps is one route below and one method on ``RecoveryService``.

The handlers are deliberately trivial -- parse the path parameter, hand the
request body to the service, return what comes back. Nothing here decides
anything. In particular, ``approve`` does not check whether approval is allowed:
the guardrails are re-evaluated inside ``RecoveryService.approve`` against live
state, because minutes or hours pass between a plan being proposed and a human
clicking the button, and in that time the daily budget moves, attempts
accumulate and links expire. A check performed here would be checking stale
facts, and would also be unreachable from the tests that matter.

Every route declares ``response_model=`` and ``summary=`` so ``/docs`` reads as
the API reference for this service.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import RecoveryServiceDep
from app.domain.schemas import (
    AgentToolCallOut,
    AnalyzeIn,
    ApproveIn,
    CheckoutSessionOut,
    ErrorOut,
    MarkAttemptFailedIn,
    RecoveryCaseOut,
    RecoveryCaseSummaryOut,
    RejectIn,
    VerifyPaymentIn,
)

router = APIRouter(
    prefix="/api/recovery",
    tags=["recovery"],
    responses={
        403: {"model": ErrorOut, "description": "A guardrail denied the action, or approval is required"},
        404: {"model": ErrorOut, "description": "No such payment or case"},
        409: {"model": ErrorOut, "description": "Illegal state transition, or a case already exists"},
    },
)


@router.post(
    "/payments/{payment_id}/analyze",
    response_model=RecoveryCaseOut,
    status_code=status.HTTP_201_CREATED,
    summary="Analyse a failed payment and open a recovery case",
    description="Runs the agent (Gemini with function calling, or the deterministic planner "
    "when no key is configured), scores recovery propensity with the ML model, evaluates all "
    "13 guardrails, and persists one recovery case with the full reasoning trace. Creates no "
    "order and moves no money. Set `force_rule_based` to demonstrate the degraded path.",
)
def analyze_payment(
    payment_id: str,
    body: AnalyzeIn,
    service: RecoveryServiceDep,
) -> RecoveryCaseOut:
    """Open a recovery case for one failed payment."""
    return service.analyze_payment(payment_id, body)


@router.get(
    "/cases",
    response_model=list[RecoveryCaseSummaryOut],
    summary="List recovery cases",
    description="Light rows for list views -- no agent trace, no guardrail evaluations. "
    "Fetch a single case for those.",
)
def list_cases(
    service: RecoveryServiceDep,
    status_filter: str = Query(
        default="all",
        alias="status",
        description="Any `RecoveryStatus` value (e.g. `awaiting_approval`, `recovered`), or "
        "`all` to disable filtering.",
    ),
    limit: int = Query(default=100, ge=1, le=500, description="Maximum rows to return."),
) -> list[RecoveryCaseSummaryOut]:
    """Return a page of recovery cases, optionally narrowed to one status."""
    return service.list_cases(status=status_filter, limit=limit)


@router.get(
    "/cases/{case_id}",
    response_model=RecoveryCaseOut,
    summary="Get one recovery case",
    description="Everything the approval screen needs in one response: classification, "
    "propensity score, per-rule guardrail results, the policy snapshot in force at decision "
    "time, and precomputed `can_approve` / `can_reject` flags so the UI never re-implements "
    "policy.",
)
def get_case(case_id: str, service: RecoveryServiceDep) -> RecoveryCaseOut:
    """Return the full detail of one recovery case."""
    return service.to_out(service.get_case(case_id))


@router.get(
    "/cases/{case_id}/trace",
    response_model=list[AgentToolCallOut],
    summary="Get the agent's reasoning trace",
    description="One row per tool call, in order, with arguments, result, latency and the "
    "tool's security capability. The capability column is what lets a reader confirm from the "
    "data -- not just from the code -- that no money-moving tool was ever in the loop.",
)
def get_trace(case_id: str, service: RecoveryServiceDep) -> list[AgentToolCallOut]:
    """Return the ordered tool calls that produced this case's plan."""
    return service.trace(case_id)


@router.post(
    "/cases/{case_id}/approve",
    response_model=RecoveryCaseOut,
    summary="Approve a recovery and create the Razorpay order",
    description="Re-evaluates every guardrail against live state before acting, then creates "
    "the order and the customer link. Returns 403 with `guardrail_denied` if a rule now "
    "refuses, and 409 if the case is not awaiting approval. This is the only path in the "
    "system that causes money to move, and it requires a named human operator.",
)
def approve_case(
    case_id: str,
    body: ApproveIn,
    service: RecoveryServiceDep,
) -> RecoveryCaseOut:
    """Approve a case; the service re-runs the guardrails before executing."""
    return service.approve(case_id, body)


@router.post(
    "/cases/{case_id}/reject",
    response_model=RecoveryCaseOut,
    summary="Reject a recovery",
    description="Terminal. The operator identity and the reason are recorded in the audit "
    "ledger, because 'nobody chased this customer' is as important to be able to prove as "
    "'somebody did'.",
)
def reject_case(
    case_id: str,
    body: RejectIn,
    service: RecoveryServiceDep,
) -> RecoveryCaseOut:
    """Reject a case with a recorded reason."""
    return service.reject(case_id, body)


@router.get(
    "/cases/{case_id}/checkout",
    response_model=CheckoutSessionOut,
    summary="Get the customer-facing checkout session",
    description="Everything the recovery page needs to open Razorpay Checkout. Returns the "
    "publishable key id only -- the secret never leaves the server. In simulated mode the key "
    "is `null` and the page uses the simulate endpoint instead.",
)
def get_checkout_session(case_id: str, service: RecoveryServiceDep) -> CheckoutSessionOut:
    """Return the checkout session for an approved, order-created case."""
    return service.checkout_session(case_id)


@router.post(
    "/cases/{case_id}/verify",
    response_model=RecoveryCaseOut,
    summary="Verify a Razorpay Checkout callback and settle the case",
    description="The signature is recomputed server-side with HMAC-SHA256 before anything is "
    "marked recovered. A client-side 'payment succeeded' callback is a claim, not proof: "
    "trusting it would let anyone mark any case recovered with curl.",
)
def verify_payment(
    case_id: str,
    body: VerifyPaymentIn,
    service: RecoveryServiceDep,
) -> RecoveryCaseOut:
    """Verify the checkout signature and move the case to recovered."""
    return service.verify_payment(case_id, body)


@router.post(
    "/cases/{case_id}/simulate-checkout",
    response_model=RecoveryCaseOut,
    summary="Simulate the customer paying (simulated gateway only)",
    description="Produces a genuine HMAC-SHA256 signature over `order_id|payment_id` and runs "
    "it through the same verification path as a real callback, so the demo exercises the real "
    "code rather than a shortcut. Rejected when real Razorpay credentials are configured.",
)
def simulate_checkout(
    case_id: str,
    service: RecoveryServiceDep,
    succeed: bool = Query(
        default=True,
        description="`true` simulates a successful payment, `false` a customer-side failure.",
    ),
) -> RecoveryCaseOut:
    """Drive the simulated gateway through a success or failure for this case."""
    return service.simulate_checkout(case_id, succeed=succeed)


@router.post(
    "/cases/{case_id}/mark-failed",
    response_model=RecoveryCaseOut,
    summary="Mark the open attempt as failed (demo helper)",
    description="Forces the current attempt to fail so the failure branch of the state machine "
    "can be demonstrated. Whether the case may then be re-proposed is decided by the "
    "max-attempts guardrail, not by this endpoint.",
)
def mark_attempt_failed(
    case_id: str,
    body: MarkAttemptFailedIn,
    service: RecoveryServiceDep,
) -> RecoveryCaseOut:
    """Fail the open recovery attempt with a recorded reason."""
    return service.mark_attempt_failed(case_id, body)
