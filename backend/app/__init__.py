"""
RecoverAI -- an AI revenue-recovery agent for failed Razorpay payments.

The product in one line: *AI decides, guardrails control, Razorpay executes, the
audit trail proves.* Those four verbs map onto four package boundaries, and the
boundaries are the whole design:

*   ``app.agent``     -- decides. Classifies the failure and proposes a strategy.
                         It has no tool that can move money.
*   ``app.policy``    -- controls. A deterministic rule engine that can veto the
                         agent but can never be talked into permitting more.
*   ``app.payments``  -- executes. The only code that talks to a payment gateway,
                         reached only after an explicit human approval.
*   ``app.audit``     -- proves. An append-only, hash-chained ledger that makes
                         "we logged it" checkable rather than merely claimed.

Two operational promises hold across every module:

*   **Zero credentials required.** With no ``GEMINI_API_KEY`` the agent falls back
    to a deterministic planner; with no ``RAZORPAY_*`` keys the gateway is an
    in-process simulator. Both degradations are labelled in the API and the UI --
    a demo that quietly pretends to be live would be worse than one that admits
    it is simulated.
*   **Money is integer paise.** Rupees exist only in response builders. Binary
    floats cannot represent 0.10 exactly, and a rounding error in a payments
    system is a defect, not a curiosity.
"""

from __future__ import annotations

#: Reported by ``GET /api/status`` so a screenshot of the running app is always
#: traceable to a specific build of the code.
__version__ = "1.0.0"
