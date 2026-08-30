# Design Decisions

Every entry below is a choice that had a real, defensible alternative. The alternative is
named, and so is the reason it lost. Decisions without a genuine trade-off are not recorded
here — they belong in the code comments.

---

## D1. The LLM has no money-moving tool at all

**Decision.** The agent's toolset contains six read-only tools and one tool that submits a
*recommendation*. There is no `create_order` tool, gated or otherwise. Order creation lives
in `RecoveryService.approve()`, which is reachable only from an authenticated HTTP endpoint
that a human calls.

**Alternative considered.** Give the agent a `create_recovery_order` tool that internally
checks for an approval token, and refuse the call when the token is absent. This is the
common pattern in agent frameworks, and it reads well in a demo.

**Why it lost.** A gated tool is a *runtime* control: it depends on the check being correct,
staying correct, and never being bypassed by a future refactor. Removing the tool entirely
is a *structural* control — there is no code path from the model's output to an order,
so the property holds even if the agent is jailbroken, the prompt is injected, or the model
hallucinates a tool call. Structural beats runtime for anything touching money.

The property is also made checkable rather than merely claimed: every tool carries a
`ToolCapability`, `assert_no_financial_tools()` runs on every toolset build, and
`tests/test_agent_tool_safety.py` pins the exact tool list so that *adding* a tool is a
deliberate act that breaks the build rather than a quiet commit.

---

## D2. The recovery amount is copied from the payment, never taken from the AI

**Decision.** `AgentRecoveryPlan` has no amount field, and `model_config` sets
`extra="forbid"`. `case.amount_paise = payment.amount_paise`, always. Rule
`R9_AMOUNT_INTEGRITY` independently denies any case whose amount differs from its original
payment by even one paisa.

**Alternative considered.** Let the agent propose an amount, so it could recommend a partial
capture or a discount to rescue a price-sensitive customer.

**Why it lost.** That is a genuinely useful product feature and a genuinely bad first
feature. It converts the agent from "chooses a payment rail" into "sets a price", which is a
different risk class entirely and would need its own approval workflow, its own limits and
its own audit vocabulary. Defence is layered here on purpose — schema, service and policy
rule all enforce it — which contradicts the project's general rule of validating once, and
is justified precisely because this is the invariant an attacker or a bug would target first.

---

## D3. Guardrails are re-evaluated at approval, not just at proposal

**Decision.** `approve()` rebuilds the full `GuardrailContext` from live state and re-runs
all thirteen rules before creating an order. A case can be proposed as approvable and denied
minutes later.

**Alternative considered.** Trust the verdict computed at proposal time. It is already
stored on the case, and re-running the engine costs a handful of queries.

**Why it lost.** A proposal is a snapshot of a world that keeps moving. Between proposal and
approval the daily budget can be consumed by other cases, another attempt can be made, the
customer can be risk-flagged, and the recovery window can expire. Approving against a stale
verdict means the limits are advisory. The check that counts is the one that runs at the
instant money would move.

---

## D4. Money is integer paise, never float, never Decimal

**Decision.** Every amount in the database, the services and the policy engine is an `int`
of paise. Rupees exist only in API response fields and in the UI.

**Alternatives considered.** (a) `float` rupees — simple and readable. (b) `Decimal` rupees
— exact, and the textbook answer for money.

**Why they lost.** `float` is disqualified outright: `0.1 + 0.2 != 0.3` in binary floating
point, and a rounding error in a payment system is a defect. `Decimal` is correct but adds
serialisation friction at every boundary (JSON, SQLite, and Razorpay's API all want an
integer), and Razorpay's API is *already* denominated in paise. Choosing paise means there is
exactly one unit from the database to the gateway and no conversion at the boundaries where
conversion mistakes happen.

---

## D5. The audit log is a hash chain, not a table of rows

**Decision.** Each `AuditEvent` stores a SHA-256 hash over its own content plus the previous
event's hash, and sequence numbers must be gapless. `GET /api/audit/verify` recomputes the
whole chain and names the first sequence that fails.

**Alternatives considered.** (a) A plain append-only table with a timestamp. (b) An actual
blockchain or an external immutable log service.

**Why they lost.** A plain table proves nothing: anyone with database access can edit a row
and the log will happily agree with itself. A real distributed ledger solves a trust problem
this project does not have — there is one writer and one operator. The hash chain sits
exactly in between: it makes tampering *detectable* by anyone who can read the table, costs
one hash per write, and — most importantly — a reviewer can press a button and watch the
verification run. An immutability claim nobody can check is just a sentence in a README.

---

## D6. The simulated gateway implements real HMAC signature verification

**Decision.** `SimulatedGateway.simulate_payment()` returns a genuine HMAC-SHA256 signature
computed over `f"{order_id}|{payment_id}"` with a fixed simulated secret, and the checkout
flow routes it through the same `verify_payment()` path that real Razorpay payments use.

**Alternative considered.** Have the simulated path skip verification and mark the case
recovered directly. Fewer moving parts, same visible outcome.

**Why it lost.** Then the credential-free demo would exercise a *different* code path from
the credentialed one, and the signature verification — the security-critical part — would be
the one thing never tested in the mode most reviewers will run. The simulator exists to
remove the dependency on a Razorpay account, not to remove the logic.

---

## D7. The rule-based planner is a first-class path, not a stub

**Decision.** With no `GEMINI_API_KEY`, or when the LLM call fails for any reason, a
deterministic planner produces the plan from the failure taxonomy, the playbook and the ML
propensity score. It writes a real rationale citing real evidence, populates the tool-call
trace with the steps it actually took, and the UI labels the case `Rule-based`.

**Alternative considered.** Require an API key and return an error without one.

**Why it lost.** Two reasons, one practical and one architectural. Practically: a submitted
project that cannot run without the reviewer obtaining a third-party API key is a project
that will be marked without being run. Architecturally: a payments recovery pipeline that
stops working when a third-party model API has an outage is not a payments pipeline. The
fallback is a resilience property that happens to also solve the demo problem.

The trade-off is honesty. The rule-based plan is genuinely less nuanced than Gemini's, so
the mode is labelled everywhere it appears rather than quietly substituted.

---

## D8. Guardrail rules are pure functions over a precomputed context

**Decision.** Each rule is `(GuardrailContext) -> GuardrailEvaluation` with no database
access and no I/O. The service layer computes everything the rules need — daily totals,
attempt counts, last-attempt time — and passes it in.

**Alternative considered.** Let each rule query the database for what it needs. Less
plumbing, and each rule becomes self-contained.

**Why it lost.** Purity is what makes the policy exhaustively testable. Thirteen rules with
pass and fail cases each is a fast, dependency-free test file, and a financial control that
is hard to test is a financial control nobody should trust. It also makes evaluation cost
predictable: the queries happen once, not thirteen times, so adding a rule can never
introduce an N+1.

---

## D9. Every rule returns an evaluation, including when it passes

**Decision.** A passing rule returns `passed=True` with a reason, and the approval screen
renders the complete checklist.

**Alternative considered.** Return evaluations only for rules that fired, which is what most
validation code does.

**Why it lost.** An operator approving a payment needs to see what *was* checked, not only
what failed. A screen showing "1 warning" tells you nothing about whether the other twelve
controls exist, ran, or silently errored. Showing the full checklist is how the interface
makes the guardrail layer visible instead of asserted.

---

## D10. Three distinct terminal states mean "no money will move"

**Decision.** `BLOCKED` (a guardrail refused), `NO_ACTION` (the agent judged that doing
nothing is correct) and `ESCALATED` (a human must handle it off-platform) are separate
states.

**Alternative considered.** One `CLOSED` state with a reason string.

**Why it lost.** These three have different causes, different owners and different follow-up.
A merchant filtering for "cases the automation refused" must not have to string-match a
reason field to find them, and metrics that lump them together would hide whether the
guardrails are too tight or the agent is too cautious.

---

## D11. One state machine, declared as data

**Decision.** `ALLOWED_TRANSITIONS` is a dict in `enums.py`, and every state change goes
through `RecoveryService._transition`, which validates against it.

**Alternative considered.** Set `case.status = ...` at each call site with a nearby `if`.

**Why it lost.** Scattered assignments are how a lifecycle ends up tracked in two places
that disagree. As data, the machine can be validated at import time, rendered in the docs,
and tested exhaustively; and an illegal transition raises rather than silently corrupting a
case. There is exactly one recovery state machine in this codebase — new lifecycle concepts
extend it rather than adding a parallel boolean.

---

## D12. The service layer owns the transaction; the ledger never commits

**Decision.** `AuditLedger.record()` and the agent orchestrator `flush()` but never
`commit()`. Each public service method commits exactly once, at the end.

**Alternative considered.** Let the ledger commit its own writes, so an audit entry is never
lost.

**Why it lost.** It inverts the guarantee that matters. If the audit entry commits
separately from the state change it describes, a failure between the two produces either a
recorded approval that never happened or an approval with no record. Committing them
together means the audit trail and reality are atomic: both, or neither.

---

## D13. SQLite, with a standard SQLAlchemy DSN

**Decision.** SQLite via SQLAlchemy 2.0, database file under `backend/data/`.

**Alternatives considered.** (a) PostgreSQL in Docker. (b) Firestore, which the author has
shipped before.

**Why they lost.** Postgres means Docker Desktop, which is a heavyweight install on a
Windows laptop and a real barrier for a reviewer. Firestore means a Google Cloud project and
service-account credentials, which reintroduces exactly the "cannot run it without keys"
problem D7 exists to solve — and it is a document store, while this domain is highly
relational (payments → cases → attempts, with foreign keys that matter). SQLite is in the
Python standard library, needs no server, and the DSN shape means moving to Postgres later
is a configuration change rather than a rewrite.

One SQLite-specific trap is handled explicitly: foreign keys are **off by default**, so the
declared constraints would be decorative without a `PRAGMA foreign_keys=ON` on connect.

---

## D14. FastAPI backend + Next.js frontend, as two processes

**Decision.** A Python API and a separate Next.js app that calls it over HTTP with CORS.

**Alternative considered.** Put everything in Next.js API routes and drop Python entirely.

**Why it lost.** The ML model is scikit-learn and the agent tooling is Python. Running the
propensity model from Node would mean either a second service anyway or a port to a
JavaScript ML stack, which is strictly worse. Splitting also enforces the boundary the
project is about: the guardrails run server-side, in a process the browser cannot influence.

---

## D15. shadcn/ui components vendored by hand, without the CLI or Radix

**Decision.** The UI kit is written into `src/components/ui/` as owned source, with
`class-variance-authority` + `tailwind-merge`. `tabs`, `dialog` and `tooltip` are small
accessible implementations rather than Radix wrappers.

**Alternative considered.** `npx shadcn@latest init` and add the Radix dependencies.

**Why it lost.** shadcn's actual model is that components are copied into your repository
and owned by you — so writing them in is the authentic use of the pattern, not a shortcut.
It also removes a network-dependent build step and about a dozen transitive packages from a
project that must install cleanly on a laptop behind whatever network a reviewer is on.

The cost is real and worth naming: hand-written `dialog` and `tabs` need their
accessibility behaviour implemented deliberately — focus management, Escape, `aria-*`,
arrow-key navigation — where Radix would have supplied it. That work is done and commented.

---

## D16. TypeScript types are hand-mirrored, with Zod at the boundary

**Decision.** `src/lib/types.ts` mirrors the Pydantic schemas by hand. Zod validates the
critical responses at runtime.

**Alternative considered.** Generate the client from FastAPI's OpenAPI schema.

**Why it lost.** Codegen adds a build step that must be re-run at the right moment, and a
stale generated client fails in a confusing way. For six screens and about twenty response
shapes, a hand-written file that a reader can open and understand is worth more than
automation. The drift risk is handled where it actually bites — at runtime, at the boundary,
with Zod — and the label maps are typed as `Record<Union, string>` so adding a backend enum
member without a display label becomes a compile error.

This would be the wrong call at ten times the size. It is the right call here.

---

## D17. The ML model is trained on synthetic data, and says so everywhere

**Decision.** A documented generative process produces the training set. The module
docstring, the metrics output, `docs/05-ML-MODEL.md` and the README all state that the data
is synthetic.

**Alternatives considered.** (a) Drop the ML model and use a lookup table of base rates.
(b) Train on synthetic data and report the metrics without qualification.

**Why they lost.** (b) is dishonest — a 0.82 ROC-AUC on data you generated measures your
generator, not the world, and a reviewer who spots that unqualified number will and should
discount everything else. (a) is honest but throws away something real: the feature
engineering, the pipeline, the baseline comparison and the recall-oriented threshold choice
are genuine work, and the *shape* of the model is what would be reused against real merchant
data. The chosen path keeps the engineering and labels the ground truth accurately —
including surfacing `is_fallback` in the API when the heuristic produced a score instead of
the model.

---

## D18. A Python task runner instead of a Makefile

**Decision.** `dev.py` holds every task; `dev.bat` and `dev.sh` are thin launchers.

**Alternative considered.** A Makefile, with `.bat` files for Windows.

**Why it lost.** `make` is not installed on a stock Windows machine, and Makefile recipes
are full of shell constructs (`source .venv/bin/activate &&`) that do not run in cmd.exe.
Maintaining a Makefile *and* a parallel set of batch files means two implementations that
drift. Python is already a hard prerequisite, so putting the logic there costs nothing and
is identical on every platform. `dev.py` imports nothing outside the standard library,
because it is the thing that does the installing.

---

## D19. Approval identity is a recorded name, not authentication

**Decision.** The approver types a name; it is stored on the case and in the audit event.
There is no login.

**Why.** Real authentication means an identity provider, sessions, roles and a permission
model — a substantial subsystem that would dominate the codebase without demonstrating
anything about the actual thesis. The audit trail is built to record *whatever identity it
is given*, so adding SSO later changes where the string comes from and nothing else. This is
called out as a limitation in the README rather than left for a reviewer to discover.

---

## D20. `RETRY_LATER` parks the case instead of scheduling it

**Decision.** A strategy of `RETRY_LATER` — the correct answer for `INSUFFICIENT_FUNDS`,
where the instrument works and the balance does not — moves the case to `NO_ACTION` with the
reasoning recorded, rather than scheduling a future attempt.

**Alternative considered.** A background scheduler that re-attempts after a delay.

**Why it lost.** Delayed execution needs a durable job store and a worker process, which is a
second runtime a reviewer would have to start and a whole class of failure modes (missed
jobs, duplicate jobs, jobs firing after a case expires) to get right. The judgment — "do not
retry this in thirty seconds, the balance has not changed" — is the valuable part and it is
fully implemented. The execution mechanism is scoped out and labelled, rather than
half-built.
