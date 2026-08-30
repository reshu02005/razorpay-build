# Architecture

---

## The idea in one paragraph

A payment fails. An AI agent works out *why*, using read-only tools and a failure taxonomy
built from Razorpay's real error vocabulary, and recommends a recovery strategy. A
deterministic policy engine — not the AI — decides whether that recommendation may be acted
on. A human approves. Razorpay executes. Every step is written to a hash-chained ledger that
anyone can verify. The AI is the reasoning layer; it is not, at any point, an authority over
money.

---

## System shape

```
┌──────────────────────────────────────────────────────────────────────┐
│  Next.js console (browser)                                           │
│    /  ·  /payments/[id]  ·  /recovery/[id]  ·  /checkout/[id]         │
│    /audit  ·  /policy                                                │
└───────────────────────────┬──────────────────────────────────────────┘
                            │  HTTP + JSON (CORS)
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│  FastAPI                                                             │
│                                                                      │
│   api/routers/       thin: parse → delegate → return                 │
│         │                                                            │
│   services/          business logic · owns the transaction           │
│         │                                                            │
│    ┌────┴──────┬───────────┬───────────┬───────────┐                 │
│    ▼           ▼           ▼           ▼           ▼                 │
│  agent/     policy/       ml/      payments/    audit/               │
│  reasons    decides     scores     executes     records              │
│    │           │           │           │           │                 │
│    └───────────┴───────────┴─────┬─────┴───────────┘                 │
│                                  ▼                                   │
│                             db/  SQLAlchemy 2.0                      │
└──────────────────────────────────┬───────────────────────────────────┘
                                   ▼
                    SQLite            Gemini API        Razorpay API
                 (local file)      (optional)         (optional, Test Mode)
```

Both external services are optional. Without them the agent uses a deterministic planner and
the gateway is simulated in-process — see [D7 and D6](08-DESIGN-DECISIONS.md).

---

## Layering, and the rule that keeps it honest

| Layer | May depend on | Must never |
|---|---|---|
| `api/routers` | services, schemas | touch the ORM, decide policy, hold business logic |
| `api/deps.py` | services, schemas, ORM *(see note)* | hold business logic |
| `services` | everything below | know it is being called over HTTP |
| `agent` | policy (read-only), ml, db (read) | write money-moving state |
| `policy` | domain, config | perform I/O of any kind |
| `ml` | domain | know what a Payment is (it takes a feature dict) |
| `payments` | domain, config | know about recovery cases |
| `audit` | db, domain | commit a transaction |
| `domain` | nothing | import from any layer above |

One documented exception: `api/deps.py` runs two direct ORM reads — fetching one
customer, and listing audit events. Both are single queries with no business rules, and
wrapping each in a service class would add a layer that does nothing but forward. The line is
"no decisions in the API layer", not "no SQL"; anything that branches on policy, moves state
or touches money goes through a service without exception.

The rule that does the most work: **services own the transaction.** The audit ledger and the
agent orchestrator `flush()` but never `commit()`. Each public service method commits once,
at the end. So a state change and the audit entry describing it are atomic — you get both,
or neither. An audit trail that can disagree with reality is worse than no audit trail,
because it invites you to trust it.

---

## Data model

```
Customer ──1:N──► Payment ──1:1──► RecoveryCase ──1:N──► RecoveryAttempt
                     ▲                    │                     │
                     └──── parent_payment_id                    │
                     (a recovery payment is also a Payment)      │
                                          │                     │
                                          └──── AuditEvent ◄─────┘
                                          └──── AgentToolCall
```

| Table | Holds |
|---|---|
| `customers` | Identity, risk flag, and denormalised counters (`total_payments`, `successful_payments`) that the ML model reads at inference time |
| `payments` | Every attempt, successful or failed, including recovery attempts |
| `recovery_cases` | The decision record: what the AI concluded, what the guardrails ruled, who approved, how it ended |
| `recovery_attempts` | One concrete try at collecting — a Razorpay order plus its outcome |
| `audit_events` | Append-only, hash-chained ledger |
| `agent_tool_calls` | Every step of the agent's reasoning, with arguments, results and latency |

Three modelling choices worth defending:

**A recovery payment is a `Payment` row, not a separate table.** It carries
`is_recovery_attempt=True` and `parent_payment_id`. This means merchant revenue reporting
sums one table and structurally cannot omit recovered money.

**Cases and attempts are separate.** A case is the *reasoning*; an attempt is the *try*. The
`max_recovery_attempts` guardrail counts attempts inside a case, so an attempt can fail
without destroying the analysis that produced it — and re-proposing lands on the same case,
so the attempt budget carries over rather than resetting.

**Denormalised customer counters.** The propensity model needs a customer's historic success
rate at inference time. Recomputing it with a `COUNT` on every prediction would put a table
scan in the hot path of a payment flow.

Money is **integer paise** in every column. Timestamps are **timezone-aware UTC**, because
the guardrails do date arithmetic and a naive local timestamp would make cooldowns and daily
budgets behave differently in IST than in UTC.

---

## The recovery state machine

There is exactly one lifecycle for a recovery, declared as data in
`domain/enums.py::ALLOWED_TRANSITIONS` and enforced centrally by
`RecoveryService._transition`.

```
                    PROPOSED
                       │
      ┌────────────────┼────────────────┬──────────────┐
      ▼                ▼                ▼              ▼
   BLOCKED       AWAITING_APPROVAL   NO_ACTION     ESCALATED
  (guardrail          │              (agent:        (agent:
   said no)           │             nothing to     needs a
                      │              be done)       human)
           ┌──────────┼──────────┐
           ▼          ▼          ▼
       REJECTED   APPROVED    EXPIRED
                      │
                      ▼
                  EXECUTING ──────────► FAILED ───► (re-propose,
                      │                    ▲         if R1 allows)
                      ▼                    │
              AWAITING_PAYMENT ────────────┤
                      │                    │
                      ├──► RECOVERED       │
                      └──► EXPIRED ────────┘
```

**Three different terminal states mean "no money will move", and they are deliberately
distinct.** `BLOCKED` is a guardrail refusal. `NO_ACTION` is the agent judging that doing
nothing is correct — the right answer for `insufficient_funds`, where the instrument works
and the balance does not. `ESCALATED` means a human must handle it off-platform. A merchant
filtering for "cases the automation refused" must not have to string-match a reason field,
and metrics that lumped these together would hide whether the guardrails are too tight or
the agent is too cautious.

`FAILED` is the only non-terminal failure: it can return to `PROPOSED`. The attempt limit,
not the state machine, is what stops that becoming a loop.

---

## Request flow: analysing a failed payment

`POST /api/recovery/payments/{id}/analyze`

1. **Router** parses and delegates. That is all it does.
2. **Service** loads the payment; rejects anything not in `failed` status — you cannot
   recover money that was collected.
3. **Agent** runs. It classifies the failure from the taxonomy, pulls customer history,
   scores recovery propensity with the ML model, reads the policy, dry-runs the guardrails,
   and submits an `AgentRecoveryPlan`. Every tool call is persisted.
4. **Service** builds a `GuardrailContext` — with real queries for today's recovery total,
   the customer's case count, the last attempt time, whether an order is already open.
5. **Policy engine** evaluates all thirteen rules and returns a verdict.
6. **Service** routes the case to `NO_ACTION`, `ESCALATED`, `BLOCKED` or
   `AWAITING_APPROVAL`, freezes the policy snapshot onto the case, writes the audit events,
   and commits **once**.

## Request flow: approving

`POST /api/recovery/cases/{id}/approve`

1. Assert the case is `AWAITING_APPROVAL`.
2. **Re-evaluate every guardrail against live state.** A proposal is a snapshot of a world
   that keeps moving; the binding check is the one at the moment money would move.
3. On `DENY` → `BLOCKED`, audit, and return the refusal with the rule that fired.
4. `APPROVED` → `EXECUTING`. Look up or create the `RecoveryAttempt` by the deterministic
   idempotency key `case_id:attempt_number` — a double-clicked Approve button is not a
   hypothetical.
5. Create the Razorpay order with `amount_paise` **read from the payment**, never from the
   plan.
6. `AWAITING_PAYMENT`, set the expiry, audit the order and the link, commit once.

---

## The audit ledger

Each `AuditEvent` stores a SHA-256 hash over its own canonical content **plus the previous
event's hash**. Sequence numbers must be gapless.

```
seq 1 ── hash(content₁ + GENESIS)  ─┐
seq 2 ── hash(content₂ + hash₁)    ◄┘ ─┐
seq 3 ── hash(content₃ + hash₂)       ◄┘
```

Editing event 2 changes `hash₂`, which invalidates event 3, and every event after it.
`GET /api/audit/verify` recomputes the whole chain and names the first sequence that fails.

Two details that matter more than they look:

- Timestamps are serialised as UTC ISO-8601 before hashing, so the same instant expressed in
  `+05:30` and in `Z` produces the identical hash. Without that, a server timezone change
  would appear to be tampering.
- The ledger flushes but never commits, for the atomicity reason above.

This is not a blockchain and does not pretend to be. There is one writer and one operator;
the problem is *detecting* quiet edits, not achieving distributed consensus. See
[D5](08-DESIGN-DECISIONS.md#d5-the-audit-log-is-a-hash-chain-not-a-table-of-rows).

---

## Degradation, by design

| Subsystem | Primary | Fallback | How you know |
|---|---|---|---|
| Reasoning | Google Gemini, function calling | Deterministic planner over the same taxonomy, playbook and ML score | `AgentMode` badge in the UI, `degraded_reason` in the API |
| Gateway | Razorpay Orders API (Test Mode) | In-process simulator with **real HMAC signatures** | `GatewayMode` banner |
| Propensity | Trained scikit-learn pipeline | Documented heuristic over the same base rates | `is_fallback` flag, "Heuristic estimate" marker |

Every fallback is labelled where it appears. A demo that silently pretends to charge a card
is misleading, and saying so out loud is what makes the rest of the numbers on screen
credible.

The Gemini fallback is a resilience property first and a demo convenience second: a payment
recovery pipeline that stops working when a third-party model API has an outage is not a
payments pipeline.

---

## Frontend

Next.js 15 App Router, TypeScript strict, Tailwind, shadcn-style components vendored as
owned source.

- `src/lib/api.ts` is the **only** module that calls `fetch`. Errors are parsed from the
  uniform `ErrorOut` envelope into a typed `ApiRequestError`.
- `src/lib/types.ts` hand-mirrors the Pydantic schemas. Label maps are typed
  `Record<Union, string>`, so adding a backend enum member without a display label becomes a
  compile error.
- Zod validates critical responses at the boundary, which is where schema drift actually
  bites.
- `can_approve`, `can_reject` and `approval_blocked_reason` are computed **server-side** and
  read by the UI. Deriving them in React would fork the policy into a second implementation
  — and the one users see would be the wrong one.

---

## What is deliberately not here

| | Why |
|---|---|
| Authentication | The approver's identity is a recorded string. Real SSO is a substantial subsystem that would demonstrate nothing about the thesis. The audit trail records whatever identity it is given, so adding SSO changes where the string comes from and nothing else. |
| A job scheduler | `RETRY_LATER` parks the case rather than scheduling it. The *judgment* — "do not retry this in thirty seconds, the balance has not changed" — is the valuable part and is fully implemented. |
| Multi-tenancy / multi-currency | Every guardrail limit is denominated in paise. Multi-currency needs per-currency limits, which is a different design. |
| A message queue | Analysis is synchronous and takes seconds. Adding a broker would add a runtime to start and a failure mode to debug, for no benefit at this scale. |

Each of these is listed in the README's limitations section too, because a project that
claims no weaknesses invites the reviewer to go looking for them.
