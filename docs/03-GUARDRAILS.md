# Guardrails

> The AI recommends. These rules decide.

This is the layer the project exists to demonstrate. An agent that reasons well about
payments is useful; an agent that reasons well *and cannot act unilaterally on money* is
deployable. Everything here is deterministic, server-side, and independent of whatever the
model concluded.

---

## Where the guardrails sit

```
Agent produces AgentRecoveryPlan          ← a recommendation, nothing more
        │
        ▼
PolicyEngine.evaluate(GuardrailContext)   ← 13 pure functions
        │
        ├── DENY ─────────────► case BLOCKED. No order. Audited.
        ├── REQUIRE_APPROVAL ─► case AWAITING_APPROVAL. A human must click.
        └── ALLOW ────────────► (only reachable with the auto-approve lane on; ships off)
                │
                ▼
        Human clicks Approve
                │
                ▼
PolicyEngine.evaluate(...) AGAIN          ← re-evaluated against live state
                │
                ▼
        Razorpay order created
```

The engine runs **twice**: once when the plan is proposed, and again at the instant of
approval. That second evaluation is the one that binds. See [why](#why-evaluate-twice).

---

## The thirteen rules

Rules are evaluated in order, and **every rule returns a result — including when it
passes.** The approval screen renders the full checklist, so an operator can see what was
checked, not only what failed.

| # | Rule | Fires when | Verdict |
|---|---|---|---|
| **R1** | Maximum recovery attempts | This would be attempt number > 2 | `DENY` |
| **R2** | Attempt cooldown | Less than 15 minutes since the last attempt | `DENY` |
| **R3** | Recoverable failure category | Category is `risk_blocked` or `unknown` | `DENY` |
| **R4** | Absolute amount ceiling | Amount > ₹50,000 | `DENY` |
| **R5** | High-value review | Amount ≥ ₹10,000 | `REQUIRE_APPROVAL` |
| **R6** | No duplicate open order | An attempt is already awaiting payment | `DENY` |
| **R7** | Daily recovery budget | Today's recovery volume + this amount > ₹2,00,000 | `DENY` |
| **R8** | Per-customer velocity | This customer already has 3 cases today | `DENY` |
| **R9** | Amount integrity | Recovery amount ≠ original payment amount | `DENY` |
| **R10** | Minimum success likelihood | ML propensity score < 0.15 | `DENY` |
| **R11** | Payment freshness | Original payment older than 7 days | `DENY` |
| **R12** | Customer risk flag | Customer is flagged by the merchant's risk process | `DENY` |
| **R13** | Explicit human approval | Always | `REQUIRE_APPROVAL` |

Every limit is configurable in `backend/.env` and exposed read-only at `GET /api/policy`.

### Aggregation: most restrictive wins

`DENY` > `REQUIRE_APPROVAL` > `ALLOW`. The verdict is the highest severity any rule
returned.

This ordering is what makes the engine safe to extend: **adding a rule can only ever make
the system more conservative, never less.** A contributor cannot accidentally widen the
system's authority by adding a check.

---

## Why each limit is where it is

**R1 — two attempts.** The second attempt captures most transient failures. The third
mostly annoys the customer and burns gateway calls for a diminishing return. Two is the
industry norm, and the number a merchant would recognise.

**R2 — fifteen minutes.** Long enough for an issuer-side transient fault to clear, short
enough that the customer still remembers the purchase. Its real job is preventing a retry
storm: without a cooldown, a bug in the approval path becomes a loop that hammers both the
gateway and the customer.

**R3 — fraud is never retried.** `risk_blocked` means an issuer or Razorpay's risk engine
flagged the transaction. Re-presenting it is at best a wasted call and at worst helps push a
stolen instrument through. `unknown` is excluded for a different reason: we cannot reason
safely about a failure we could not classify. Absence of evidence is not evidence of safety.

**R4 / R5 — two thresholds, not one.** ₹10,000 forces a human to look. ₹50,000 stops the
automated path entirely and escalates. Splitting them means a merchant can raise the review
threshold as they gain confidence without also raising the hard ceiling.

**R6 — duplicate prevention, in three layers.** This rule is the friendly one: it produces
an explanation. Behind it sit a deterministic idempotency key (`case_id:attempt_number`)
and a unique constraint in the database, so a double-clicked Approve button, a retried HTTP
request and a replayed webhook all converge on the same single order.

**R7 — a bounded blast radius.** If the agent, the data or an operator goes wrong, the
worst-case exposure in a day is a number known in advance. This is the rule that turns
"we tested it and it seemed fine" into a quantified risk statement.

**R8 — one customer, three chases.** Protects the customer experience, not the merchant's
money. Someone having a bad day with their bank should not receive an escalating series of
payment requests.

**R9 — the AI cannot change the amount.** The recovery amount is copied from the original
payment. This rule is defence in depth over a property the schema already guarantees
(`AgentRecoveryPlan` has no amount field and forbids extra keys) — deliberately, because
this is the invariant an attacker or a subtle bug would target first.

**R10 — do not chase the hopeless.** Below a 15% predicted success probability, the
expected value of the attempt is outweighed by the customer friction it creates. The floor
is set low on purpose: this rule is meant to screen out the genuinely hopeless, not to
second-guess the agent. See [`05-ML-MODEL.md`](05-ML-MODEL.md) for how the score is produced
and why the model is tuned toward recall.

**R11 — seven days.** After a week, card details and purchase intent have both likely moved
on, and an unexpected payment request reads as suspicious rather than helpful.

**R12 — the merchant's own risk signal wins.** RecoverAI does not get a vote on a customer
the merchant has already flagged.

**R13 — a human approves every rupee.** The master switch. An auto-approve lane exists in
the code (low value *and* high predicted success) because a real merchant would eventually
want graduated autonomy — but it ships **off**, and the submitted demo keeps a human in the
loop for every rupee.

---

## Why evaluate twice

A proposal is a snapshot of a world that keeps moving.

Between the moment the agent proposes a recovery and the moment an operator clicks Approve,
any of these can change:

- other cases consume the **daily budget**
- another **attempt** is made on the same case
- the customer gets **risk-flagged**
- the recovery window **expires**
- the payment crosses the **freshness** limit

If approval trusted the stored verdict, all thirteen limits would be advisory. So
`RecoveryService.approve()` rebuilds the entire `GuardrailContext` from live state and
re-runs every rule. A case can be proposed as approvable and denied minutes later — and when
that happens, the operator sees the exact rule and the exact numbers that changed.

---

## Why the rules are pure functions

Every rule has the signature `(GuardrailContext) -> GuardrailEvaluation` and performs **no
database access and no I/O**. The service layer computes everything a rule could need —
today's recovery total, the attempt count, the last attempt time, whether an order is
already open — and passes it in.

Two consequences, both deliberate:

1. **The policy is exhaustively testable.** Thirteen rules with pass and fail cases each is
   a fast test file with no database and no fixtures. A financial control that is hard to
   test is a financial control nobody should trust.
2. **Evaluation cost is predictable.** The queries happen once, not thirteen times. Adding a
   rule can never introduce an N+1 into the approval path.

---

## The policy snapshot

Every case stores a frozen copy of the limits that were in force when it was decided, in
`policy_snapshot`.

Without it, changing a limit next month would silently rewrite the meaning of every decision
already made — an auditor reading a six-month-old case would see it judged against today's
numbers. With it, `/recovery/[caseId]` can always show the policy that actually applied.

---

## What the agent can and cannot see

| | |
|---|---|
| The agent **can** read the policy | via the read-only `get_recovery_policy` tool, so it reasons inside the real constraints instead of proposing things that will be refused |
| The agent **can** dry-run the guardrails | via `check_recovery_eligibility`, which previews the verdict |
| The agent **cannot** change the policy | there is no tool to write it, and `GET /api/policy` is read-only for everyone |
| The agent **cannot** act on a favourable preview | the dry run has no side effects; the binding evaluation runs server-side at approval |

A limit that the automated path can raise is not a limit. That is why the policy screen is
read-only by design rather than by omission.

---

## Threat model

What this design defends against, and what it does not.

| Threat | Defence |
|---|---|
| Prompt injection in an error description | The agent's output is a constrained enum-only schema; the amount is never taken from it; guardrails run regardless of what the model concluded |
| A jailbroken or hallucinating model | There is no tool that moves money, so no output can create an order |
| A model inventing a larger amount | `AgentRecoveryPlan` has no amount field, `extra="forbid"` rejects one, and R9 denies any mismatch |
| Double-charging a customer | Idempotency key + unique constraint + R6 |
| A runaway retry loop | R1 attempt cap + R2 cooldown + R7 daily budget |
| A forged "payment succeeded" callback | Server-side HMAC-SHA256 signature verification before anything is marked recovered |
| Quietly rewriting history | Hash-chained append-only ledger, verifiable at `GET /api/audit/verify` |
| An operator approving the wrong thing | Confirmation dialog restating amount, customer and strategy; approver identity recorded |
| **Not defended:** an authenticated operator who approves maliciously | Out of scope — mitigated by the audit trail, which records who approved what and when |
| **Not defended:** a compromised server | Out of scope for a demonstration project |

---

## Configuring the limits

All limits live in `backend/.env`. Money is in **paise** (100 paise = ₹1).

```ini
MAX_RECOVERY_ATTEMPTS=2
RECOVERY_COOLDOWN_SECONDS=900
HIGH_VALUE_REVIEW_THRESHOLD_PAISE=1000000     # Rs 10,000
MAX_RECOVERY_AMOUNT_PAISE=5000000             # Rs 50,000
DAILY_RECOVERY_BUDGET_PAISE=20000000          # Rs 2,00,000
MAX_CASES_PER_CUSTOMER_PER_DAY=3
MIN_PROPENSITY_SCORE=0.15
MAX_PAYMENT_AGE_HOURS=168                     # 7 days
REQUIRE_HUMAN_APPROVAL=true
AUTO_APPROVE_ENABLED=false
```

Configuration is validated at start-up: a probability outside `[0, 1]` or an attempt cap
above 5 refuses to boot rather than silently accepting a nonsensical policy.
