"""
The append-only, hash-chained audit ledger.

This is the "audit trail proves" pillar of the product. Every other subsystem
makes a claim -- the agent claims it reasoned, the policy engine claims it
checked, the gateway claims it charged. This module is the only one that makes a
claim a reviewer can *falsify* without trusting us.

How the chain works
-------------------
Each ``AuditEvent`` row stores ``prev_hash`` (the hash of the row before it) and
``hash`` (a SHA-256 over its own content *including* ``prev_hash``). The first
row links to ``GENESIS_HASH``. Because each hash covers the previous hash, the
chain is a Merkle-style linked list: editing the payload of event 7 changes
event 7's hash, which no longer matches event 8's ``prev_hash``, and every event
after it is invalidated too. There is no way to rewrite one entry quietly.

What this does and does not defend against
------------------------------------------
It makes tampering **evident**, not impossible. Someone with write access to the
SQLite file could delete the whole table, or recompute every hash from a forged
event 7 onwards -- the algorithm is in this file, after all. What they cannot do
is change one row and leave the rest consistent, which is precisely the shape
that "helpful" edits, buggy migrations and accidental double-writes take. A
production deployment would additionally publish the head hash somewhere the
database operator cannot reach (a second service, a notary, a chain); adding
that here would be architecture theatre for a single-machine demo.

Cryptographic signatures were the alternative considered. They would prove
*authorship* as well as integrity, but they need a private key to be kept
somewhere, and this project's hard requirement is that it runs with zero
credentials. A hash chain gives the property that actually matters here --
history cannot be edited undetectably -- with no key management at all.
"""

from __future__ import annotations

import hashlib
import json
import threading
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, utcnow
from app.domain.enums import ActorType, AuditEventType
from app.domain.schemas import AuditChainVerificationOut

logger = logging.getLogger(__name__)

#: Serialises sequence allocation within this process.
#:
#: A module-level lock rather than a per-instance one: a fresh ``AuditLedger`` is
#: constructed per request, so an instance attribute would serialise nothing.
_SEQUENCE_LOCK = threading.Lock()

#: How many times to retry after losing a race for a sequence number. Collisions
#: are resolved by re-reading the maximum, so a couple of attempts is ample; the
#: bound exists so a genuine constraint problem surfaces instead of spinning.
_MAX_SEQUENCE_ATTEMPTS = 4

#: The ``prev_hash`` of the very first event. Sixty-four zeros is the same
#: convention Bitcoin uses for its genesis block, and it is self-evidently not a
#: real SHA-256 output, so nobody can mistake it for a truncated chain.
GENESIS_HASH: str = "0" * 64

#: Sequence numbers start here. Starting at 1 rather than 0 means "the ledger has
#: N events" and "the head sequence is N" are the same number, which removes an
#: off-by-one from every verification message a human has to read.
FIRST_SEQUENCE: int = 1


def _iso_utc(value: datetime) -> str:
    """
    Render a timestamp as an unambiguous UTC ISO-8601 string for hashing.

    Args:
        value: A timestamp, aware or naive.

    Returns:
        ISO-8601 text normalised to UTC.

    This function is the single trickiest line in the audit system, and it exists
    because of two traps that both produce the same symptom -- a chain that
    verifies on one machine and reports tampering on another.

    **Trap one: local time.** ``datetime.isoformat()`` on an aware timestamp
    keeps whatever offset it carries, so the same instant hashes differently in
    IST and in UTC. Normalising with ``astimezone(timezone.utc)`` first makes the
    hash a property of the *instant*, not of the server's regional settings.

    **Trap two: SQLite has no timezone type.** Columns declared
    ``DateTime(timezone=True)`` are written as aware UTC but read back **naive**.
    Calling ``astimezone()`` on a naive value makes Python assume it is local
    time, which would shift it by the machine's UTC offset and break the
    recomputed hash of every event ever written on a non-UTC laptop. Since the
    only writer is ``models.utcnow()``, a naive value read back can only ever be
    UTC, so it is stamped as such before conversion.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def compute_event_hash(
    *,
    sequence: int,
    prev_hash: str,
    event_type: str,
    actor_type: str,
    actor_id: str,
    case_id: str | None,
    payment_id: str | None,
    summary: str,
    payload: dict[str, Any],
    created_at: datetime,
) -> str:
    """
    Compute the SHA-256 digest that seals one ledger entry.

    Args:
        sequence: Position in the chain, 1-based.
        prev_hash: Hash of the preceding event, or ``GENESIS_HASH`` for the first.
        event_type: ``AuditEventType`` value.
        actor_type: ``ActorType`` value -- who caused this.
        actor_id: Identity of the actor (operator name, ``"system"``, ``"agent"``).
        case_id: Recovery case this event belongs to, if any.
        payment_id: Payment this event belongs to, if any.
        summary: One-line human description.
        payload: Structured detail. Must already be JSON-round-trippable; see
            ``AuditLedger.record`` for where that is guaranteed.
        created_at: When the event happened.

    Returns:
        A 64-character lowercase hex digest.

    Every field that a reader would rely on is inside the digest -- including
    ``sequence`` and ``prev_hash``, which is what binds this row to its
    predecessor rather than merely to itself.

    The serialisation options are load-bearing, not stylistic:

    *   ``sort_keys=True`` -- Python preserves dict insertion order, so two
        semantically identical payloads built by different code paths would
        otherwise hash differently.
    *   ``separators=(",", ":")`` -- pins the exact byte layout. The default
        separators include spaces, and a future version of ``json`` changing its
        whitespace would invalidate every historical hash.
    *   ``default=str`` -- a last-resort coercion so a stray ``Decimal`` or
        ``UUID`` in a payload degrades to a stable string instead of raising
        mid-transaction. It is a safety net, not the plan: ``record()``
        normalises payloads before they ever reach this function.
    """
    canonical: dict[str, Any] = {
        "sequence": sequence,
        "prev_hash": prev_hash,
        "event_type": event_type,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "case_id": case_id,
        "payment_id": payment_id,
        "summary": summary,
        "payload": payload,
        "created_at": _iso_utc(created_at),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AuditLedger:
    """
    Reader/writer for the hash chain, scoped to one database session.

    Constructed per unit of work rather than held as a long-lived singleton,
    because an audit entry must commit in the *same* transaction as the state
    change it describes. Sharing one ledger object across requests would mean
    sharing a session, and a state change could then be committed while its audit
    record sat in another request's pending transaction.
    """

    def __init__(self, db: Session) -> None:
        """
        Args:
            db: The session owned by the caller. The ledger reads and writes
                through it but never commits it.
        """
        self.db = db

    # -- Writing ----------------------------------------------------------

    def record(
        self,
        *,
        event_type: AuditEventType,
        actor_type: ActorType,
        summary: str,
        actor_id: str = "system",
        payload: dict[str, Any] | None = None,
        case_id: str | None = None,
        payment_id: str | None = None,
    ) -> AuditEvent:
        """
        Append one event to the ledger.

        Args:
            event_type: What happened, from the closed ``AuditEventType`` set.
            actor_type: Whether the AI, a human, the system or a webhook did it.
            summary: One line, written for a merchant operator rather than a
                developer. This is what the timeline in the UI renders.
            actor_id: Identity of the actor. Defaults to ``"system"`` for
                internal transitions such as the expiry sweep.
            payload: Structured detail -- rule outputs, gateway ids, model
                scores. This is the part that is hashed and therefore the part
                that cannot be quietly rewritten later.
            case_id: Recovery case this belongs to, if any.
            payment_id: Payment this belongs to, if any.

        Returns:
            The persisted (flushed, not committed) ``AuditEvent``.

        **This method does not commit, and that is the most important thing
        about it.** The caller owns the transaction. A recovery approval writes a
        status change, an attempt row, a gateway order id and four audit events;
        committing the audit entry independently would make it possible to have a
        ledger that says "order created" when no order row exists, or a state
        change with no ledger entry at all. Flushing (rather than committing)
        gets the row into the transaction so the unique constraint on
        ``sequence`` fires immediately, while leaving atomicity in the hands of
        the service that knows where the unit of work ends.
        """
        # Normalise the payload once, and store *exactly* what gets hashed.
        # Without this, a payload containing a datetime would be hashed via
        # ``default=str`` but would raise when SQLAlchemy serialised the JSON
        # column -- and, worse, a payload containing integer dict keys would be
        # stored as strings and then re-hash to a different digest on
        # verification. Hashing the normalised form makes stored == hashed by
        # construction.
        safe_payload: dict[str, Any] = json.loads(
            json.dumps(payload or {}, sort_keys=True, default=str)
        )

        # Allocation and insert happen under a process-wide lock, and a lost race
        # is retried.
        #
        # The allocation is a read-then-insert: SELECT max(sequence), then INSERT
        # max + 1. FastAPI runs synchronous endpoints on a worker threadpool and
        # every request gets its own session, so two simultaneous state changes
        # can read the same maximum and the second violates the unique constraint
        # on ``sequence``. That surfaced as a raw HTTP 500 -- an operator who
        # double-clicked Approve got an unstyled error with no way to tell whether
        # the recovery had gone through.
        #
        # ``_SEQUENCE_LOCK`` serialises the common case within this process. It is
        # not sufficient on its own: SQLite gives each connection its own snapshot,
        # so a thread that has flushed but not yet committed is invisible to the
        # next reader, and the lock cannot be held across the caller's commit
        # without making the whole service single-threaded. The retry is what
        # closes that window, and the unique constraint is what guarantees a lost
        # race is loud rather than silent.
        for attempt in range(_MAX_SEQUENCE_ATTEMPTS):
            try:
                with _SEQUENCE_LOCK, self.db.begin_nested():
                    return self._insert(
                        event_type=event_type,
                        actor_type=actor_type,
                        actor_id=actor_id,
                        summary=summary,
                        safe_payload=safe_payload,
                        case_id=case_id,
                        payment_id=payment_id,
                    )
            except IntegrityError:
                # Roll back only this INSERT, not the caller's whole transaction:
                # the state change this event describes must survive. `flush()`
                # inside a SAVEPOINT is what makes that possible.
                if attempt == _MAX_SEQUENCE_ATTEMPTS - 1:
                    raise
                logger.warning(
                    "Audit sequence collision on attempt %s; retrying.", attempt + 1
                )

        raise AssertionError("unreachable: the loop above either returns or raises")

    def _insert(
        self,
        *,
        event_type: AuditEventType,
        actor_type: ActorType,
        actor_id: str,
        summary: str,
        safe_payload: dict[str, Any],
        case_id: str | None,
        payment_id: str | None,
    ) -> AuditEvent:
        """
        Allocate the next sequence and insert one event.

        Called with ``_SEQUENCE_LOCK`` held and inside a savepoint, so a lost race
        rolls back this INSERT alone and leaves the caller's transaction -- the
        state change this event describes -- intact.
        """
        # The highest sequence currently in the ledger. ``scalar()`` returns None
        # on an empty table, which is the genesis case.
        last_sequence: int = (
            self.db.execute(select(func.max(AuditEvent.sequence))).scalar() or 0
        )

        if last_sequence == 0:
            prev_hash = GENESIS_HASH
            sequence = FIRST_SEQUENCE
        else:
            # Fetch the predecessor's hash by the sequence we just read, rather
            # than by a separate "latest row" query. Two independent queries
            # could disagree if a row landed between them; keyed off one number,
            # they cannot.
            prev_hash = self.db.execute(
                select(AuditEvent.hash).where(AuditEvent.sequence == last_sequence)
            ).scalar_one()
            sequence = last_sequence + 1

        # Stamp the timestamp here instead of letting the column default fire.
        # The hash covers ``created_at``, so we must know the exact value that
        # will be stored; a default assigned later by the ORM would produce a row
        # whose stored timestamp does not match the one that was hashed, and the
        # chain would fail verification the moment it was written.
        created_at = utcnow()

        event = AuditEvent(
            sequence=sequence,
            case_id=case_id,
            payment_id=payment_id,
            # ``.value`` is explicit rather than relying on the str-enum mixin:
            # ``str(AuditEventType.PAYMENT_FAILED)`` renders as
            # "AuditEventType.PAYMENT_FAILED" on modern Python, and a hash input
            # that changes with the interpreter version is a trap worth closing.
            event_type=event_type.value,
            actor_type=actor_type.value,
            actor_id=actor_id,
            summary=summary,
            payload=safe_payload,
            prev_hash=prev_hash,
            created_at=created_at,
        )
        event.hash = compute_event_hash(
            sequence=sequence,
            prev_hash=prev_hash,
            event_type=event_type.value,
            actor_type=actor_type.value,
            actor_id=actor_id,
            case_id=case_id,
            payment_id=payment_id,
            summary=summary,
            payload=safe_payload,
            created_at=created_at,
        )

        self.db.add(event)
        # Flush, never commit. Sends the INSERT so that the unique constraints on
        # ``sequence`` and ``hash`` are checked now -- the database, not this
        # code, is the final authority that the chain has no forks.
        self.db.flush()

        logger.debug(
            "Audit event %s recorded at sequence %s (case=%s)",
            event_type.value,
            sequence,
            case_id,
        )
        return event

    # -- Reading ----------------------------------------------------------

    def head(self) -> AuditEvent | None:
        """
        Return the most recent event, or ``None`` when the ledger is empty.

        Returns:
            The event with the highest sequence number.

        The head hash is the single value that summarises the entire history: if
        you recorded it yesterday and it still matches today, nothing before it
        has been altered.
        """
        return self.db.execute(
            select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1)
        ).scalar_one_or_none()

    def verify_chain(self) -> AuditChainVerificationOut:
        """
        Recompute the whole chain from genesis and report whether it holds.

        Returns:
            ``AuditChainVerificationOut`` with ``valid``, how many events were
            examined, the head hash when the chain is intact, and the sequence
            number of the first broken link when it is not.

        Three independent properties are checked, because tampering can take
        three different shapes:

        1.  **Content integrity** -- recomputing each event's hash from its
            stored fields must reproduce its stored hash. Catches an edited
            payload, summary, actor or timestamp.
        2.  **Linkage** -- each event's ``prev_hash`` must equal the previous
            event's stored hash. Catches a row spliced into the middle, or a
            re-ordering.
        3.  **Gaplessness** -- sequences must run 1, 2, 3, ... with no holes.
            This is the check that catches a *deletion*. The first two properties
            alone would not: remove the last event and the remaining chain is
            still perfectly self-consistent. A gap is not a symptom of tampering,
            it *is* the evidence, because the ledger's own writer can never
            produce one.

        Verification stops at the first break. Everything after a broken link is
        untrustworthy by definition, so listing further failures would be noise;
        and ``head_hash`` is reported as ``None`` in that case, because a chain
        with a hole in it has no head anyone should rely on.
        """
        events = list(
            self.db.execute(select(AuditEvent).order_by(AuditEvent.sequence.asc())).scalars()
        )

        if not events:
            # An empty ledger is vacuously valid. Reporting it as invalid would
            # make a fresh install look compromised, which is both wrong and the
            # kind of false alarm that trains people to ignore the real one.
            return AuditChainVerificationOut(
                valid=True,
                events_checked=0,
                head_hash=None,
                broken_at_sequence=None,
                message="Audit ledger is empty. Nothing to verify.",
            )

        expected_prev = GENESIS_HASH
        expected_sequence = FIRST_SEQUENCE

        for index, event in enumerate(events, start=1):
            if event.sequence != expected_sequence:
                return AuditChainVerificationOut(
                    valid=False,
                    events_checked=index,
                    head_hash=None,
                    broken_at_sequence=event.sequence,
                    message=(
                        f"Sequence gap detected: expected event {expected_sequence} "
                        f"but found {event.sequence}. An event has been deleted or "
                        f"inserted out of order."
                    ),
                )

            if event.prev_hash != expected_prev:
                return AuditChainVerificationOut(
                    valid=False,
                    events_checked=index,
                    head_hash=None,
                    broken_at_sequence=event.sequence,
                    message=(
                        f"Broken link at event {event.sequence}: its recorded "
                        f"previous hash does not match the hash of event "
                        f"{event.sequence - 1}."
                    ),
                )

            recomputed = compute_event_hash(
                sequence=event.sequence,
                prev_hash=event.prev_hash,
                event_type=event.event_type,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                case_id=event.case_id,
                payment_id=event.payment_id,
                summary=event.summary,
                payload=event.payload or {},
                created_at=event.created_at,
            )
            if recomputed != event.hash:
                return AuditChainVerificationOut(
                    valid=False,
                    events_checked=index,
                    head_hash=None,
                    broken_at_sequence=event.sequence,
                    message=(
                        f"Content tampering detected at event {event.sequence}: "
                        f"its stored hash does not match a hash recomputed from "
                        f"its own contents."
                    ),
                )

            expected_prev = event.hash
            expected_sequence += 1

        return AuditChainVerificationOut(
            valid=True,
            events_checked=len(events),
            head_hash=events[-1].hash,
            broken_at_sequence=None,
            message=(
                f"Audit chain intact: {len(events)} event(s) verified from genesis "
                f"to sequence {events[-1].sequence}."
            ),
        )
