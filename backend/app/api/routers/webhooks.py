"""
Inbound Razorpay webhooks.

Three rules govern this file, and all three are about not being lied to and not
causing a retry storm.

1.  **The raw bytes are read before anything else.** Razorpay signs the exact
    body it transmitted. Parsing to a dict and re-serialising produces different
    bytes -- key order, whitespace and unicode escaping all change -- so the HMAC
    would never match. ``await request.body()`` first, verify, *then* parse.

2.  **An event we do not handle still gets a 200.** Razorpay retries any non-2xx
    response with backoff. Returning an error for, say, ``payment.captured``
    would schedule repeated redeliveries of an event we deliberately ignore, and
    would eventually look like an outage on their dashboard. 400 is reserved for
    the one case where a retry is genuinely the right answer to demand: a body
    whose signature does not verify.

3.  **Every delivery is written to the audit ledger**, handled or not. "We never
    received it" and "we received it and chose not to act" are different
    statements, and only one of them is defensible after the fact.

The route declares ``response_model=`` and ``summary=`` so ``/docs`` reads as the
API reference for this service.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Header, Request

from app.api.deps import AuditLedgerDep, DbSession, PaymentServiceDep, SettingsDep
from app.domain.enums import ActorType, AuditEventType
from app.domain.errors import SignatureVerificationError
from app.domain.schemas import ErrorOut
from app.payments.webhook import parse_payment_failed, verify_webhook_signature

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/webhooks",
    tags=["webhooks"],
    responses={400: {"model": ErrorOut, "description": "Signature verification failed"}},
)

#: The only event this system acts on. Everything else is acknowledged and logged.
HANDLED_EVENT = "payment.failed"


@router.post(
    "/razorpay",
    response_model=dict[str, bool],
    summary="Receive a Razorpay webhook",
    description="Verifies `X-Razorpay-Signature` against the raw request body, then ingests "
    "`payment.failed` events as new failed payments. Any other event type is acknowledged with "
    "200 and recorded in the audit ledger without being acted on. Returns 400 only when the "
    "signature does not verify.",
)
async def razorpay_webhook(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    payments: PaymentServiceDep,
    ledger: AuditLedgerDep,
    signature: str = Header(
        default="",
        alias="X-Razorpay-Signature",
        description="HMAC-SHA256 of the raw body, keyed with the webhook secret.",
    ),
) -> dict[str, bool]:
    """
    Accept one webhook delivery.

    Args:
        request: used only to read the raw, unparsed body.
        db: session; committed once at the end so the ingested payment and its
            audit event land together or not at all.
        settings: supplies the webhook secret.
        payments: ingests the normalised failure.
        ledger: records the delivery.
        signature: the ``X-Razorpay-Signature`` header.

    Returns:
        ``{"received": True}``.

    Raises:
        SignatureVerificationError: the body did not match the signature. Renders
            as a 400 ``ErrorOut``, which is the one status that should make
            Razorpay retry.
    """
    # Step 1: the bytes exactly as they arrived. Nothing may touch the body
    # before this, because the signature covers these bytes and no others.
    raw_body = await request.body()

    # Step 2: authenticate, and fail closed.
    #
    # `verify_webhook_signature` is written to reject rather than skip when the
    # secret is unset, and its docstring is emphatic about why: "no secret
    # configured" is precisely the state an attacker would hope for. An earlier
    # version of this route wrapped the whole check in `if secret:`, which
    # inverted that -- in the shipped zero-credential configuration the endpoint
    # accepted anything, so anyone who could reach the API could mint arbitrary
    # payments and customers straight into the approval queue.
    #
    # The demo affordance it was protecting is real (this endpoint is how a local
    # demo ingests a Razorpay dashboard test event without configuring a secret),
    # so it survives -- but as an explicit, greppable opt-in rather than as an
    # accident of a blank credential.
    secret = settings.razorpay_webhook_secret.strip()
    if secret:
        if not verify_webhook_signature(body=raw_body, signature=signature, secret=secret):
            raise SignatureVerificationError(
                "Razorpay webhook signature verification failed.",
                detail={"header": "X-Razorpay-Signature", "body_bytes": len(raw_body)},
            )
        signature_verified = True
    elif settings.allow_unsigned_webhooks:
        signature_verified = False
        logger.warning(
            "Accepting an UNSIGNED Razorpay webhook because ALLOW_UNSIGNED_WEBHOOKS is on. "
            "Never enable this outside local development."
        )
    else:
        raise SignatureVerificationError(
            "This endpoint rejects unsigned deliveries. Set RAZORPAY_WEBHOOK_SECRET to the "
            "secret configured in the Razorpay dashboard, or set ALLOW_UNSIGNED_WEBHOOKS=true "
            "for local testing only.",
            detail={"header": "X-Razorpay-Signature", "secret_configured": False},
        )

    # Step 3: only now is it safe to parse.
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # The signature already told us this body is authentic, so a malformed
        # payload is a sender-side defect that a redelivery of the same bytes
        # cannot fix. Acknowledge it and leave the evidence in the ledger.
        logger.warning("Razorpay webhook body was not valid JSON; acknowledged without action.")
        event = {}

    event_type = str(event.get("event", "")) if isinstance(event, dict) else ""
    handled = event_type == HANDLED_EVENT
    ingested_payment_id: str | None = None

    unprocessable: str | None = None
    if handled:
        normalised = parse_payment_failed(event)
        try:
            payment = payments.record_webhook_failure(normalised)
            ingested_payment_id = payment.id
        except ValueError as exc:
            # An event with no gateway payment id cannot be de-duplicated, and
            # Razorpay retries anything it did not see a 200 for -- so storing it
            # would create a fresh payment and a fresh placeholder customer on
            # every redelivery. Record the delivery, ingest nothing, and still
            # answer 200: a non-2xx would guarantee the retries continue.
            handled = False
            unprocessable = str(exc)
            logger.warning("Unprocessable payment.failed delivery: %s", exc)

    ledger.record(
        event_type=AuditEventType.WEBHOOK_RECEIVED,
        actor_type=ActorType.WEBHOOK,
        actor_id="razorpay",
        payment_id=ingested_payment_id,
        summary=(
            f"Razorpay webhook '{event_type or 'unknown'}' received and ingested"
            if handled
            else f"Razorpay webhook '{event_type or 'unknown'}' received; no action taken"
        ),
        payload={
            "event": event_type,
            "handled": handled,
            "signature_verified": signature_verified,
            "body_bytes": len(raw_body),
            "unprocessable": unprocessable,
        },
    )

    # The ledger flushes but never commits, so that a caller can append several
    # events inside one transaction. This route is that caller's boundary, so it
    # owns the commit: the ingested payment and its audit entry are written
    # together or not at all.
    db.commit()

    return {"received": True}
