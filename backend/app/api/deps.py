"""
Dependency wiring for the HTTP layer, plus the system self-description.

Two jobs live here, and they are related:

1.  **Constructing services.** Every route handler receives its collaborators
    through ``Depends`` rather than building them inline. That is what makes a
    route testable: a test can override ``get_recovery_service`` with a stub and
    exercise the HTTP contract without a database, and conversely it can
    override ``get_db`` with a transactional test session and exercise the real
    service against SQLite in memory.

2.  **Answering "what mode am I actually in?"** exactly once. The start-up
    banner in ``app.main`` and ``GET /api/status`` must never disagree, because
    the whole zero-credential story rests on the app being honest about running
    degraded. Two independent implementations of "is Gemini available?" would
    eventually drift, so both callers go through
    :func:`build_system_status`.

The two small read helpers at the bottom (``load_customer_out`` and
``list_audit_events``) exist because the service contract intentionally does not
define a customer service or an audit *reader* -- both endpoints are a single
row-read with no business rules attached. Putting the two queries here rather
than in the route modules keeps the promise that a router contains no ORM code,
without inventing a class that would have exactly one call site.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.llm import GeminiClient
from app.audit.ledger import AuditLedger
from app.config import Settings, check_python_version, get_settings
from app.db.base import get_db
from app.db.models import AuditEvent, Customer
from app.domain.enums import AgentMode, GatewayMode
from app.domain.errors import NotFoundError
from app.domain.schemas import (
    AuditEventOut,
    CustomerOut,
    SystemStatusOut,
    paise_to_rupees,
)
from app.ml.predictor import get_predictor
from app.payments.gateway import get_gateway
from app.policy.engine import PolicyEngine
from app.services.metrics_service import MetricsService
from app.services.payment_service import PaymentService
from app.services.recovery_service import RecoveryService

#: Reported by ``GET /api/status`` and stamped on the OpenAPI document. Kept as a
#: plain constant rather than read from package metadata: the project is run from
#: a checkout, not from an installed wheel, so ``importlib.metadata`` would have
#: nothing to read.
APP_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Primitive dependencies
# ---------------------------------------------------------------------------

#: One SQLAlchemy session per request, closed by ``get_db``'s ``finally`` block.
DbSession = Annotated[Session, Depends(get_db)]

#: ``get_settings`` is ``lru_cache``d, so this hands every handler the same
#: object the guardrail engine reads. Declaring it as a dependency (rather than
#: importing the module-level ``settings``) is what lets a test swap the policy
#: limits via ``app.dependency_overrides``.
SettingsDep = Annotated[Settings, Depends(get_settings)]


# ---------------------------------------------------------------------------
# Service dependencies
# ---------------------------------------------------------------------------


def get_payment_service(db: DbSession) -> PaymentService:
    """Build the payment service for this request."""
    return PaymentService(db)


PaymentServiceDep = Annotated[PaymentService, Depends(get_payment_service)]


def get_recovery_service(db: DbSession, settings: SettingsDep) -> RecoveryService:
    """
    Build the recovery service for this request.

    It takes ``settings`` as well as the session because it re-evaluates the
    guardrails at approval time, and the limits it must evaluate against are the
    ones configured right now -- not the ones frozen into the case when the plan
    was proposed.
    """
    return RecoveryService(db, settings)


RecoveryServiceDep = Annotated[RecoveryService, Depends(get_recovery_service)]


def get_metrics_service(db: DbSession) -> MetricsService:
    """Build the dashboard metrics service for this request."""
    return MetricsService(db)


MetricsServiceDep = Annotated[MetricsService, Depends(get_metrics_service)]


def get_audit_ledger(db: DbSession) -> AuditLedger:
    """Build the append-only audit ledger for this request."""
    return AuditLedger(db)


AuditLedgerDep = Annotated[AuditLedger, Depends(get_audit_ledger)]


def get_policy_engine(settings: SettingsDep) -> PolicyEngine:
    """Build the guardrail engine for this request (used by ``GET /api/policy``)."""
    return PolicyEngine(settings)


PolicyEngineDep = Annotated[PolicyEngine, Depends(get_policy_engine)]


# ---------------------------------------------------------------------------
# System self-description
# ---------------------------------------------------------------------------


def _describe_database(settings: Settings) -> str:
    """
    Render the database in a form that is safe to publish over HTTP.

    The raw DSN is not returned. Today it is a local SQLite path (which would
    leak the operator's home directory into a JSON response); for any server
    engine a DSN can carry a username and password. Naming the dialect, plus the
    SQLite filename, tells a reviewer everything they need and nothing they
    should not have.
    """
    if settings.is_sqlite:
        return f"sqlite ({Path(settings.database_url.split('///', 1)[-1]).name})"
    return settings.database_url.split("://", 1)[0]


def build_system_status(settings: Settings) -> SystemStatusOut:
    """
    Report which subsystems are live and which are running degraded.

    This is the single source of truth for the honesty claim in the README: the
    app boots with no credentials at all, and when it does, it says so in the
    start-up log, in ``GET /api/status`` and (via that endpoint) in the UI
    header. All three read this function.

    Args:
        settings: the active configuration.

    Returns:
        A fully populated :class:`SystemStatusOut`.

        ``warnings`` carries only what the typed fields cannot already express.
        A degraded reasoning engine is ``agent_mode``, a simulated gateway is
        ``gateway_mode``, an untrained model is ``ml_model_loaded`` -- and each of
        those has purpose-written copy in the UI and its own line in the start-up
        log. Repeating them here as prose made every consumer say the same thing
        twice: the header banner rendered its own sentence and then the server's
        near-identical one underneath it. What belongs in this list is the
        residue: conditions with no typed representation, such as an interpreter
        outside the tested range, or a Gemini key that is present but unusable --
        a state that is genuinely not implied by ``agent_mode`` alone.

    Raises:
        Nothing. Each probe below is designed to answer "no" rather than throw:
        a missing Gemini SDK, absent Razorpay keys and an untrained model are
        all supported configurations, not errors.
    """
    warnings: list[str] = []

    # -- Reasoning engine ------------------------------------------------
    # Asking the client rather than only checking for a key: a key that is set
    # while ``google-genai`` is not importable still means the rule-based
    # planner will run, and a status endpoint that claimed "llm" there would be
    # lying about which code produced every decision in the audit trail.
    gemini = GeminiClient(settings)
    if gemini.available:
        agent_mode = AgentMode.LLM
        gemini_model: str | None = settings.gemini_model
    else:
        agent_mode = AgentMode.RULE_BASED
        gemini_model = None
        if settings.gemini_enabled:
            # Worth saying out loud: `agent_mode` alone would suggest "no key
            # configured", when in fact a key IS configured and something about
            # the SDK is broken. That is a misconfiguration somebody should fix,
            # not a supported degraded mode.
            warnings.append(
                "GEMINI_API_KEY is set but the google-genai SDK could not be used; "
                "plans are produced by the deterministic rule-based planner."
            )

    # -- Execution engine -------------------------------------------------
    # No warning: `gateway_mode` already says SIMULATED, and both the UI banner
    # and the start-up log write their own sentence from it.
    gateway_mode = get_gateway(settings).mode

    # -- Propensity model --------------------------------------------------
    # No warning: `ml_model_loaded` carries this. The remediation ("run the
    # trainer") is in the UI copy and in the start-up log's `ml model` line.
    predictor = get_predictor()

    # -- Interpreter -------------------------------------------------------
    # check_python_version() warns rather than refuses, so its message belongs in
    # the same list as the other degradations.
    python_warning = check_python_version()
    if python_warning:
        warnings.append(python_warning)

    return SystemStatusOut(
        app=settings.app_name,
        version=APP_VERSION,
        environment=settings.app_env,
        agent_mode=agent_mode,
        gemini_model=gemini_model,
        gateway_mode=gateway_mode,
        ml_model_loaded=predictor.is_loaded,
        ml_model_version=predictor.model_version,
        database=_describe_database(settings),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Trivial reads the service contract does not cover
# ---------------------------------------------------------------------------


def load_customer_out(db: Session, customer_id: str) -> CustomerOut:
    """
    Fetch one customer and render it for the API.

    Args:
        db: active session.
        customer_id: primary key, e.g. ``cust_1a2b3c...``.

    Returns:
        The customer as a :class:`CustomerOut`.

    Raises:
        NotFoundError: no such customer (rendered as a 404 ``ErrorOut``).

    ``lifetime_value_rupees`` has to be filled in explicitly. It is a
    presentation field with no ORM column behind it, so ``model_validate`` would
    silently fall back to its ``0.0`` default and the UI would report every
    customer as worth nothing -- a wrong number is worse than a missing one.
    """
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError(f"Customer not found: {customer_id}")
    return CustomerOut.model_validate(customer).model_copy(
        update={"lifetime_value_rupees": paise_to_rupees(customer.lifetime_value_paise)}
    )


def list_audit_events(
    db: Session,
    *,
    case_id: str | None = None,
    limit: int = 100,
) -> list[AuditEventOut]:
    """
    Read a page of the audit ledger.

    Args:
        db: active session.
        case_id: when given, restrict to events belonging to that recovery case.
        limit: maximum number of events to return.

    Returns:
        Events ordered newest-first.

    Newest-first is the opposite of the order the ledger *verifies* in, and that
    is intentional: verification walks the chain from genesis forward because
    that is how the hashes link, while an operator opening the audit page wants
    the most recent thing that happened at the top of the screen.
    """
    stmt = select(AuditEvent)
    if case_id:
        stmt = stmt.where(AuditEvent.case_id == case_id)
    stmt = stmt.order_by(AuditEvent.sequence.desc()).limit(limit)
    return [AuditEventOut.model_validate(row) for row in db.execute(stmt).scalars()]
