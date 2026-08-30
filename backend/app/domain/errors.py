"""
The application's exception hierarchy.

Why a typed hierarchy instead of raising ``fastapi.HTTPException`` directly from
the service layer?

1.  **Services stay framework-agnostic.** ``RecoveryService`` is where the rules
    of the business live -- "you may not approve a case that is not awaiting
    approval", "a denied guardrail stops the money". None of that is an HTTP
    concept. If the service raised ``HTTPException`` it would silently become a
    web-only component: unusable from a CLI seeder, a scheduled expiry sweeper or
    a background worker, all of which exist in this codebase.

2.  **The tests stop needing a web server.** ``pytest`` can assert
    ``with pytest.raises(InvalidStateTransition)`` against a plain service call.
    Testing the same rule through an HTTP client would test FastAPI's routing as
    well as our logic, and a failure would not tell you which one broke.

3.  **One rendering, not thirty.** Because every failure is a subclass of
    ``RecoverAIError`` carrying its own ``code`` and ``http_status``, ``app/main.py``
    registers exactly *one* exception handler that renders any of them as
    ``ErrorOut``. Adding a new failure mode never means touching the API layer,
    and no endpoint can accidentally invent a different error envelope.

4.  **The frontend gets a stable contract.** ``code`` is a machine token that is
    part of the API surface (``"guardrail_denied"``), while ``message`` is prose
    that may be reworded freely. The React client branches on ``code``; it never
    string-matches the message.

The alternative considered and rejected was a single ``RecoverAIError`` carrying
an error-code string argument. It is fewer lines, but it makes
``except GuardrailDenied:`` impossible -- callers would have to compare strings
to decide how to react, which is exactly the fragility ``code`` was meant to fix
for the frontend.
"""

from __future__ import annotations

from typing import Any

from app.domain.schemas import ErrorOut


class RecoverAIError(Exception):
    """
    Base class for every failure this application raises deliberately.

    Anything that inherits from this is a *known*, explainable outcome that a
    merchant operator should be shown. Anything that does not -- a ``KeyError``,
    a driver timeout -- is a bug or an outage, and ``app/main.py`` renders those
    as an opaque 500 rather than leaking internals to the browser.

    Subclasses override the two class attributes and nothing else; there is no
    constructor to re-implement, so a new error type is three lines.

    Attributes:
        code: Stable machine-readable token, part of the public API contract.
        http_status: Status the API layer responds with.
        message: Human-readable explanation, safe to display to an operator.
        detail: Optional structured context (which rule fired, which limit was
            exceeded). Rendered into ``ErrorOut.detail`` so the UI can show
            "3 attempts vs limit 2" without parsing prose.
    """

    #: Overridden by every subclass. The base value is only reachable if someone
    #: raises ``RecoverAIError`` itself, which is treated as an internal fault.
    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        # ``super().__init__(message)`` keeps ``str(exc)`` working, which is what
        # log formatters and tracebacks print. Storing ``message`` separately as
        # well means the API layer never has to rely on ``str(exc)`` -- a habit
        # that breaks the moment someone passes a second positional argument.
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_schema(self) -> ErrorOut:
        """
        Render this exception as the wire-format error envelope.

        Returns:
            An ``ErrorOut`` carrying the machine code, the human message and any
            structured detail.

        Keeping the conversion on the exception (rather than in the API handler)
        means a subclass can enrich its own payload later without the handler
        growing an ``isinstance`` ladder.
        """
        return ErrorOut(error=self.code, message=self.message, detail=self.detail)


class NotFoundError(RecoverAIError):
    """A referenced payment, customer, case or scenario does not exist."""

    code = "not_found"
    http_status = 404


class DuplicateCaseError(RecoverAIError):
    """
    A recovery case already exists for this payment.

    409 rather than 400: the request was well-formed, it just conflicts with the
    current state of the resource. The database's ``uq_case_per_payment``
    constraint is the real guarantee; this exception is the friendly explanation
    that gets raised before the driver produces an integrity error.
    """

    code = "duplicate_case"
    http_status = 409


class InvalidStateTransition(RecoverAIError):
    """
    An attempt to move a recovery case along an edge that does not exist in
    ``ALLOWED_TRANSITIONS``.

    This is the exception that makes the state machine real rather than
    decorative. Every double-clicked approve button, every replayed webhook and
    every out-of-order gateway callback lands here instead of corrupting a case.
    """

    code = "invalid_transition"
    http_status = 409


class GuardrailDenied(RecoverAIError):
    """
    The policy engine refused the action.

    403 is deliberate: the caller is authenticated and the request is valid, but
    policy forbids it. ``detail`` carries the failing ``rule_id``s so the UI can
    highlight the exact row of the guardrail checklist that blocked the money.
    """

    code = "guardrail_denied"
    http_status = 403


class ApprovalRequired(RecoverAIError):
    """
    A money-moving step was reached without the human approval that policy
    demands. Raised when something tries to execute a case that is still only
    proposed -- the last line of defence behind the state machine.
    """

    code = "approval_required"
    http_status = 403


class GatewayError(RecoverAIError):
    """
    Razorpay (or the simulator standing in for it) failed or answered
    unusably.

    502, not 500: the fault is upstream of us. The distinction matters
    operationally -- a 500 means "fix RecoverAI", a 502 means "check the
    gateway's status page" -- and it tells the caller a retry may succeed.
    """

    code = "gateway_error"
    http_status = 502


class SignatureVerificationError(RecoverAIError):
    """
    An HMAC signature on a checkout callback or a webhook did not verify.

    400 rather than 401/403 on purpose. A bad signature means the *payload* is
    not authentic; it is not a statement about who the caller is, and answering
    401 would invite a client to retry with credentials, which cannot help.
    Treated as fatal for the request: an unverified "payment succeeded" claim is
    exactly the message an attacker would forge, so it is never acted upon.
    """

    code = "invalid_signature"
    http_status = 400


class ConfigurationError(RecoverAIError):
    """
    The application is configured in a way that cannot work.

    Reserved for genuine misconfiguration -- a live Razorpay key, a database URL
    that cannot be opened. A *missing* credential is explicitly not a
    configuration error: absent keys are a supported mode that degrades to the
    rule-based planner and the simulated gateway.
    """

    code = "configuration_error"
    http_status = 500
