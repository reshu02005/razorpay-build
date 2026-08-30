"""
Shared fixtures -- and the testing philosophy -- for the RecoverAI suite.

Why this suite exists
---------------------
RecoverAI concludes, on its own, that money should move. Every file in this
directory protects one of the properties that makes that safe to put in front of
a merchant:

* the model cannot reach a tool that moves money and cannot name an amount
  (``test_agent_tool_safety``);
* thirteen guardrails run on every proposal and the most restrictive verdict wins
  (``test_policy_engine``);
* the audit ledger detects any edit to its own history (``test_audit_chain``);
* the classification and the score a decision rests on are complete, and degrade
  honestly when their inputs are missing (``test_taxonomy``, ``test_ml_predictor``);
* a payment is only ever marked recovered against a signature the server computed
  itself (``test_gateway``, ``test_recovery_flow``);
* and the HTTP surface a merchant integrates against does not drift away from its
  documented contract (``test_api_smoke``).

These tests are not here for a coverage number. Every test must answer "what bug
does this catch?" from its own name. That standard is what rules out tests of
trivial accessors, assertions on log wording, and fixtures heavier than the
invariant they exist to set up. Where a test cannot pin an exact value without
over-constraining an implementation choice, it pins the *property* instead and
says so in a comment -- an over-specified test is a test that will be deleted by
the next person who touches the code, which is worse than no test at all.
"""

from __future__ import annotations

import os

#: The one credential the suite configures. "A correctly signed webhook is
#: accepted" is not testable without a secret to sign with, and Razorpay's
#: webhook secret is independent of the API credentials -- setting it does *not*
#: switch the gateway out of simulated mode, which needs a key id *and* a key
#: secret. See ``Settings.razorpay_enabled``.
TEST_WEBHOOK_SECRET = "whsec_recoverai_test_only"

# ---------------------------------------------------------------------------
# Environment, fixed before the application is imported
# ---------------------------------------------------------------------------
# This has to happen at module import, not in a fixture. pydantic-settings reads
# the process environment when the first ``Settings()`` is constructed, and
# ``app/config.py`` constructs one at import time; pytest imports conftest before
# it imports (and therefore before it collects) any test module, but every
# fixture runs later than that. A fixture would be too late.
#
# Each assignment overrides whatever a developer happens to have in backend/.env,
# because real environment variables outrank the .env file in pydantic-settings'
# precedence order. That is deliberate: the suite must exercise the documented
# zero-credential configuration on every machine, not the configuration of
# whoever is running it.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"  # never touch the dev's real recoverai.db
os.environ["GEMINI_API_KEY"] = ""                  # force AgentMode.RULE_BASED
os.environ["RAZORPAY_KEY_ID"] = ""                 # force GatewayMode.SIMULATED
os.environ["RAZORPAY_KEY_SECRET"] = ""
os.environ["RAZORPAY_WEBHOOK_SECRET"] = TEST_WEBHOOK_SECRET

from collections.abc import Callable, Iterator, Mapping  # noqa: E402
from datetime import timedelta  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.agent.taxonomy import DESCRIPTION_PATTERNS, ERROR_CODE_MAP, REASON_MAP  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.db.models import Base, Customer, Payment, utcnow  # noqa: E402
from app.domain.enums import (  # noqa: E402
    FailureCategory,
    PaymentMethod,
    PaymentStatus,
    RecoveryStrategy,
)
from app.policy.rules import GuardrailContext  # noqa: E402

# ``app.config`` builds a cached Settings at import time. If anything imported it
# before the assignments above ran, that instance would hold the developer's
# credentials. Dropping the cache here makes the first fixture-level
# ``get_settings()`` rebuild from the environment we just fixed.
get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """
    One in-memory SQLite engine for the whole session.

    ``StaticPool`` is not an optimisation, it is a correctness requirement. An
    ``sqlite:///:memory:`` database lives inside a single DBAPI connection, so
    with the default pool every checkout can hand back a *different* connection
    and therefore a different, empty database -- tables created by a fixture
    would simply not exist by the time the test queried them. StaticPool pins the
    pool to exactly one connection, so the schema and the rows persist.

    ``check_same_thread=False`` is needed because Starlette's TestClient runs the
    application on a worker thread while the test body stays on the main thread,
    and both touch the same connection.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        # SQLite ignores REFERENCES clauses unless foreign keys are switched on
        # per connection. Without this the tests would happily accept an orphaned
        # recovery case, which the production database (with the same PRAGMA set
        # in app/db/base.py) would reject.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """
    A session over a freshly rebuilt schema.

    Dropping and recreating every table per test is cheap on an in-memory
    database and gives perfect isolation, including for the audit ledger -- whose
    tests depend on sequence numbers starting from a known, empty chain. A
    rollback-only strategy would not reset the sequence counter.

    ``expire_on_commit=False`` matters here: the service layer commits, and
    without this every ORM object a fixture handed the test would be expired and
    silently reloaded. On SQLite that reload returns *naive* datetimes (the
    driver has no timezone support), which would make guardrail arithmetic in a
    test behave differently from the same arithmetic in the application.
    """
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    """
    The active configuration, rebuilt for each test.

    ``cache_clear()`` is what stops a test that tweaked the environment from
    leaking a stale singleton into the next one. Tests that need a different
    policy use ``settings.model_copy(update={...})`` rather than mutating this
    object, so the guardrail limits one test relaxes cannot silently apply to
    another.
    """
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------


def error_fields_for(category: FailureCategory) -> dict[str, str]:
    """
    Build the smallest set of gateway error fields that classifies to ``category``.

    Derived from the taxonomy's own lookup tables instead of hard-coding Razorpay
    error codes in the fixtures. The alternative -- writing ``"GATEWAY_ERROR"``
    literals into every test -- would mean that renaming a code in the taxonomy
    breaks a dozen unrelated tests with an unhelpful message, and that a fixture
    could quietly stop producing the category it claims to produce.

    Args:
        category: the failure category the caller wants a payment to exhibit.

    Returns:
        Keyword arguments suitable for a ``Payment`` row's error columns.

    Raises:
        AssertionError: if no rule in the taxonomy can produce ``category``,
            which means the category is unreachable from real gateway output.
    """
    for code, mapped in ERROR_CODE_MAP.items():
        if mapped is category:
            return {"error_code": code}
    for reason, mapped in REASON_MAP.items():
        if mapped is category:
            return {"error_reason": reason}
    for pattern, mapped in DESCRIPTION_PATTERNS:
        if mapped is category:
            return {"error_description": pattern}
    raise AssertionError(
        f"No taxonomy entry produces {category.value!r}; a fixture cannot manufacture it."
    )


@pytest.fixture()
def customer(db: Session) -> Customer:
    """A settled, unflagged customer with a healthy payment history (8 of 10)."""
    record = Customer(
        id="cust_test_asha",
        name="Asha Nair",
        email="asha.nair@example.test",
        phone="+919800000001",
        risk_flagged=False,
        total_payments=10,
        successful_payments=8,
        lifetime_value_paise=1_500_000,
    )
    db.add(record)
    db.commit()
    return record


@pytest.fixture()
def make_failed_payment(db: Session, customer: Customer) -> Callable[..., Payment]:
    """
    Factory for failed payments with a chosen failure category.

    A factory rather than a set of pre-baked fixtures because the flow tests need
    several payments per test (one case per payment is a database constraint) and
    because the interesting axis -- which failure category the taxonomy will see
    -- differs per test.
    """

    def _make(
        *,
        category: FailureCategory = FailureCategory.GATEWAY_ERROR,
        amount_paise: int = 250_000,  # Rs 2,500: under every default ceiling
        method: PaymentMethod = PaymentMethod.CARD,
        owner: Customer | None = None,
        age_hours: float = 0.0,
        description: str = "Order #TEST-1001",
    ) -> Payment:
        payment = Payment(
            customer_id=(owner or customer).id,
            amount_paise=amount_paise,
            currency="INR",
            method=method.value,
            status=PaymentStatus.FAILED.value,
            description=description,
            created_at=utcnow() - timedelta(hours=age_hours),
            **error_fields_for(category),
        )
        db.add(payment)
        db.commit()
        return payment

    return _make


@pytest.fixture()
def failed_payment(make_failed_payment: Callable[..., Payment]) -> Payment:
    """One recoverable failed payment: a gateway-side fault, fresh, Rs 2,500."""
    return make_failed_payment()


# ---------------------------------------------------------------------------
# Guardrail context factory
# ---------------------------------------------------------------------------


@pytest.fixture()
def policy_ctx(
    failed_payment: Payment,
    customer: Customer,
    settings: Settings,
) -> Callable[..., GuardrailContext]:
    """
    Build a ``GuardrailContext`` that violates nothing, with per-test overrides.

    The defaults are chosen so that a clean context trips exactly one rule -- the
    human-approval rule, which always fires under the shipped configuration. That
    makes a rule test one line: override the single field the rule reads and
    assert on that rule's evaluation.

    Two deliberate details:

    * ``amount_paise`` defaults to the payment's own amount, read at call time.
      So a test that raises ``payment.amount_paise`` before calling the factory
      exercises the ceiling rules *without* also tripping the amount-integrity
      rule, and a test that overrides ``amount_paise`` alone exercises amount
      integrity in isolation. Those two rules are the ones most easily confused
      for each other.
    * The same ``Settings`` object is passed here and to ``PolicyEngine``, so a
      test that retunes policy does it in one place and cannot end up with an
      engine and a context that disagree about the limits.
    """

    def _make(**overrides: object) -> GuardrailContext:
        payment = overrides.pop("payment", failed_payment)
        values: dict[str, object] = {
            "payment": payment,
            "customer": customer,
            "strategy": RecoveryStrategy.RETRY_SAME_METHOD,  # moves money -> rules apply
            "failure_category": FailureCategory.GATEWAY_ERROR,
            "propensity_score": 0.70,
            "amount_paise": payment.amount_paise,
            "attempt_number": 1,
            "now": utcnow(),
            "last_attempt_at": None,
            "open_attempt_exists": False,
            "daily_recovery_total_paise": 0,
            "customer_cases_today": 0,
            "settings": settings,
        }
        values.update(overrides)
        return GuardrailContext(**values)  # type: ignore[arg-type]

    return _make


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db: Session) -> Iterator[object]:
    """
    A ``TestClient`` whose request handlers share this test's session.

    Overriding ``get_db`` -- rather than pointing the application's own engine at
    the test database -- keeps one session, and therefore one identity map, for
    both the fixtures and the request handlers. Without that, a row a fixture
    committed and a row a handler reads back would be two different Python
    objects, and assertions about counters updated during a request would be
    testing a stale copy.

    The application is imported inside the fixture so that test modules which
    never speak HTTP do not pay the cost of importing FastAPI, the routers and
    scikit-learn.
    """
    from fastapi.testclient import TestClient

    from app.db.base import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        # Overrides live on the module-level app object, which is shared by every
        # test in the session; leaving one behind would silently hand a closed
        # session to the next test.
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Small helpers shared by more than one test module
# ---------------------------------------------------------------------------


def field_of(entry: object, name: str) -> object:
    """
    Read ``name`` off a record that may be a mapping, a dataclass or a model.

    The seed scenario catalogue is data, and data catalogues get restructured.
    Reading through one accessor means a change of container shape does not
    rewrite the tests that consume it.
    """
    if isinstance(entry, Mapping):
        return entry.get(name)
    return getattr(entry, name, None)
