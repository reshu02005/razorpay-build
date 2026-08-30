"""
Merchant-level numbers for the dashboard.

Both routes are pure reads that delegate straight to ``MetricsService``. The
aggregation lives there rather than here for a specific reason: the recovery-rate
denominator (failed volume that the guardrails would actually permit an attempt
on) is a policy-shaped number, and a policy-shaped number computed in a route
handler is one that no test can reach without an HTTP client.

Every route declares ``response_model=`` and ``summary=`` so ``/docs`` reads as
the API reference for this service.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import MetricsServiceDep
from app.domain.schemas import DashboardMetricsOut, FailureBreakdownItem

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get(
    "/dashboard",
    response_model=DashboardMetricsOut,
    summary="Dashboard KPIs",
    description="Volumes in both paise and rupees, case counts by status, recovery and failure "
    "rates, and today's spend against the daily recovery budget cap. An empty database reports "
    "zeroes rather than failing -- '0% of nothing' is an honest answer.",
)
def dashboard(service: MetricsServiceDep) -> DashboardMetricsOut:
    """Return the merchant-level KPI block for the landing dashboard."""
    return service.dashboard()


@router.get(
    "/failure-breakdown",
    response_model=list[FailureBreakdownItem],
    summary="Failures grouped by category",
    description="Count, volume and recovered count per failure category. This is the evidence "
    "for the project's central claim -- that the right recovery action differs by failure "
    "reason, so the reasons have to be counted separately.",
)
def failure_breakdown(service: MetricsServiceDep) -> list[FailureBreakdownItem]:
    """Return failed-payment counts and volume grouped by failure category."""
    return service.failure_breakdown()
