"""
The audit ledger, read side.

The ledger is append-only and hash-chained: every row commits to its
predecessor's hash, so editing or deleting history breaks every hash after it.
There is no write endpoint here, and that absence is deliberate -- events are
appended by the code that actually did the thing, never by a client.

``GET /api/audit/verify`` is what turns the immutability claim from a sentence in
a README into something a reviewer can check: it recomputes every hash from
genesis and names the first sequence number that fails.

Every route declares ``response_model=`` and ``summary=`` so ``/docs`` reads as
the API reference for this service.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import AuditLedgerDep, DbSession, list_audit_events
from app.domain.schemas import AuditChainVerificationOut, AuditEventOut

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get(
    "",
    response_model=list[AuditEventOut],
    summary="Read the audit ledger",
    description="Newest first. Pass `case_id` to get the complete life story of one recovery "
    "case: classification, propensity score, guardrail verdict, who approved it and when, the "
    "order that was created, and how it ended.",
)
def list_events(
    db: DbSession,
    case_id: str | None = Query(
        default=None,
        description="Restrict to events belonging to one recovery case.",
    ),
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum events to return."),
) -> list[AuditEventOut]:
    """Return a page of audit events, optionally scoped to one case."""
    return list_audit_events(db, case_id=case_id, limit=limit)


@router.get(
    "/verify",
    response_model=AuditChainVerificationOut,
    summary="Verify the hash chain from genesis",
    description="Recomputes the SHA-256 of every event in sequence order and compares it with "
    "the stored hash. Returns `valid: false` and the exact `broken_at_sequence` if any row was "
    "altered, inserted or removed.",
)
def verify_chain(ledger: AuditLedgerDep) -> AuditChainVerificationOut:
    """Recompute the ledger's hash chain and report whether it holds."""
    return ledger.verify_chain()
