"""
Liveness probe.

``GET /api/health`` answers exactly one question -- "is the process up and
serving?" -- and deliberately touches nothing else. It does not open a database
session, call the gateway or load the model.

That restraint is the point. A health check that queries its dependencies
conflates two different questions: "should the process be restarted?" and "is
the system fully functional?". Answering the second one here would make the
endpoint report unhealthy while the app is happily running in its documented
degraded mode with no credentials. ``GET /api/status`` (in ``app.main``) answers
the second question, in detail, and is the one a human should read.

Every route in this package declares ``response_model=`` and ``summary=`` so the
generated OpenAPI page at ``/docs`` is usable as the API reference; that page is
how a reviewer is expected to explore the backend.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["health"])


@router.get(
    "/health",
    response_model=dict[str, str],
    summary="Liveness probe",
    description="Returns `{\"status\": \"ok\"}` if the process is serving requests. "
    "Performs no I/O. For subsystem detail (agent, gateway, model) call `/api/status`.",
)
def health() -> dict[str, str]:
    """Report that the process is alive."""
    return {"status": "ok"}
