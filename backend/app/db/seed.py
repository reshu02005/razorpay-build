"""
Deterministic demo data for RecoverAI.

Run it with::

    python -m app.db.seed

**Why it is deterministic.** Every random choice comes from ``random.Random(42)``,
a generator local to this module, so the seeded database is byte-for-byte the
same story every time. That is not a nicety: a demo whose numbers move between
the rehearsal and the presentation is a demo you cannot script. Knowing that the
third failed payment is always a Rs 24,000 UPI timeout is what lets a
walkthrough be written in advance and still be true on the day. Seeding the
module-global ``random`` instead would let any other import perturb the
sequence, which is exactly the drift this avoids.

**What it builds.** Ten customers with deliberately different histories, sixty
payments across the last fourteen days at roughly a four-to-one captured/failed
split, and one audit event per failure so the ledger is anchored and
``GET /api/audit/verify`` has a real chain to check.

**What it deliberately does not build.** No recovery cases. Every failed payment
is left unanalysed, because the single most important thing a reviewer should
see is the agent running live on data it has not seen before -- not a database
of conclusions somebody else's code reached earlier.

The failure mix is drawn from ``app.db.scenarios`` rather than restated here, so
the seeder and ``POST /api/payments/simulate-failure`` manufacture failures from
one catalogue and cannot drift apart.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.audit.ledger import AuditLedger
from app.config import get_settings
from app.db.scenarios import SCENARIOS
from app.db.base import SessionLocal, reset_db
from app.db.models import Customer, Payment, utcnow
from app.domain.enums import (
    ActorType,
    AuditEventType,
    FailureCategory,
    PaymentMethod,
    PaymentStatus,
)

#: Fixed so the demo is reproducible. See the module docstring.
SEED = 42

#: Target dataset size. Sized so the dashboard percentages read like a real
#: merchant's fortnight rather than a toy: with the failure count pinned by
#: category coverage (below), the total is what sets the failure *rate*, and a
#: small total would force an implausible one.
TOTAL_PAYMENTS = 140

#: Target number of failures. The floor is really the number of failure
#: categories -- every category must appear at least once so the agent can be
#: demonstrated on each, and ``seed()`` raises this automatically if the
#: catalogue grows. Against 140 payments that is roughly a 9% failure rate,
#: which is the right order for Indian online card and UPI traffic.
#:
#: Failed *volume* runs at a higher share than failed *count*, and that is
#: deliberate on two counts. The demo needs specimens above the high-value review
#: threshold and above the absolute ceiling for those guardrails to be seen
#: firing, and large payments genuinely do fail more often in the real world --
#: they are the ones that hit issuer limits and attract risk scrutiny. The
#: dashboard reports count and volume separately rather than blending them, so
#: neither number has to stand in for the other.
TARGET_FAILED_PAYMENTS = 12

#: Payments are dated across this window so "last 7 days" style filters and the
#: payment-freshness guardrail both have something to bite on.
HISTORY_DAYS = 14

#: Characters Razorpay uses in its object ids. Only cosmetic -- these ids are
#: never sent anywhere -- but a realistic-looking id keeps the demo honest about
#: what the columns hold.
_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

#: Amount ranges per customer profile, in paise. Ends in whole rupees because
#: real checkout amounts do.
_AMOUNT_BANDS: dict[str, tuple[int, int]] = {
    "small": (19_900, 99_900),      # Rs 199 - Rs 999
    "mid": (100_000, 499_900),      # Rs 1,000 - Rs 4,999
    "large": (500_000, 999_900),    # Rs 5,000 - Rs 9,999
}

#: Two failures above the Rs 10,000 high-value review threshold, so the
#: R5_HIGH_VALUE_REVIEW guardrail fires during the demo instead of being a rule
#: nobody ever sees run.
_HIGH_VALUE_AMOUNTS = (1_250_000, 2_400_000)   # Rs 12,500 and Rs 24,000

#: One failure above the Rs 50,000 absolute ceiling, so R4_AMOUNT_CEILING
#: produces a hard DENY that a reviewer can watch happen.
_ABOVE_CEILING_AMOUNT = 6_499_900              # Rs 64,999

#: One failure older than the 7-day freshness window, so R11_PAYMENT_FRESHNESS
#: has material too.
_STALE_FAILURE_AGE_DAYS = 10.0

#: Newest failures are kept inside the freshness window so most of the queue is
#: actually actionable -- a demo where everything is blocked proves nothing.
_FRESH_FAILURE_MAX_AGE_DAYS = 6.0

_PRODUCTS = (
    "Pro plan annual renewal",
    "Wireless earbuds",
    "Standing desk converter",
    "Yoga mat and blocks",
    "Mechanical keyboard",
    "Coffee subscription",
    "Running shoes",
    "Course bundle: Data Structures",
    "Smart watch strap",
    "Noise cancelling headphones",
)


@dataclass(frozen=True)
class _CustomerSpec:
    """
    A customer profile to generate.

    ``share`` is a relative weight, not a count: payments are drawn from a pool
    in which each customer appears ``share`` times, which produces the long-tail
    distribution a real merchant sees (a few customers buy constantly, most buy
    once) without hard-coding a per-customer payment count.
    """

    name: str
    email: str
    phone: str
    risk_flagged: bool
    share: int
    band: str
    note: str


#: Ten customers, chosen so the propensity model and the guardrails each have at
#: least one interesting case to reason about.
_CUSTOMER_SPECS: tuple[_CustomerSpec, ...] = (
    _CustomerSpec(
        name="Ananya Iyer",
        email="ananya.iyer@example.in",
        phone="+919812300001",
        risk_flagged=False,
        share=12,
        band="large",
        note="High-value loyal customer: buys often, almost never fails.",
    ),
    _CustomerSpec(
        name="Rohit Menon",
        email="rohit.menon@example.in",
        phone="+919812300002",
        risk_flagged=False,
        share=8,
        band="mid",
        note="Regular customer.",
    ),
    _CustomerSpec(
        name="Priya Sharma",
        email="priya.sharma@example.in",
        phone="+919812300003",
        risk_flagged=False,
        share=7,
        band="mid",
        note="Regular customer.",
    ),
    _CustomerSpec(
        name="Vikram Desai",
        email="vikram.desai@example.in",
        phone="+919812300004",
        risk_flagged=False,
        share=6,
        band="small",
        note="Frequent small-ticket buyer.",
    ),
    _CustomerSpec(
        name="Neha Kulkarni",
        email="neha.kulkarni@example.in",
        phone="+919812300005",
        risk_flagged=False,
        share=5,
        band="mid",
        note="Regular customer.",
    ),
    _CustomerSpec(
        name="Arjun Nair",
        email="arjun.nair@example.in",
        phone="+919812300006",
        risk_flagged=False,
        share=5,
        band="small",
        note="Regular customer.",
    ),
    _CustomerSpec(
        name="Sneha Reddy",
        email="sneha.reddy@example.in",
        phone="+919812300007",
        risk_flagged=False,
        share=4,
        band="small",
        note="Occasional customer.",
    ),
    _CustomerSpec(
        name="Imran Qureshi",
        email="imran.qureshi@example.in",
        phone="+919812300008",
        risk_flagged=False,
        share=3,
        band="mid",
        note="Occasional customer.",
    ),
    _CustomerSpec(
        name="Kavya Rao",
        email="kavya.rao@example.in",
        phone="+919812300009",
        risk_flagged=True,
        share=3,
        band="mid",
        note="Risk-flagged by the merchant: R12_CUSTOMER_RISK_FLAG must deny recovery.",
    ),
    _CustomerSpec(
        name="Dev Bhatia",
        email="dev.bhatia@example.in",
        phone="+919812300010",
        risk_flagged=False,
        # Zero payments on purpose. total_payments == 0 is the only way to
        # exercise the neutral 0.5 prior in Customer.prior_success_rate; giving
        # this customer a single failed payment would score them 0.0, which is
        # the opposite of "we have no information about them yet". They are also
        # the customer to aim POST /api/payments/simulate-failure at when
        # demonstrating a cold-start prediction.
        share=0,
        band="small",
        note="Brand new customer with no payment history at all.",
    ),
)

# ---------------------------------------------------------------------------
# Scenario catalogue
# ---------------------------------------------------------------------------


def _load_scenarios() -> dict[FailureCategory, list[dict[str, Any]]]:
    """
    Read the shared failure catalogue and index it by failure category.

    Returns:
        Category -> list of scenario dicts carrying exactly the columns a
        ``Payment`` row needs.

    Indexing by category (rather than just iterating the catalogue) is what lets
    the seeder guarantee that *every* category appears in the failed set.
    Category coverage is a property of the generated dataset, and the dataset is
    this module's responsibility, so the grouping belongs here.

    ``SCENARIOS`` is imported and its fields read directly, rather than probed
    for by name. An earlier version tried to be tolerant -- looking for the
    collection under several plausible module attributes and each field under
    several plausible names, falling back to ``None``. That tolerance is a
    liability, not a kindness: when the catalogue actually named its field
    ``expected_category`` and the probe looked for ``category``, the fallback
    turned a mismatch that should have failed at import into a ``None`` that
    surfaced hundreds of lines later as ``None is not a valid FailureCategory``.
    A direct attribute access on a frozen dataclass fails immediately, names the
    missing field, and is checkable by a type checker.
    """
    by_category: dict[FailureCategory, list[dict[str, Any]]] = {}
    for scenario in SCENARIOS.values():
        by_category.setdefault(scenario.expected_category, []).append(
            {
                "name": scenario.key,
                "category": scenario.expected_category,
                "method": scenario.method,
                "error_code": scenario.error_code,
                "error_source": scenario.error_source,
                "error_step": scenario.error_step,
                "error_reason": scenario.error_reason,
                "error_description": scenario.error_description,
            }
        )
    return by_category


# ---------------------------------------------------------------------------
# Small deterministic generators
# ---------------------------------------------------------------------------


def _gateway_id(rng: random.Random, prefix: str) -> str:
    """Produce a Razorpay-shaped identifier such as ``order_Nf3kQ2mZp8Ax1B``."""
    return f"{prefix}_{''.join(rng.choices(_ID_ALPHABET, k=14))}"


def _amount(rng: random.Random, band: str) -> int:
    """Draw an amount in paise from a band, rounded to whole rupees."""
    low, high = _AMOUNT_BANDS[band]
    return rng.randrange(low, high, 100)


def _when(rng: random.Random, now: datetime, *, min_days: float, max_days: float) -> datetime:
    """Pick a timezone-aware timestamp between ``min_days`` and ``max_days`` ago."""
    return now - timedelta(hours=rng.uniform(min_days * 24.0, max_days * 24.0))


def _description(rng: random.Random) -> str:
    """Produce a plausible order description."""
    return f"{rng.choice(_PRODUCTS)} (order #{rng.randrange(10_000, 99_999)})"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed(db: Session) -> dict[str, Any]:
    """
    Populate an empty database with the demo dataset.

    Args:
        db: an open session against an already-created, empty schema. The caller
            owns resetting the database -- ``main()`` does it, tests usually want
            a fresh in-memory schema instead, and a seeder that dropped tables on
            its own would be unusable from a test.

    Returns:
        A summary dict: counts, volumes in paise, the failure mix, and the
        guardrail material the dataset deliberately contains.

    Raises:
        RuntimeError: the scenario catalogue could not be read.
    """
    rng = random.Random(SEED)
    settings = get_settings()
    # One "now" for the whole run, so every relative age in the dataset is
    # measured from the same instant. Calling utcnow() per row would let the
    # clock move mid-seed and make the ordering subtly non-reproducible.
    now = utcnow()

    catalogue = _load_scenarios()

    # -- Customers --------------------------------------------------------
    customers: list[Customer] = []
    for spec in _CUSTOMER_SPECS:
        customer = Customer(
            name=spec.name,
            email=spec.email,
            phone=spec.phone,
            risk_flagged=spec.risk_flagged,
            # Counters are recomputed from the generated rows at the end of this
            # function. Setting them here as well would create a second source of
            # truth that could quietly disagree with the payments table.
            total_payments=0,
            successful_payments=0,
            lifetime_value_paise=0,
            created_at=now - timedelta(days=HISTORY_DAYS + 30),
        )
        db.add(customer)
        customers.append(customer)
    db.flush()  # assign primary keys before payments reference them

    spec_by_customer = dict(zip(customers, _CUSTOMER_SPECS))
    risk_customer = next(c for c in customers if c.risk_flagged)

    # Weighted draw pool. A customer with share 0 (the brand-new one) simply
    # never appears, which is how they end up with no payment history.
    pool: list[Customer] = [
        customer
        for customer, spec in spec_by_customer.items()
        for _ in range(spec.share)
    ]

    # -- Failed payments ---------------------------------------------------
    # Recoverable categories first. The three guardrail-demo failures are the
    # first three slots, and they need to reach the amount rules -- a
    # non-recoverable category short-circuits every rule to ALLOW long before
    # R4/R5 are consulted, so it would hide exactly what those slots exist to
    # show.
    ordered_categories = sorted(catalogue, key=lambda c: (not c.is_recoverable, c.value))
    failed_count = max(TARGET_FAILED_PAYMENTS, len(ordered_categories))
    category_slots = [
        ordered_categories[i % len(ordered_categories)] for i in range(failed_count)
    ]

    # Failures are dealt round-robin across a shuffled pool rather than drawn
    # independently, so no customer collects a disproportionate share. This is
    # not cosmetic: R8 caps a customer at three recovery cases per day, and an
    # independent draw over ten customers routinely gave one of them five of the
    # twelve failures. A reviewer who then analysed everything would watch the
    # headline bank-decline case get denied for *velocity* rather than for the
    # amount rule it was built to demonstrate -- the same "two causes teaches you
    # nothing about either" problem the slot assignments above exist to avoid.
    #
    # R8 stays demonstrable: the reviewer can simulate extra failures for one
    # customer, and the rule still fires exactly as designed.
    #
    # The rotation is built from DISTINCT customers, not from `pool`. `pool` is a
    # weighted list that repeats a busy customer once per unit of share -- correct
    # for drawing captured payments, where some customers genuinely should buy
    # more often, but cycling it would reproduce the very concentration this is
    # meant to remove. The risk-flagged customer is excluded because slot 3 is
    # already reserved for them, and the zero-share customer is excluded because
    # their entire purpose is to have no payment history at all.
    rotation = [
        customer
        for customer, spec in spec_by_customer.items()
        if spec.share > 0 and not customer.risk_flagged
    ]
    rng.shuffle(rotation)

    failed_payments: list[Payment] = []
    for index, category in enumerate(category_slots):
        scenario = rng.choice(catalogue[category])

        # Slot 3 belongs to the risk-flagged customer and slot 4 is deliberately
        # stale; both carry recoverable categories (they are near the front of
        # the ordered list) so that R12 and R11 respectively are the *only*
        # reason those cases get blocked. A blocked case with two causes teaches
        # a reviewer nothing about either.
        if index == 3:
            customer = risk_customer
        else:
            customer = rotation[index % len(rotation)]

        if index == 0:
            amount_paise = _HIGH_VALUE_AMOUNTS[0]
        elif index == 1:
            amount_paise = _HIGH_VALUE_AMOUNTS[1]
        elif index == 2:
            amount_paise = _ABOVE_CEILING_AMOUNT
        else:
            amount_paise = _amount(rng, spec_by_customer[customer].band)

        if index == 4:
            created_at = _when(
                rng, now, min_days=_STALE_FAILURE_AGE_DAYS, max_days=_STALE_FAILURE_AGE_DAYS
            )
        else:
            created_at = _when(rng, now, min_days=0.05, max_days=_FRESH_FAILURE_MAX_AGE_DAYS)

        payment = Payment(
            razorpay_order_id=_gateway_id(rng, "order"),
            razorpay_payment_id=_gateway_id(rng, "pay"),
            customer_id=customer.id,
            amount_paise=amount_paise,
            currency=settings.currency,
            # ``.value`` rather than the enum member: these columns are plain
            # String, and storing the raw token keeps the database readable with
            # any SQLite browser.
            method=scenario["method"].value,
            status=PaymentStatus.FAILED.value,
            description=_description(rng),
            error_code=scenario["error_code"],
            error_source=scenario["error_source"],
            error_step=scenario["error_step"],
            error_reason=scenario["error_reason"],
            error_description=scenario["error_description"],
            is_recovery_attempt=False,
            created_at=created_at,
            updated_at=created_at,
        )
        db.add(payment)
        failed_payments.append(payment)

    # -- Captured payments -------------------------------------------------
    captured_payments: list[Payment] = []
    for _ in range(max(TOTAL_PAYMENTS - failed_count, 0)):
        customer = rng.choice(pool)
        created_at = _when(rng, now, min_days=0.1, max_days=float(HISTORY_DAYS))
        payment = Payment(
            razorpay_order_id=_gateway_id(rng, "order"),
            razorpay_payment_id=_gateway_id(rng, "pay"),
            customer_id=customer.id,
            amount_paise=_amount(rng, spec_by_customer[customer].band),
            currency=settings.currency,
            method=rng.choice(
                (PaymentMethod.CARD, PaymentMethod.UPI, PaymentMethod.NETBANKING, PaymentMethod.WALLET)
            ).value,
            status=PaymentStatus.CAPTURED.value,
            description=_description(rng),
            is_recovery_attempt=False,
            created_at=created_at,
            updated_at=created_at,
        )
        db.add(payment)
        captured_payments.append(payment)

    db.flush()

    # -- Denormalised counters --------------------------------------------
    # Recomputed from the rows just written rather than accumulated while
    # generating, so the counters cannot disagree with the payments table. The
    # ML model reads prior_success_rate at inference time; if it were wrong here,
    # every propensity score in the demo would be wrong in a way nobody could see.
    all_payments = failed_payments + captured_payments
    for customer in customers:
        owned = [p for p in all_payments if p.customer_id == customer.id]
        captured = [p for p in owned if p.status == PaymentStatus.CAPTURED.value]
        customer.total_payments = len(owned)
        customer.successful_payments = len(captured)
        customer.lifetime_value_paise = sum(p.amount_paise for p in captured)

    # -- Summary ------------------------------------------------------------
    # The failure category is not a column on Payment -- only the raw gateway
    # error is stored, so that a later correction to the taxonomy cannot rewrite
    # history. The mix is therefore counted from the slots that generated the
    # rows, which is the same thing the agent will re-derive at analysis time.
    failure_mix: dict[str, int] = {}
    for category in category_slots:
        failure_mix[category.value] = failure_mix.get(category.value, 0) + 1

    freshness_cutoff = now - timedelta(hours=settings.max_payment_age_hours)
    summary: dict[str, Any] = {
        "seed": SEED,
        "customers": len(customers),
        "risk_flagged_customers": sum(1 for c in customers if c.risk_flagged),
        "customers_without_history": sum(1 for c in customers if c.total_payments == 0),
        "payments": len(all_payments),
        "captured_payments": len(captured_payments),
        "failed_payments": len(failed_payments),
        "unanalysed_failures": len(failed_payments),
        "total_volume_paise": sum(p.amount_paise for p in all_payments),
        "captured_volume_paise": sum(p.amount_paise for p in captured_payments),
        "failed_volume_paise": sum(p.amount_paise for p in failed_payments),
        "failure_mix": failure_mix,
        "guardrail_material": {
            "above_high_value_threshold": sum(
                1 for p in failed_payments
                if p.amount_paise >= settings.high_value_review_threshold_paise
            ),
            "above_absolute_ceiling": sum(
                1 for p in failed_payments
                if p.amount_paise > settings.max_recovery_amount_paise
            ),
            "beyond_freshness_window": sum(
                1 for p in failed_payments if p.created_at < freshness_cutoff
            ),
            "on_risk_flagged_customer": sum(
                1 for p in failed_payments if p.customer_id == risk_customer.id
            ),
        },
        # One ledger entry per failure; finalised here rather than after the loop
        # below so that the copy embedded in the genesis payload is complete. A
        # dict mutated after it has been hashed is a subtle way to end up with a
        # payload that no longer matches its own hash.
        "audit_events": len(failed_payments),
    }

    # -- Audit ledger -------------------------------------------------------
    # One event per seeded failure, written oldest-first so the chain reads in
    # the order things happened. The first of these is the genesis entry: its
    # prev_hash is the all-zero root, and every later hash depends on it.
    #
    # AuditEventType is a closed set on purpose -- an auditor should be able to
    # read the enum and know everything the system can do to money -- so the
    # genesis entry is not given an invented "seeded" type. It is a real
    # payment_failed event that carries the dataset summary in its payload, which
    # makes the root of the chain self-describing without lying about what it is.
    ledger = AuditLedger(db)
    for position, payment in enumerate(sorted(failed_payments, key=lambda p: p.created_at)):
        payload: dict[str, Any] = {
            "amount_paise": payment.amount_paise,
            "method": payment.method,
            "error_code": payment.error_code,
            "error_reason": payment.error_reason,
        }
        if position == 0:
            # A copy, so the ledger owns an immutable snapshot rather than a
            # live reference to the dict this function goes on to return.
            payload["seed_summary"] = dict(summary)
        ledger.record(
            event_type=AuditEventType.PAYMENT_FAILED,
            actor_type=ActorType.SYSTEM,
            actor_id="seed",
            payment_id=payment.id,
            summary=(
                f"Payment {payment.id} failed: "
                f"{payment.error_description or payment.error_reason or 'no reason supplied'}"
            ),
            payload=payload,
        )

    db.commit()
    return summary


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------


def _rupees(paise: int) -> str:
    """
    Format paise as a rupee string for the console.

    Float division is acceptable here and only here: this value is printed and
    then discarded. Nothing downstream computes on it.
    """
    return f"Rs {paise / 100:,.2f}"


def _format_summary(summary: dict[str, Any]) -> str:
    """
    Render the seed result as plain ASCII.

    No box-drawing characters. The target machine is Windows, whose console
    still defaults to cp1252 in several common configurations, and a
    UnicodeEncodeError while printing a success message is an embarrassing way to
    end a successful run.
    """
    rule = "=" * 66
    thin = "-" * 66
    material = summary["guardrail_material"]

    lines = [
        rule,
        "RecoverAI demo data seeded",
        rule,
        f"Customers .................. {summary['customers']:>4}"
        f"  ({summary['risk_flagged_customers']} risk-flagged, "
        f"{summary['customers_without_history']} with no history)",
        f"Payments ................... {summary['payments']:>4}"
        f"  ({summary['captured_payments']} captured, {summary['failed_payments']} failed)",
        f"Total volume ............... {_rupees(summary['total_volume_paise'])}",
        f"Captured volume ............ {_rupees(summary['captured_volume_paise'])}",
        f"Failed volume .............. {_rupees(summary['failed_volume_paise'])}"
        "   <- the recovery opportunity",
        f"Audit events ............... {summary['audit_events']:>4}"
        "  (chain anchored at sequence 1)",
        "",
        f"Failure mix -- all {summary['unanalysed_failures']} left UNANALYSED so the agent runs live:",
    ]
    for category, count in sorted(summary["failure_mix"].items()):
        lines.append(f"  {category:.<34} {count:>3}")

    lines += [
        "",
        "Guardrail material deliberately included:",
        f"  above the high-value review threshold ..... {material['above_high_value_threshold']:>3}",
        f"  above the absolute amount ceiling ......... {material['above_absolute_ceiling']:>3}",
        f"  older than the payment freshness window ... {material['beyond_freshness_window']:>3}",
        f"  belonging to a risk-flagged customer ...... {material['on_risk_flagged_customer']:>3}",
        thin,
        "Next:  python -m app.main      then open http://127.0.0.1:8000/docs",
        rule,
    ]
    return "\n".join(lines)


def main() -> None:
    """
    Drop, recreate and repopulate the database, then print what was created.

    Destructive by design: re-running it must give the same database, and the
    only way to guarantee that is to start from nothing. ``seed()`` itself is
    non-destructive so tests can call it against their own fresh schema.
    """
    reset_db()
    with SessionLocal() as db:
        summary = seed(db)
    print(_format_summary(summary))


if __name__ == "__main__":
    main()
