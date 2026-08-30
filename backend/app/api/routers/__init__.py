"""
Router registry.

``app.main`` includes ``ALL_ROUTERS`` in order rather than naming each router
individually. Adding an endpoint group therefore means touching one tuple, and
``main.py`` never grows a wall of ``include_router`` calls that has to be kept in
sync by hand.

The order below is the order the groups appear in the ``/docs`` sidebar, and it
follows the story a reviewer should read in: is it up, what payments failed, what
did the agent decide, what does the ledger prove, what policy was in force, and
finally how real events get in.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routers.audit import router as audit_router
from app.api.routers.health import router as health_router
from app.api.routers.metrics import router as metrics_router
from app.api.routers.payments import customers_router, router as payments_router
from app.api.routers.policy import router as policy_router
from app.api.routers.recovery import router as recovery_router
from app.api.routers.webhooks import router as webhooks_router

#: Every router the application serves.
ALL_ROUTERS: tuple[APIRouter, ...] = (
    health_router,
    metrics_router,
    payments_router,
    customers_router,
    recovery_router,
    audit_router,
    policy_router,
    webhooks_router,
)

__all__ = ["ALL_ROUTERS"]
