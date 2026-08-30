"""
The audit ledger: proving that the record of what the system did to money has
not been edited after the fact.

A demo that merely *claims* immutability proves nothing, and an append-only
convention is only a convention -- anyone with the SQLite file has an UPDATE
statement. These tests do what a sceptical reviewer would do: tamper with the
stored history in the three ways that matter (rewrite a row, delete a row,
represent a timestamp differently) and check that verification notices, and
notices in the right place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.audit.ledger import GENESIS_HASH, AuditLedger, compute_event_hash
from app.db.models import AuditEvent
from app.domain.enums import ActorType, AuditEventType

#: Enough concurrent writers to make the race reliable without making the test slow.
WRITER_COUNT = 8

CHAIN_LENGTH = 5

#: One instant, written two legitimate ways. IST is the timezone the merchant and
#: the developer's laptop actually live in, which is exactly why it is the one
#: that will show up in a hand-written payload some day.
IST = timezone(timedelta(hours=5, minutes=30))
INSTANT_IST = datetime(2026, 3, 1, 17, 30, 0, tzinfo=IST)
INSTANT_UTC = INSTANT_IST.astimezone(timezone.utc)


@pytest.fixture()
def ledger(db: Session) -> AuditLedger:
    return AuditLedger(db)


@pytest.fixture()
def chain(db: Session, ledger: AuditLedger) -> list[AuditEvent]:
    """A short, realistic ledger: one case walked from failure to recovery."""
    script = [
        (AuditEventType.PAYMENT_FAILED, ActorType.WEBHOOK, "Payment failed at the issuer"),
        (AuditEventType.STRATEGY_PROPOSED, ActorType.AGENT, "Agent proposed a same-method retry"),
        (AuditEventType.GUARDRAILS_EVALUATED, ActorType.SYSTEM, "13 guardrails evaluated"),
        (AuditEventType.APPROVAL_GRANTED, ActorType.HUMAN, "Approved by ops@merchant.test"),
        (AuditEventType.RECOVERY_SUCCEEDED, ActorType.SYSTEM, "Rs 2,500 recovered"),
    ]
    events = [
        ledger.record(
            event_type=event_type,
            actor_type=actor_type,
            summary=summary,
            case_id="case_under_test",
            payment_id="pay_under_test",
            payload={"step": index},
        )
        for index, (event_type, actor_type, summary) in enumerate(script)
    ]
    db.commit()
    assert len(events) == CHAIN_LENGTH
    return events


def test_an_untouched_chain_verifies(ledger: AuditLedger, chain: list[AuditEvent]) -> None:
    """The control case: without it, a verifier that always reports failure would pass."""
    result = ledger.verify_chain()
    assert result.valid is True
    assert result.events_checked == CHAIN_LENGTH
    assert result.broken_at_sequence is None
    assert result.head_hash == chain[-1].hash


def test_the_chain_starts_at_genesis_and_has_no_gaps(chain: list[AuditEvent]) -> None:
    """
    Sequence numbers must be contiguous, because a gap is itself the evidence of
    a deletion. If the ledger allocated sequences from a source that could skip -
    a UUID ordering, a per-case counter, a database default that a rollback
    consumed - the gap test below would report false tampering on a healthy chain.
    """
    assert chain[0].prev_hash == GENESIS_HASH
    sequences = [event.sequence for event in chain]
    assert sequences == list(range(sequences[0], sequences[0] + CHAIN_LENGTH))
    for earlier, later in zip(chain, chain[1:]):
        assert later.prev_hash == earlier.hash


def test_rewriting_a_stored_payload_is_caught_at_that_exact_event(
    db: Session, ledger: AuditLedger, chain: list[AuditEvent]
) -> None:
    """
    The attack this whole design exists to defeat: quietly changing what the
    record says happened.

    Naming the *first* broken sequence is the part worth testing. "Something is
    wrong somewhere" is not an audit trail; an investigator needs to be pointed at
    the row, and every row after it will also fail to verify.
    """
    tampered = chain[2]
    tampered.payload = {"step": 2, "guardrails": "all passed, honest"}
    db.commit()

    result = ledger.verify_chain()
    assert result.valid is False
    assert result.broken_at_sequence == tampered.sequence
    assert result.message


def test_deleting_an_event_from_the_middle_is_detected(
    db: Session, ledger: AuditLedger, chain: list[AuditEvent]
) -> None:
    """
    Removing an inconvenient event -- the blocked recovery, the rejected approval
    -- must be as visible as rewriting one.

    The reported position is allowed to be either the missing sequence or the
    orphaned event that followed it: both name the break correctly, and pinning
    one would be testing an implementation choice rather than the guarantee.
    """
    removed_sequence = chain[2].sequence
    db.delete(chain[2])
    db.commit()

    result = ledger.verify_chain()
    assert result.valid is False
    assert result.broken_at_sequence in (removed_sequence, removed_sequence + 1)
    assert result.events_checked < CHAIN_LENGTH


def test_the_hash_is_stable_across_equivalent_timezone_representations() -> None:
    """
    The same instant must hash the same however its offset is written.

    Otherwise verification would depend on what timezone the machine recomputing
    it happened to be in, and a ledger written on an IST laptop would "fail" on a
    UTC server -- an unfalsifiable audit trail, which is worse than none, because
    a real tampering alert would be indistinguishable from the daily noise.
    """
    fields = {
        "sequence": 7,
        "prev_hash": "a" * 64,
        "event_type": AuditEventType.APPROVAL_GRANTED.value,
        "actor_type": ActorType.HUMAN.value,
        "actor_id": "ops@merchant.test",
        "case_id": "case_under_test",
        "payment_id": "pay_under_test",
        "summary": "Approved by ops@merchant.test",
        "payload": {"note": "looks legitimate"},
    }

    assert INSTANT_IST == INSTANT_UTC  # same instant, different written offset
    assert compute_event_hash(**fields, created_at=INSTANT_IST) == compute_event_hash(
        **fields, created_at=INSTANT_UTC
    )

    # ...and the normalisation must not have been achieved by ignoring the input.
    # A hash that discards fields would satisfy the assertion above trivially.
    moved = {**fields, "payload": {"note": "looks legitimate", "amount_paise": 1}}
    assert compute_event_hash(**moved, created_at=INSTANT_UTC) != compute_event_hash(
        **fields, created_at=INSTANT_UTC
    )


def test_timestamps_survive_the_round_trip_as_aware_utc(db: Session) -> None:
    """
    The bug this catches: the audit ledger displayed events five and a half hours
    before they happened.

    SQLite has no timestamp type. It stores datetimes as text without an offset,
    so a value written as ``...20:00:33+00:00`` reads back *naive* -- the instant
    is right, but the fact that it is UTC is gone, and ``DateTime(timezone=True)``
    does not prevent that. A naive datetime then serialises to JSON with no
    ``Z``, and a browser parses an offset-less string as **local** time: the
    ledger rendered a 20:00 UTC event as "20:00 IST", putting it on the wrong
    date and disagreeing with the same event's timestamp on every other screen.

    ``UtcDateTime`` re-attaches UTC on load. This test asserts the property at the
    ORM boundary rather than through the API, because that is where it is
    actually enforced and where a future column added with a plain
    ``DateTime(timezone=True)`` would fail it.
    """
    ledger = AuditLedger(db)
    event = ledger.record(
        event_type=AuditEventType.PAYMENT_FAILED,
        actor_type=ActorType.SYSTEM,
        summary="A payment failed.",
    )
    db.commit()

    # Expire everything so the value has to come back through the database
    # rather than out of the identity map, which would still hold the aware
    # object that was written and hide the defect entirely.
    db.expire_all()
    reloaded = db.get(AuditEvent, event.id)
    assert reloaded is not None

    assert reloaded.created_at.tzinfo is not None, (
        "Timestamp came back naive; a browser will read it as local time."
    )
    assert reloaded.created_at.utcoffset() == timedelta(0)


def test_concurrent_writers_do_not_collide_on_the_sequence() -> None:
    """
    The bug this catches: a double-clicked Approve returned a raw HTTP 500.

    ``record()`` allocates the next sequence with a read-then-insert. FastAPI
    runs synchronous endpoints on a worker threadpool and each request gets its
    own session, so two simultaneous state changes could read the same maximum
    and the second violated the unique constraint. ``IntegrityError`` is not a
    ``RecoverAIError``, so it escaped every handler and Starlette returned
    unstyled plain text -- leaving an operator unable to tell whether their
    approval had gone through. Measured before the fix: 11 of 40 concurrent
    writes succeeded.

    Every write path in the application goes through the ledger, so this is not
    specific to approvals; any two simultaneous state-changing requests were
    exposed.

    Uses its own file-backed database rather than the in-memory fixture, because
    the shared ``StaticPool`` connection the test suite uses would serialise the
    writers and hide exactly the thing under test.
    """
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp).as_posix()}/ledger.db")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        failures: list[str] = []
        barrier = threading.Barrier(WRITER_COUNT)

        def writer(index: int) -> None:
            db = Session()
            try:
                # Release all writers at the same instant; staggered starts would
                # let each one commit before the next reads, which is precisely
                # the race not happening.
                barrier.wait(timeout=10)
                AuditLedger(db).record(
                    event_type=AuditEventType.PAYMENT_FAILED,
                    actor_type=ActorType.SYSTEM,
                    summary=f"concurrent writer {index}",
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001 - the failure type is the finding
                db.rollback()
                failures.append(f"{type(exc).__name__}: {exc}")
            finally:
                db.close()

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(WRITER_COUNT)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not failures, f"concurrent writes failed: {failures}"

        db = Session()
        try:
            sequences = sorted(event.sequence for event in db.query(AuditEvent).all())
            assert sequences == list(range(1, WRITER_COUNT + 1)), (
                f"sequence is not gapless: {sequences}"
            )
            assert AuditLedger(db).verify_chain().valid
        finally:
            db.close()
            engine.dispose()
