"""
Payment and customer reads, plus the demo failure generator.

These routes are the entry point to the whole story: a merchant lands on the
dashboard, sees failed payments here, and clicks through to have one analysed.

The handlers do three things and nothing else -- validate the query string,
call ``PaymentService``, return the schema it produced. No ORM query, no filter
logic and no paise-to-rupees arithmetic lives in this file, because the flow
"list failed payments, pick one, analyse it" has to be exercisable in a unit
test that never constructs an HTTP request.

Every route declares ``response_model=`` and ``summary=`` so ``/docs`` reads as
the API reference for this service.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, PaymentServiceDep, load_customer_out
from app.domain.schemas import (
    FailureScenarioOut,
    CustomerOut,
    ErrorOut,
    PaymentOut,
    PaymentStatusFilter,
    SimulateFailureIn,
)

router = APIRouter(
    prefix="/api/payments",
    tags=["payments"],
    responses={404: {"model": ErrorOut, "description": "No payment with that id"}},
)

#: Customers are read-only here and exist only as context for a payment, so they
#: share this module rather than getting a package of their own for one endpoint.
customers_router = APIRouter(
    prefix="/api/customers",
    tags=["customers"],
    responses={404: {"model": ErrorOut, "description": "No customer with that id"}},
)


@router.get(
    "",
    response_model=list[PaymentOut],
    summary="List payments",
    description="Newest first. Filter with `status=failed` to get exactly the queue the "
    "recovery agent works from. `recovery_case_id` on each row tells the UI whether a "
    "failure has already been analysed.",
)
def list_payments(
    service: PaymentServiceDep,
    status_filter: PaymentStatusFilter = Query(
        default="all",
        alias="status",
        description="Payment lifecycle filter; `all` disables filtering.",
    ),
    limit: int = Query(default=100, ge=1, le=500, description="Maximum rows to return."),
    offset: int = Query(default=0, ge=0, description="Rows to skip, for paging."),
) -> list[PaymentOut]:
    """Return a page of payments, optionally narrowed to one lifecycle status."""
    return service.list_payments(status=status_filter, limit=limit, offset=offset)


@router.post(
    "/simulate-failure",
    response_model=PaymentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a realistic failed payment (demo helper)",
    description="Manufactures a failed payment drawn from the seeded scenario catalogue. "
    "Exists because a reviewer cannot make a real card decline on demand. Every field is "
    "optional: an empty `{}` body produces a plausible failure.",
)
def simulate_failure(body: SimulateFailureIn, service: PaymentServiceDep) -> PaymentOut:
    """Manufacture one failed payment so the recovery flow has something to act on."""
    return service.simulate_failure(body)


@router.get(
    "/failure-scenarios",
    response_model=list[FailureScenarioOut],
    summary="List the demo failure scenarios",
    description="The catalogue `POST /simulate-failure` draws from. The console builds its "
    "scenario picker from this rather than from a hard-coded list, so the two cannot drift.",
)
def list_failure_scenarios(service: PaymentServiceDep) -> list[FailureScenarioOut]:
    """Return every scenario key the simulate endpoint accepts."""
    return service.list_scenarios()


# Declared BEFORE the `/{payment_id}` route on purpose. FastAPI matches routes in
# declaration order, so a literal path registered after a same-shaped parameter
# route is unreachable -- `/failure-scenarios` would be swallowed as a payment id
# and answered with a 404 for payment "failure-scenarios".
@router.get(
    "/{payment_id}",
    response_model=PaymentOut,
    summary="Get one payment",
    description="Includes the verbatim gateway failure fields (`error_code`, `error_reason`, "
    "`error_description`) that the agent's taxonomy classifies.",
)
def get_payment(payment_id: str, service: PaymentServiceDep) -> PaymentOut:
    """Return one payment, including its customer and raw failure detail."""
    # ``get_payment`` returns the ORM row (the services layer needs it internally);
    # the router is the boundary that converts it to the wire schema.
    return service.to_out(service.get_payment(payment_id))


@customers_router.get(
    "/{customer_id}",
    response_model=CustomerOut,
    summary="Get one customer",
    description="Includes the denormalised history counters the propensity model reads: "
    "`prior_success_rate` is 0.5 for a customer with no payments yet, because 'no data' is "
    "not the same as 'always fails'.",
)
def get_customer(customer_id: str, db: DbSession) -> CustomerOut:
    """Return one customer with their payment-history counters."""
    return load_customer_out(db, customer_id)
