"""
Engine, session factory and schema lifecycle.

Everything that needs a database goes through this module, so there is exactly
one engine and one set of connection settings in the process. Two of those
settings exist specifically because the default database is SQLite, and SQLite
has two behaviours that will quietly corrupt a payments demo if left alone:

*   it refuses cross-thread reuse of a connection, and FastAPI runs synchronous
    endpoints on a thread pool; and
*   it does **not** enforce foreign keys unless asked to, per connection.

Both are handled below with the dialect guarded, so pointing ``DATABASE_URL`` at
Postgres later is a configuration change rather than a code change.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings

# Importing the models module is what registers every table on ``Base.metadata``.
# Without this import ``init_db()`` would happily create *nothing* and the first
# query would fail with "no such table" -- a confusing symptom for a
# five-characters-long cause.
from app.db.models import Base

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_connect_args: dict[str, Any] = {}
_engine_kwargs: dict[str, Any] = {}

if settings.is_sqlite:
    # SQLite's Python driver raises if a connection is used from a thread other
    # than the one that created it. FastAPI dispatches every `def` (non-async)
    # endpoint to a worker thread, so a connection checked out of the pool is
    # routinely touched by a different thread than the one that opened it.
    # Disabling the check is safe here because SQLAlchemy's connection pool
    # already guarantees one connection is only handed to one caller at a time --
    # the property the driver's check was approximating.
    _connect_args["check_same_thread"] = False

    if ":memory:" in settings.database_url:
        # An in-memory database lives *inside* a connection. With the normal pool
        # each checkout would open a fresh, empty database, so a table created by
        # one call would be invisible to the next. StaticPool keeps a single
        # connection alive for the engine's lifetime, which is what makes
        # `sqlite:///:memory:` usable for tests at all.
        _engine_kwargs["poolclass"] = StaticPool

#: The one engine for the process. ``echo`` is driven by config rather than
#: hard-coded because SQL logging is invaluable while debugging a guardrail
#: query and unreadable noise the rest of the time.
engine = create_engine(
    settings.database_url,
    echo=settings.sql_echo,
    connect_args=_connect_args,
    **_engine_kwargs,
)


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    """
    Turn on foreign-key enforcement for every new SQLite connection.

    SQLite ships with ``PRAGMA foreign_keys`` **off** for backwards
    compatibility, and the setting is per-connection, not per-database. Without
    this listener every ``ForeignKey`` declared in ``models.py`` would be pure
    documentation: a ``RecoveryCase`` could point at a payment id that does not
    exist, and an orphaned case in a financial audit trail is not a cosmetic
    problem.

    The ``isinstance`` check is the dialect guard. This listener is registered on
    the generic ``Engine`` class, so it fires for *any* engine created in the
    process -- including a Postgres one added later, where this PRAGMA is not
    valid SQL and would raise on connect.

    Args:
        dbapi_connection: The raw driver connection SQLAlchemy just opened.
        connection_record: Pool bookkeeping object; unused, but part of the
            event's signature.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

#: Session factory. Two non-default choices, both about who owns a transaction:
#:
#: ``autoflush=False`` -- an implicit flush in the middle of a read is a classic
#: source of "why did my half-built row hit the database?". The services in this
#: project flush explicitly, at points they chose (see ``AuditLedger.record``).
#:
#: ``expire_on_commit=False`` -- after ``commit()`` SQLAlchemy would normally
#: expire every loaded attribute, so the response builder reading
#: ``case.status`` would trigger a fresh SELECT on a session the request is about
#: to close. Keeping the loaded values usable after commit is what lets a service
#: commit once at the end and still return the object it just wrote.
SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Iterator[Session]:
    """
    FastAPI dependency yielding a request-scoped session.

    Yields:
        A ``Session`` that is closed when the request finishes.

    The session is deliberately *not* committed here. Transactions are owned by
    the service that knows what a complete unit of work is -- for a recovery
    approval that unit spans a state transition, an attempt row, a gateway call
    and several audit events, and committing them piecemeal would leave a
    half-approved case behind if step four failed.

    There is no explicit ``rollback()`` in the ``finally`` block because
    ``Session.close()`` already discards any uncommitted transaction. A handler
    that raises mid-way therefore cannot leak partial writes into the next
    request, and adding a second rollback would only restate the guarantee.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Schema lifecycle
# ---------------------------------------------------------------------------


def init_db() -> None:
    """
    Create any missing tables.

    Idempotent -- ``create_all`` skips tables that already exist -- so it is safe
    to call on every application start-up, which is what makes a clean checkout
    runnable with a single command.

    This project uses ``create_all`` rather than Alembic migrations on purpose.
    Migrations solve the problem of evolving a schema that holds data you cannot
    lose; here the database is a disposable demo fixture rebuilt by the seeder,
    so a migration tool would add a dependency and a workflow step while solving
    nothing that is actually at risk.
    """
    Base.metadata.create_all(bind=engine)


def reset_db() -> None:
    """
    Drop every table and recreate it empty.

    **Destructive.** Intended for the seeder (``python -m app.db.seed``) and for
    test fixtures that need a known-empty ledger -- notably the audit chain
    tests, which cannot assert "sequence starts at 1" against a database
    carrying yesterday's events.

    Dropping before creating rather than deleting rows is intentional: it also
    clears the schema itself, so a model change during development takes effect
    instead of silently mismatching an old table definition.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
