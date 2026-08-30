"""
The ASGI application: composition root, error contract and start-up banner.

This module wires the pieces together and does nothing else. It owns four
responsibilities that genuinely belong at the top of the process:

*   **Start-up.** Logging is configured and the schema is created inside an
    ``asynccontextmanager`` lifespan. The older ``@app.on_event("startup")``
    decorator is deprecated in current Starlette and has no matching shutdown
    guarantee, so it is not used here.

*   **One error shape.** Every non-2xx response the application produces is an
    ``ErrorOut``. A client that has to branch between ``{"error": ...}`` and
    FastAPI's default ``{"detail": ...}`` will eventually mis-render one of them,
    and the one it mis-renders will be the guardrail refusal that mattered.

*   **Honesty on boot.** The banner states, in plain words, whether the agent is
    an LLM or the rule engine, whether the gateway is Razorpay Test Mode or the
    simulator, and whether the ML model is trained or heuristic. It is the first
    thing anyone sees and it must not flatter the demo.

*   **A single command to run.** ``python -m app.main`` from ``backend/`` starts
    the server with no shell, no Makefile and no ``uvicorn`` invocation to
    remember -- which is what makes the project runnable on a stock Windows
    laptop.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import APP_VERSION, SettingsDep, build_system_status
from app.api.routers import ALL_ROUTERS
from app.config import Settings, get_settings
from app.db.base import init_db
from app.domain.enums import AgentMode, GatewayMode
from sqlalchemy.exc import IntegrityError

from app.domain.errors import RecoverAIError
from app.domain.schemas import ErrorOut, SystemStatusOut

logger = logging.getLogger("recoverai")

APP_DESCRIPTION = """
**AI decides. Guardrails control. Razorpay executes. Audit trail proves.**

RecoverAI turns a failed Razorpay payment into a reviewed, bounded, fully
auditable recovery attempt.

* The agent classifies the failure and recommends a strategy. It has **no tool
  that moves money** and **no way to set an amount**.
* Thirteen guardrails decide whether a recovery may be offered at all, and are
  re-evaluated at approval time rather than only at proposal time.
* A named human approves every rupee.
* Every step is appended to a hash-chained ledger you can verify at
  `GET /api/audit/verify`.

The service boots with **zero credentials**: with no Gemini key it plans with a
deterministic rule engine, and with no Razorpay keys it executes against an
in-process simulator. Both degradations are reported by `GET /api/status` and
shown in the UI. Call that endpoint first -- it tells you which modes you are
actually looking at.
"""


def configure_logging(settings: Settings) -> None:
    """
    Set up root logging from ``settings.log_level``.

    Args:
        settings: supplies the level; an unrecognised value falls back to INFO
            rather than crashing the process over a typo in ``.env``.

    ``force=True`` is deliberately **not** passed. Uvicorn installs its own
    handlers for the access log before the lifespan runs, and forcing a
    reconfiguration here would tear them out -- the request log would silently
    vanish, which is a confusing thing to debug.
    """
    level = getattr(logging, settings.log_level.strip().upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _log_startup_banner(settings: Settings) -> None:
    """
    Print which subsystems are live and which are degraded.

    Reads :func:`build_system_status`, the same function behind
    ``GET /api/status``, so the log and the API can never disagree about what is
    running. Warnings from that report -- including the interpreter-version check
    -- are emitted at WARNING level so they survive a higher log threshold.
    """
    status = build_system_status(settings)

    if status.agent_mode is AgentMode.LLM:
        agent = f"Google Gemini ({status.gemini_model}) with function calling"
    else:
        agent = "rule-based planner (deterministic; no LLM in the loop)"

    if status.gateway_mode is GatewayMode.RAZORPAY_TEST:
        gateway = "Razorpay Test Mode (real API calls, test money)"
    else:
        gateway = "simulated gateway (in-process; no credentials, no network)"

    if status.ml_model_loaded:
        ml = f"trained model loaded (version {status.ml_model_version})"
    else:
        ml = "heuristic fallback (no trained artefact; run 'python -m app.ml.train')"

    logger.info("%s v%s starting (%s)", status.app, status.version, status.environment)
    logger.info("  agent    : %s", agent)
    logger.info("  gateway  : %s", gateway)
    logger.info("  ml model : %s", ml)
    logger.info("  database : %s", status.database)
    logger.info("  api docs : http://%s:%s/docs", settings.api_host, settings.api_port)

    for warning in status.warnings:
        logger.warning("  %s", warning)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Start-up and shutdown for the ASGI app.

    Creates the schema if it does not exist, so a fresh clone answers requests
    without a migration step. It does not seed: demo data is an explicit
    ``python -m app.db.seed``, because a server that invents rows on boot makes
    an empty database impossible to observe.
    """
    settings = get_settings()
    configure_logging(settings)
    init_db()
    _log_startup_banner(settings)
    yield
    logger.info("%s shutting down", settings.app_name)


app = FastAPI(
    title="RecoverAI API",
    description=APP_DESCRIPTION,
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS is read from configuration rather than hard-coded to localhost:3000,
# because Next.js silently moves to 3001 when 3000 is busy -- a first-run failure
# that presents as an unexplained blank dashboard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# The error contract
# ---------------------------------------------------------------------------
# Three handlers, one response shape. Anything the client can receive as a
# non-2xx body is an ErrorOut: {"error": <stable code>, "message": ..., "detail": ...}.
# A single shape means the frontend parses errors in one place, and the stable
# `error` code -- not a substring of the message -- is what it branches on.


@app.exception_handler(RecoverAIError)
async def handle_recoverai_error(request: Request, exc: RecoverAIError) -> JSONResponse:
    """
    Render any domain error using its own declared HTTP status.

    One handler covers the whole hierarchy because Starlette resolves handlers by
    walking the exception's MRO. Registering a handler per subclass would mean
    that adding a new error type silently produced an unhandled 500 until
    somebody remembered to register it here.
    """
    body = ErrorOut(
        error=exc.code,
        message=str(exc),
        detail=getattr(exc, "detail", None),
    )
    logger.warning("%s %s -> %s: %s", request.method, request.url.path, exc.code, exc)
    return JSONResponse(status_code=exc.http_status, content=jsonable_encoder(body))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Render a request-validation failure in the same envelope as a domain error.

    FastAPI's default 422 body is ``{"detail": [...]}``, a second shape the client
    would otherwise have to special-case. The per-field errors are preserved
    under ``detail`` -- they are genuinely useful -- but the top level matches
    every other error the API can return.
    """
    body = ErrorOut(
        error="validation_error",
        message="The request body or query string failed validation.",
        # jsonable_encoder because Pydantic's error entries can carry exception
        # objects in `ctx`, which json.dumps cannot serialise.
        detail={"errors": jsonable_encoder(exc.errors())},
    )
    return JSONResponse(status_code=422, content=jsonable_encoder(body))


@app.exception_handler(IntegrityError)
async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    """
    Render a lost write race in the standard envelope instead of a raw 500.

    The ledger already serialises and retries sequence allocation, and the
    idempotency key on `recovery_attempts` already makes a replayed approval a
    no-op, so reaching here means two requests genuinely collided on a unique
    constraint. That is a conflict the caller can resolve by retrying -- 409, not
    500 -- and the operator gets a sentence rather than an unstyled error page.

    Registered last of the four handlers because it is the safety net: if it is
    ever the one that fires, something above it did not do its job.
    """
    logger.warning(
        "%s %s -> integrity conflict: %s",
        request.method,
        request.url.path,
        exc.orig if exc.orig is not None else exc,
    )
    body = ErrorOut(
        error="conflict",
        message="That request conflicted with another one happening at the same "
        "time and was not applied. Nothing was charged; try again.",
        detail=None,
    )
    return JSONResponse(status_code=409, content=jsonable_encoder(body))


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """
    Render framework-level failures -- an unknown path, a wrong method -- in the
    same envelope.

    Without this, ``GET /api/paymnets`` would come back as ``{"detail": "Not
    Found"}`` and break the "one error shape" promise for exactly the requests a
    developer makes while learning the API.
    """
    body = ErrorOut(
        error="http_error",
        message=str(exc.detail),
        detail={"status_code": exc.status_code},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(body),
        headers=getattr(exc, "headers", None),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get(
    "/api/status",
    response_model=SystemStatusOut,
    tags=["health"],
    summary="Report which subsystems are live and which are degraded",
    description="The app's self-report: LLM agent vs rule-based planner, Razorpay Test Mode vs "
    "simulated gateway, trained model vs heuristic. Read the degraded modes from the typed "
    "fields (`agent_mode`, `gateway_mode`, `ml_model_loaded`); `warnings` carries only what "
    "those cannot express, such as an interpreter outside the tested range. The UI header "
    "renders this so a demo can never quietly imply it is doing something it is not.",
)
def system_status(settings: SettingsDep) -> SystemStatusOut:
    """Return the runtime self-description used by the start-up banner and the UI."""
    return build_system_status(settings)


for _router in ALL_ROUTERS:
    app.include_router(_router)


if __name__ == "__main__":
    # Guarded so that importing ``app.main`` (tests, ``uvicorn app.main:app``)
    # never starts a server as a side effect.
    #
    # Run with:  python -m app.main     (from the backend/ directory)
    #
    # The application object is passed directly rather than the "app.main:app"
    # import string. The string form is only needed for --reload, which spawns a
    # worker process that re-imports the module; passing the object keeps this to
    # a single process, which avoids the double-import and console-handle
    # quirks of process spawning on Windows.
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        app,
        host=_settings.api_host,
        port=_settings.api_port,
        log_level=_settings.log_level.strip().lower(),
    )
