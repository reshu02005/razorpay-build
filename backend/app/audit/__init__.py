"""
The tamper-evident audit ledger.

This package exists to turn "trust our logs" into "verify our logs". Every event
commits to the hash of its predecessor, so altering or deleting any historical
row invalidates every hash after it and ``GET /api/audit/verify`` reports exactly
where the chain broke. For a system that lets an AI recommend financial actions,
an inspectable history is not a nice-to-have -- it is the evidence.
"""
