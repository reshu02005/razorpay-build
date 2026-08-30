"""
The service layer: every business rule and every database write in the system.

**Why this package exists.** FastAPI routers in ``app/api/routers`` are thin
translators -- they parse a request, call one method here, and render the result.
They never touch the ORM, never open a transaction and never decide anything.
Two concrete pay-offs justify the extra layer:

1.  *The flow is testable without HTTP.* ``RecoveryService.approve()`` can be
    driven straight from ``pytest`` with a session and a settings object. If the
    approval rules lived in a route handler, testing "a denied guardrail blocks
    the money" would also be testing FastAPI's routing, dependency injection and
    JSON encoding -- and when that test failed you would not know which of the
    four broke.

2.  *A business rule has exactly one home.* The expiry sweeper, the seed script
    and the REST API all move cases through the same state machine. With the
    rules in the service, all three share one implementation. With the rules in
    the router, the sweeper would need its own copy, and two copies of a
    financial rule is two policies.

**Transaction ownership.** The service owns the unit of work. The audit ledger
and the agent orchestrator both *flush* and neither *commits*; each public
service method commits exactly once, at the end. That is what makes a state
change and its audit entry atomic: either the case moved and the ledger recorded
it, or neither happened. A ledger that can disagree with the data it describes is
not an audit trail, it is a log file.

The three services split by aggregate rather than by endpoint:

*   ``PaymentService``  -- the ``payments`` and ``customers`` tables.
*   ``RecoveryService`` -- the recovery case lifecycle, from analysis to money.
*   ``MetricsService``  -- read-only aggregation for the dashboard.
"""

from __future__ import annotations

from app.services.metrics_service import MetricsService
from app.services.payment_service import PaymentService
from app.services.recovery_service import RecoveryService

__all__ = ["MetricsService", "PaymentService", "RecoveryService"]
