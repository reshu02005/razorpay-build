"""
Reads and writes for payments and the customers who make them.

This module owns the ``payments`` table. Everything that creates a payment row
goes through here -- the demo failure generator, the Razorpay webhook, and (via
``RecoveryService``) the successful recovery capture -- so there is one place
where a payment's shape, its customer counters and its audit entry are kept
consistent with each other.

It also hosts two small time helpers used by all three services. They live in
this module rather than in ``app/services/__init__.py`` so that importing a
single service never has to execute the whole package, and rather than being
duplicated per service because a date boundary that is defined twice eventually
gets defined differently -- one copy using local midnight is all it takes to make
the daily budget guardrail wrong for eight months of the year.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.ledger import AuditLedger
from app.config import get_settings
from app.db.models import utc_day_start, Customer, Payment, RecoveryCase
from app.domain.enums import ActorType, AuditEventType, PaymentMethod, PaymentStatus
from app.domain.errors import ConfigurationError, NotFoundError
from app.domain.schemas import (
    FailureScenarioOut,
    CustomerOut,
    PaymentOut,
    SimulateFailureIn,
    paise_to_rupees,
)

# The scenario catalogue powers the demo's "simulate a failure" button. The
# import is guarded because that button is a convenience, not the product: if the
# catalogue is unavailable the payments API, the dashboard and the recovery flow
# must all still serve. The failure is reported at the call site as a
# ConfigurationError -- an honest 500 on one endpoint -- instead of taking the
# whole application down at import time.
from app.db.scenarios import SCENARIOS, FailureScenario

logger = logging.getLogger(__name__)

#: Names the catalogue module may use for its collection of scenarios. Reading
#: the catalogue by field name rather than by container type lets the data and
#: the consumer evolve independently: adding a scenario is a data edit, never a
#: code edit here.

#: Keys that may carry a scenario's identifier.

#: Range for a generated order value, in whole rupees. Chosen to straddle the
#: Rs 10,000 high-value review threshold so a demo run naturally produces both
#: ordinary cases and ones where R5_HIGH_VALUE_REVIEW fires.
_DEMO_AMOUNT_MIN_RUPEES = 299
_DEMO_AMOUNT_MAX_RUPEES = 14_999


# ---------------------------------------------------------------------------
# Time helpers, shared across the service layer
# ---------------------------------------------------------------------------


def as_utc(moment: datetime) -> datetime:
    """
    Stamp a timestamp read back from the database as UTC.

    Args:
        moment: A timestamp, aware or naive.

    Returns:
        The same instant, guaranteed timezone-aware in UTC.

    SQLite has no timezone type. Columns declared ``DateTime(timezone=True)`` are
    written from ``models.utcnow()`` as aware UTC but come back **naive**, and
    comparing a naive value with an aware one raises ``TypeError`` -- which would
    surface as a 500 on whichever endpoint happened to touch the older row first.
    Because the only writer in this system is ``utcnow()``, a naive value read
    back can only ever be UTC, so it is safe to say so explicitly.

    The alternative was to call ``astimezone()`` directly. That is a trap: on a
    naive value Python assumes *local* time and silently shifts the instant by
    the machine's UTC offset, so timestamps would render five and a half hours
    off on the Indian laptop this is built on and correctly on a UTC server.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Response builders shared with the recovery service
# ---------------------------------------------------------------------------


def customer_to_out(customer: Customer) -> CustomerOut:
    """
    Render a customer row as its wire schema.

    Args:
        customer: The ORM row.

    Returns:
        A ``CustomerOut`` with the derived success rate and rupee value filled in.

    Built field by field rather than with ``model_validate`` because two of the
    fields are not columns: ``prior_success_rate`` is a Python property and
    ``lifetime_value_rupees`` is the paise-to-rupees conversion. Doing it here
    keeps that conversion at the API edge, which is the only place rupees are
    allowed to exist.
    """
    return CustomerOut(
        id=customer.id,
        name=customer.name,
        email=customer.email,
        phone=customer.phone,
        risk_flagged=customer.risk_flagged,
        total_payments=customer.total_payments,
        successful_payments=customer.successful_payments,
        # Rounded for presentation only. The unrounded property is what the ML
        # feature row uses; a display value must never become a model input.
        prior_success_rate=round(customer.prior_success_rate, 4),
        lifetime_value_paise=customer.lifetime_value_paise,
        lifetime_value_rupees=paise_to_rupees(customer.lifetime_value_paise),
    )


def coerce_method(raw: str | PaymentMethod | None) -> PaymentMethod:
    """
    Map an instrument value from outside the system onto the enum.

    Args:
        raw: A method name from a webhook, a scenario file or a request body --
            or an already-decoded ``PaymentMethod``.

    Returns:
        The matching ``PaymentMethod``, or ``UNKNOWN`` when it is not one we model.

    This is a boundary coercion, applied once, where untrusted text enters. It is
    not a silent fallback: ``UNKNOWN`` is a real member of the enum with its own
    entry in the recovery playbook, so an unrecognised instrument stays visible
    all the way to the operator instead of being guessed at as "card".

    The early return for an existing enum member is load-bearing, not defensive
    padding. ``PaymentMethod`` mixes in ``str``, but ``Enum`` still supplies
    ``__str__``, so ``str(PaymentMethod.UPI)`` is ``"PaymentMethod.UPI"`` and not
    ``"upi"``. Feeding that through the lookup below raises ``ValueError`` and
    lands on ``UNKNOWN`` -- which is exactly what happened when the scenario
    catalogue (whose ``method`` field is already typed) was passed straight in:
    every simulated failure was recorded as an unknown instrument, and because
    the miss is logged at INFO rather than raised, nothing failed loudly. A
    boundary coercion has to be idempotent, or callers end up having to know
    which representation they hold before they can normalise it.
    """
    if isinstance(raw, PaymentMethod):
        return raw
    if not raw:
        return PaymentMethod.UNKNOWN
    try:
        return PaymentMethod(str(raw).strip().lower())
    except ValueError:
        logger.info("Unrecognised payment method '%s'; recording as unknown.", raw)
        return PaymentMethod.UNKNOWN


# ---------------------------------------------------------------------------
# Scenario catalogue access
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PaymentService:
    """
    Payment reads, the demo failure generator, and webhook ingestion.

    Holds a session for the length of one unit of work. Every public method that
    writes commits exactly once, at the end, so a payment row and the audit event
    describing it are persisted together or not at all.
    """

    def __init__(self, db: Session) -> None:
        """
        Args:
            db: The request-scoped session. The service commits it; nothing below
                this layer does.
        """
        self.db = db
        self.ledger = AuditLedger(db)

    # -- Reads ------------------------------------------------------------

    def list_payments(self, *, status: str = "all", limit: int = 100, offset: int = 0) -> list[PaymentOut]:
        """
        Return a page of payments, newest first.

        Args:
            status: A ``PaymentStatus`` value, or ``"all"`` for no filter.
            limit: Maximum rows to return.
            offset: Rows to skip, for paging.

        Returns:
            The page, rendered as ``PaymentOut``.
        """
        stmt = select(Payment).order_by(Payment.created_at.desc()).limit(limit).offset(offset)
        if status != "all":
            stmt = stmt.where(Payment.status == status)
        payments = list(self.db.execute(stmt).scalars())

        # One query for the whole page, not one per row. Rendering a payment
        # needs the id of its recovery case so the table can show "Analyse" or
        # "View case"; looking that up inside to_out() would issue a query per
        # row -- the classic N+1 -- turning a 100-row page into 101 round trips.
        case_ids = self._case_ids_for(payment.id for payment in payments)
        return [self._to_out(payment, case_ids.get(payment.id)) for payment in payments]

    def get_payment(self, payment_id: str) -> Payment:
        """
        Load one payment.

        Args:
            payment_id: The payment's id.

        Returns:
            The ORM row.

        Raises:
            NotFoundError: When no such payment exists.

        Returns the ORM object rather than a schema because the recovery service
        needs the live row -- to read the customer relationship and to be certain
        the amount it copies came from the database, not from a response model
        that something could have rebuilt along the way.
        """
        payment = self.db.get(Payment, payment_id)
        if payment is None:
            raise NotFoundError(f"No payment with id '{payment_id}'.", detail={"payment_id": payment_id})
        return payment

    def to_out(self, payment: Payment) -> PaymentOut:
        """
        Render one payment as its wire schema.

        Args:
            payment: The ORM row.

        Returns:
            A ``PaymentOut`` with rupees, the nested customer and the recovery
            case id populated.
        """
        return self._to_out(payment, self._case_ids_for([payment.id]).get(payment.id))

    # -- Writes -----------------------------------------------------------

    def list_scenarios(self) -> list[FailureScenarioOut]:
        """
        Return the demo failure catalogue, ordered for a human reading a menu.

        Returns:
            Every scenario, most-recoverable categories first so that the
            interesting refusals (fraud, unclassifiable) sit together at the end
            rather than scattered through the list.
        """
        return [
            FailureScenarioOut(
                key=scenario.key,
                label=scenario.label,
                method=scenario.method,
                expected_category=scenario.expected_category,
                error_reason=scenario.error_reason,
                error_description=scenario.error_description,
            )
            for scenario in sorted(
                SCENARIOS.values(),
                key=lambda s: (not s.expected_category.is_recoverable, s.expected_category.value, s.key),
            )
        ]

    def simulate_failure(self, body: SimulateFailureIn) -> PaymentOut:
        """
        Manufacture a realistic failed payment from the scenario catalogue.

        Args:
            body: Optional overrides. Every field may be omitted; omitted fields
                are drawn from a randomly chosen catalogue scenario.

        Returns:
            The created payment.

        Raises:
            NotFoundError: When a named scenario or customer does not exist.
            ConfigurationError: When the catalogue or the customer table is empty.

        A reviewer cannot make a real card decline on demand, so this endpoint
        stands in for one. It is not a shortcut around the pipeline: the row it
        writes is an ordinary ``Payment`` carrying the gateway's real error
        vocabulary, and the analysis, guardrail and approval path that follows is
        the same one a Razorpay webhook would trigger.
        """
        scenario = self._resolve_scenario(body.scenario)
        customer = self._resolve_customer(body.customer_id)

        # Precedence is explicit: caller override, then the scenario's own value,
        # then a generated one. The caller is the most specific source of intent
        # and therefore wins; nothing lower re-defaults what a higher layer set.
        # Precedence is explicit: caller override, then the scenario's own value,
        # then a generated one. The caller is the most specific source of intent
        # and therefore wins; nothing lower re-defaults what a higher layer set.
        #
        # A scenario describes a *failure*, not an order, so it carries no amount
        # and no product description -- those are generated. It does carry the
        # instrument, because which rail a failure happened on is part of the
        # failure.
        amount_paise = body.amount_paise or self._demo_amount()
        method = coerce_method(body.method or scenario.method)
        description = body.description or f"Simulated order ({scenario.label})"

        settings = get_settings()
        payment = Payment(
            customer_id=customer.id,
            amount_paise=int(amount_paise),
            currency=settings.currency,
            method=method.value,
            status=PaymentStatus.FAILED.value,
            description=str(description)[:255],
            error_code=scenario.error_code,
            error_source=scenario.error_source,
            error_step=scenario.error_step,
            error_reason=scenario.error_reason,
            error_description=scenario.error_description,
        )
        self.db.add(payment)

        # A failed attempt is still an attempt in this customer's history. The
        # denominator of prior_success_rate has to include it, or a customer who
        # fails constantly would keep a spotless score and the propensity model
        # would be fed a flattering lie.
        customer.total_payments += 1

        self.db.flush()
        self._audit_payment_failed(payment, actor_type=ActorType.SYSTEM, actor_id="simulator")

        self.db.commit()
        logger.info("Simulated failed payment %s for customer %s", payment.id, customer.id)
        return self.to_out(payment)

    def record_webhook_failure(self, normalised: dict) -> Payment:
        """
        Persist a ``payment.failed`` event received from Razorpay.

        Args:
            normalised: The flat dict produced by
                ``app.payments.webhook.parse_payment_failed`` -- keys
                ``razorpay_payment_id``, ``razorpay_order_id``, ``amount_paise``,
                ``currency``, ``method``, ``email``, ``contact`` and the five
                ``error_*`` fields.

        Returns:
            The payment row, whether newly created or already on file.

        Records ``PAYMENT_FAILED``. It does **not** record ``WEBHOOK_RECEIVED`` --
        the route already does, for every delivery including the ones this method
        never sees. Two writers for one event type meant an auditor counting
        `webhook_received` double-counted every ingested failure, and the two rows
        disagreed about what the payload contained.

        Does not commit. The route owns the transaction, so the ingested payment
        and the route's own audit entry are written together or not at all --
        which is what the route's comment already claimed and what every other
        write in this codebase does.

        Razorpay retries a delivery it did not see a 200 for, so the same event
        can arrive several times. Keying on the gateway's payment id makes a
        replay a no-op that returns the existing row -- without it, one retried
        webhook would create a second failed payment, and the merchant's failure
        volume would climb every time the network hiccuped.

        Raises:
            ValueError: The event carries no gateway payment id. Such a delivery
                cannot be de-duplicated (the replay key is missing) and the row it
                would create could never be traced back to a gateway payment, so
                it is refused rather than stored. Before this check, one malformed
                event that Razorpay kept retrying created a fresh payment *and* a
                fresh "Unknown customer" on every arrival.
        """
        gateway_payment_id = str(normalised.get("razorpay_payment_id") or "")
        if not gateway_payment_id:
            raise ValueError("payment.failed event carries no razorpay_payment_id")

        existing = None
        if gateway_payment_id:
            existing = self.db.execute(
                select(Payment).where(Payment.razorpay_payment_id == gateway_payment_id)
            ).scalar_one_or_none()

        if existing is not None:
            logger.info("Webhook replay for payment %s ignored; row already exists.", gateway_payment_id)
            return existing

        customer = self._customer_for_webhook(normalised)
        settings = get_settings()

        payment = Payment(
            razorpay_payment_id=gateway_payment_id or None,
            razorpay_order_id=str(normalised.get("razorpay_order_id") or "") or None,
            customer_id=customer.id,
            amount_paise=int(normalised.get("amount_paise") or 0),
            # The parser deliberately does not default the currency, so the layer
            # that owns that default -- Settings -- supplies it here.
            currency=str(normalised.get("currency") or "") or settings.currency,
            method=coerce_method(normalised.get("method")).value,
            status=PaymentStatus.FAILED.value,
            description="Payment reported failed by Razorpay",
            error_code=normalised.get("error_code"),
            error_source=normalised.get("error_source"),
            error_step=normalised.get("error_step"),
            error_reason=normalised.get("error_reason"),
            error_description=normalised.get("error_description"),
        )
        self.db.add(payment)
        customer.total_payments += 1
        self.db.flush()

        self._audit_payment_failed(payment, actor_type=ActorType.WEBHOOK, actor_id="razorpay")
        # No commit: the route is the transaction boundary for a webhook delivery.
        logger.info("Recorded webhook failure as payment %s", payment.id)
        return payment

    # -- Internals --------------------------------------------------------

    def _to_out(self, payment: Payment, recovery_case_id: str | None) -> PaymentOut:
        """Render a payment given a case id the caller has already resolved."""
        return PaymentOut(
            id=payment.id,
            customer_id=payment.customer_id,
            customer=customer_to_out(payment.customer) if payment.customer is not None else None,
            amount_paise=payment.amount_paise,
            amount_rupees=paise_to_rupees(payment.amount_paise),
            currency=payment.currency,
            method=coerce_method(payment.method),
            status=PaymentStatus(payment.status),
            description=payment.description,
            razorpay_order_id=payment.razorpay_order_id,
            razorpay_payment_id=payment.razorpay_payment_id,
            error_code=payment.error_code,
            error_source=payment.error_source,
            error_step=payment.error_step,
            error_reason=payment.error_reason,
            error_description=payment.error_description,
            is_recovery_attempt=payment.is_recovery_attempt,
            parent_payment_id=payment.parent_payment_id,
            recovery_case_id=recovery_case_id,
            created_at=as_utc(payment.created_at),
            updated_at=as_utc(payment.updated_at),
        )

    def _case_ids_for(self, payment_ids: Any) -> dict[str, str]:
        """Map payment id -> recovery case id for a batch of payments, in one query."""
        ids = [pid for pid in payment_ids]
        if not ids:
            return {}
        rows = self.db.execute(
            select(RecoveryCase.original_payment_id, RecoveryCase.id).where(
                RecoveryCase.original_payment_id.in_(ids)
            )
        ).all()
        return {payment_id: case_id for payment_id, case_id in rows}

    def _audit_payment_failed(self, payment: Payment, *, actor_type: ActorType, actor_id: str) -> None:
        """Write the ledger entry that opens a payment's story."""
        self.ledger.record(
            event_type=AuditEventType.PAYMENT_FAILED,
            actor_type=actor_type,
            actor_id=actor_id,
            payment_id=payment.id,
            summary=(
                f"Payment of Rs {paise_to_rupees(payment.amount_paise):,.2f} by {payment.method} "
                f"failed: {payment.error_description or payment.error_reason or 'reason not reported'}."
            ),
            payload={
                "payment_id": payment.id,
                "customer_id": payment.customer_id,
                "amount_paise": payment.amount_paise,
                "method": payment.method,
                "error_code": payment.error_code,
                "error_reason": payment.error_reason,
                "error_source": payment.error_source,
                "error_step": payment.error_step,
            },
        )

    def _resolve_scenario(self, name: str | None) -> FailureScenario:
        """
        Pick the named scenario, or a random one when the caller did not name it.

        Args:
            name: A key from ``app.db.scenarios.SCENARIOS``, or ``None``.

        Returns:
            The scenario to build a failed payment from.

        Raises:
            NotFoundError: No scenario carries that key. The error lists the keys
                that do exist, because the caller is a human poking at the demo
                and the useful answer to "that name is wrong" is "here are the
                right ones".
        """
        if name is None:
            return random.choice(list(SCENARIOS.values()))
        try:
            return SCENARIOS[name]
        except KeyError:
            raise NotFoundError(
                f"No failure scenario named '{name}'.",
                detail={"scenario": name, "available": sorted(SCENARIOS)},
            ) from None

    def _resolve_customer(self, customer_id: str | None) -> Customer:
        """Load the named customer, or pick one at random from the seeded set."""
        if customer_id is not None:
            customer = self.db.get(Customer, customer_id)
            if customer is None:
                raise NotFoundError(
                    f"No customer with id '{customer_id}'.", detail={"customer_id": customer_id}
                )
            return customer

        customers = list(self.db.execute(select(Customer)).scalars())
        if not customers:
            # Raise rather than invent a customer. A fabricated payer would have
            # no payment history, so the propensity model would score it against
            # a neutral prior and the demo would silently stop demonstrating the
            # thing it exists to demonstrate.
            raise ConfigurationError(
                "No customers exist yet. Seed the database before simulating a failure."
            )
        return random.choice(customers)

    def _customer_for_webhook(self, normalised: dict) -> Customer:
        """
        Find or create the customer a webhook refers to.

        A merchant frequently meets a customer for the first time through a
        failed payment, so an unknown email is a new customer rather than an
        error. The row is created with what the event carried and nothing
        invented: no risk flag, no history, so the propensity model sees a
        genuine blank slate.
        """
        email = str(normalised.get("email") or "").strip()
        if email:
            customer = self.db.execute(
                select(Customer).where(Customer.email == email)
            ).scalars().first()
            if customer is not None:
                return customer

        customer = Customer(
            name=email.split("@")[0] if email else "Unknown customer",
            email=email,
            phone=str(normalised.get("contact") or ""),
        )
        self.db.add(customer)
        self.db.flush()
        logger.info("Created customer %s from webhook payload.", customer.id)
        return customer

    @staticmethod
    def _demo_amount() -> int:
        """A plausible order value in integer paise (never float rupees)."""
        return random.randrange(_DEMO_AMOUNT_MIN_RUPEES, _DEMO_AMOUNT_MAX_RUPEES) * 100
