# Demo Script

Five minutes, one story: **a single failed payment worth ₹24,000, recovered end to end,
with every decision on the record.**

The temptation with a project this size is to show twenty features. Do not. A reviewer
remembers one narrative and forgets a tour. The narrative here is the product's own
sentence, walked in order:

> **AI decides. Guardrails control. Razorpay executes. Audit trail proves.**

| Scene | Screen | Time |
|---|---|---|
| 0 | Setup, before the room | — |
| 1 | `/` — the problem, in money | 0:00 – 0:40 |
| 2 | `/payments/[id]` — one failed payment | 0:40 – 1:10 |
| 3 | `/recovery/[id]` — the AI decides | 1:10 – 2:00 |
| 4 | `/recovery/[id]` — the guardrails control | 2:00 – 2:45 |
| 5 | `/recovery/[id]` — the human approves | 2:45 – 3:20 |
| 6 | `/checkout/[id]` — the customer pays | 3:20 – 3:55 |
| 7 | Two refusals | 3:55 – 4:30 |
| 8 | `/audit` — the proof | 4:30 – 4:50 |
| 9 | `/policy` — the close | 4:50 – 5:00 |

Everything below has been walked against the running application. The amounts, customers
and failure reasons are fixed by the deterministic seed, so the script is true every time
you run it.

---

## Scene 0 — Setup, before the room

Run these two commands, in this order, before anyone is watching.

```bash
python dev.py demo      # setup + seed + train, then prints the checklist
python dev.py start     # backend and frontend together, in one terminal
```

On Windows: `dev.bat demo` then `dev.bat start`, or double-click `SETUP-WINDOWS.bat` and
then `START-WINDOWS.bat`.

`demo` is setup, seed and train in one command. On a clean machine that includes a
`pip install` and an `npm install` and takes several minutes, so do not run it two minutes
before you present. On a machine that is already set up it is quick, and it rebuilds the
database from scratch — which is exactly what you want, because this script assumes twelve
failed payments that nobody has analysed yet.

`start` prints the two URLs and holds the terminal:

```
API      : http://127.0.0.1:8000        (docs at /docs)
Console  : http://localhost:3000
Stop both with Ctrl+C
```

**About the banner.** Unless you have put keys in `backend/.env`, the dashboard opens with
an amber panel headed **"Running with reduced credentials"**, listing:

- **Simulated gateway** — no Razorpay credentials configured. Payments are not real.
- **Rule-based planner** — no Gemini key configured. Plans come from the deterministic
  playbook, scored by the same model and held to the same guardrails.

Do not apologise for this and do not skip past it. It is the first thing you should point
at, because it is the thing that makes every number after it believable: the application
says out loud which of its subsystems are live and which are standing in. A demo that
quietly pretends to charge a card has to be taken on trust; this one does not. Say it once,
in one sentence, and move on:

> "It runs with no credentials at all, and it tells you so rather than pretending — the
> planner is deterministic and the gateway is in-process, but the guardrails, the model and
> the signature verification are the real ones."

If you *do* have a Gemini key configured, the badge on the case reads **Gemini** instead of
**Rule-based** and the agent trace is longer. Nothing else in this script changes.

---

## Scene 1 — The problem, in money (0:00 – 0:40)

**Click:** open `http://localhost:3000`. Nothing else — this is the landing screen.

**What appears:** the header **Revenue recovery**, the credentials banner, then six tiles
reading left to right:

| Tile | Value | Below it |
|---|---|---|
| Total volume | `₹5.7L` | 140 payments |
| Captured | `₹4.5L` | 128 collected |
| Failed | `₹1.2L` | 12 payments · 8.6% of all payments |
| Recoverable | `—` | passed the guardrails |
| Recovered | `—` | 0 cases closed |
| Recovery rate | `0.0%` | of recoverable volume |

Under them, the **Daily recovery budget** bar sits at `₹0.00 / ₹2,00,000.00`, and further
down the **Failed payments** table says *"12 of 12 not analysed yet."*

The right-hand half of the KPI row is deliberately empty. Nothing has been analysed, so
there is no recoverable figure and no recovery rate yet. That emptiness is the demo's
opening: you are about to fill it in.

**Say:**

> "A fortnight of traffic: ₹5.7 lakh in, and ₹1,22,512 of it failed — that is money this
> merchant has already earned and simply has not collected."

Lead with the rupee figure, not the failure rate. 8.6% sounds survivable; ₹1.2 lakh does
not.

---

## Scene 2 — One failed payment (0:40 – 1:10)

**Click:** in the **Failed payments** table, the second row — `₹24,000.00`, *Pro plan
annual renewal*, **Neha Kulkarni**, Card, 2d ago. Click the amount itself; it is a link to
the payment.

**What appears:** the payment page, headed **Payment**, with a red **Failed** badge and the
card **Payment record** — *"The gateway's own account of this payment, unedited."*

Scroll a fraction to **Gateway error, verbatim**. Five fields, exactly as Razorpay reports
them:

```
error_code         BAD_REQUEST_ERROR
error_source       bank
error_step         payment_authorization
error_reason       international_transaction_not_allowed
error_description  This card is not enabled for international transactions.
                   Please use a different card or contact your bank.
```

To the right, the customer: Neha Kulkarni, prior success rate **88%**, *14 of 16
succeeded*, lifetime value **₹40,331.00**.

**Say:**

> "The merchant's checkout showed this customer 'Payment failed, please try again' — and
> handed her the entire problem, even though the gateway told us precisely what went wrong
> and she has paid successfully fourteen times before."

That is the whole thesis in one sentence. `error_reason` is the signal every merchant
already receives and almost nobody acts on.

---

## Scene 3 — The AI decides (1:10 – 2:00)

**Click:** the button **Analyse with RecoverAI**, in the **Recovery** card on the right.

**What appears:** while the request is in flight the button reads *"Analysing…"* and a line
underneath narrates it — *"Classifying the failure, scoring recovery propensity, and
evaluating the guardrails."* Then it navigates to `/recovery/[caseId]`: **Recovery
decision**, status pill **Awaiting approval**, and a header line reading
`₹24,000.00 · Neha Kulkarni · Rule-based · Attempts: 0`.

The left column, top to bottom:

**Agent recommendation**

```
CLASSIFIED AS          →     RECOMMENDED STRATEGY
Bank decline                 Switch to UPI
```

*"Classification confidence 92% — how sure the agent is of the failure category above, not
of the strategy."* That distinction is worth pausing on: the number is about the diagnosis,
not the prescription.

**Rationale, in the agent's own words** — the whole paragraph is on screen. Read one clause
of it aloud rather than all of it:

> "Re-presenting the same card to the same issuer that just declined it is the single most
> common wasted retry in payments … Moving the customer to UPI routes the money through a
> different set of rails and a different risk decision."

It also cites what it reasoned from — `error_reason='international_transaction_not_allowed'
-> bank_decline` — and the customer's history, *14 of 16 prior payments succeeded (88%)*.

**What the customer would be told** — the message that would actually be sent, quoted, so
the operator approves the wording as well as the decision.

**Recovery propensity — 26%**, model `v1-gradient_boosting-18000`, with its top factors:

- Issuer declined the payment — historically 32% recoverable
- 'switch to upi' is a strong fit for this failure
- Customer has a payment success history of 88% over 16 payments
- High value (Rs 24,000) slightly lowers the odds

**Then scroll to the bottom, where the `Agent trace` tab is already open** (the tab strip
beside it holds the policy snapshot; you do not need it). It leads with a green summary bar:

> **0** money-moving tool calls in **3** steps — 2 read-only · 1 proposal

and the three steps, each with a capability badge:

| Step | Tool | Badge |
|---|---|---|
| 1 | `classify_failure_code` | Read-only |
| 2 | `score_recovery_propensity` | Read-only |
| 3 | `submit_recovery_plan` | Writes a proposal |

Any step can be expanded to its raw arguments and result. The counter is computed from the
recorded rows, not from a constant in the frontend. Three steps is what the deterministic
path records; a Gemini run records however many tools it chose to call. Either way the whole
toolset is seven — `get_payment_details`, `get_customer_history`, `classify_failure_code`,
`score_recovery_propensity`, `get_recovery_policy`, `check_recovery_eligibility`, all
read-only, plus `submit_recovery_plan`, which writes a recommendation.

**Say:**

> "Every tool is classified, and the badge is on the recorded step rather than on a
> description of it. There is no order-creation tool behind a check — there is no
> order-creation tool at all, and a test fails the build if anyone adds one."

---

## Scene 4 — The guardrails control (2:00 – 2:45)

**Click:** nothing. Scroll to the **Guardrails** card in the left column.

**What appears:** a badge reading **Approval required**, and the description

> *"All 13 rules are listed, passed ones included. The most restrictive verdict wins."*

Then two groups:

**REQUIRE A HUMAN SIGNATURE — 2**

- **R5_HIGH_VALUE_REVIEW** — *"Amount Rs 24,000.00 is at or above the high-value review
  threshold of Rs 10,000.00, so an operator must sign it off."*  `observed Rs 24,000.00`
  `limit Rs 10,000.00`
- **R13_HUMAN_APPROVAL** — *"Policy requires a human to approve every money-moving
  action."*  `observed Rs 24,000.00 at 25.9%`  `limit Rs 500.00 at 80.0%`

**SATISFIED — 11**, each with its own observed value and limit. R1 attempts, R2 cooldown,
R3 category, R4 ceiling, R6 duplicate order, R7 daily budget, R8 velocity, R9 amount
integrity, R10 propensity floor, R11 freshness, R12 risk flag.

The passing rules are the point of this panel. A checklist that showed only the two rules
that fired would leave a reviewer assuming the other eleven exist and ran. Here, *"Today's
committed total would be Rs 24,000.00, within the daily budget of Rs 2,00,000.00"* is a
finding, not an omission.

**Put your cursor on R9_AMOUNT_INTEGRITY.** It reads:

> **Amount integrity** — *"Recovery amount Rs 24,000.00 matches the original payment
> exactly."*  `observed Rs 24,000.00`  `limit Rs 24,000.00`

**Say:**

> "The AI cannot change the amount. Its plan has no amount field to put one in, the amount
> is copied from the original payment row, and this rule checks it independently anyway —
> so a rule that should never fire is the one that would catch the refactor that broke it."

Then point at the group heading above:

> "Two of the thirteen require a human signature — one because this is over ₹10,000, one
> because the master switch says every rupee does. Neither can be argued away by the model."

---

## Scene 5 — The human approves (2:45 – 3:20)

**Click:** in the sticky **Human approval** panel on the right — *"Nothing in this system
creates a payment order without a decision recorded here."*

1. Type a name into the **Operator** field. The placeholder is *"Your name, for the audit
   trail"*, and while it is empty the hint underneath says *"Required: the ledger records
   who decided, not that someone did."* The **Approve** button stays disabled until it has
   a value.
2. Click **Approve**.

**What appears:** a confirmation dialog, **Approve this recovery?**

> *"Approving creates a live payment order. Guardrails are re-evaluated on the server at
> this moment, so approval can still be refused."*

It restates the three facts that can be misread from a dense screen — **Amount**
₹24,000.00, **Customer** Neha Kulkarni, **Strategy** Switch to UPI — plus **Approving as**,
an optional note, and the footnote *"The amount is copied from the original payment and
cannot be changed here or by the agent."*

3. Click **Approve and create order**.

The status pill flips to **Awaiting payment**. The panel becomes *"Order live — waiting on
the customer"* with a copyable recovery link, an **Open customer checkout** button, and a
**Simulate customer abandonment** control. The **Audit trail** beneath it grows five new
entries as you watch: *Guardrails evaluated* — "Re-evaluated guardrails at approval time" —
then *Approval granted* (actor **Operator**, with the name you typed), *Gateway simulated*,
*Recovery order created* and *Recovery link sent*.

**Say:**

> "The name is required because the ledger has to answer 'who approved it', not 'was it
> approved' — and the guardrails run again right now, against live state, because the
> proposal was a snapshot and this is the moment money actually moves."

---

## Scene 6 — The customer pays (3:20 – 3:55)

**Click:** **Open customer checkout**. It opens in a new tab, so the console stays where
it is behind it.

**What appears:** a single centred column. No navigation bar, no dashboard link, no audit
ledger, no theme control. Just:

```
SECURE PAYMENT
RecoverAI

AMOUNT DUE
₹24,000.00
Your bank did not approve that payment. Paying by UPI usually goes through
straight away -- here is a fresh link for the same amount.
```

then an amber panel:

> **Simulated payment** — *"No Razorpay credentials are configured, so this checkout is an
> in-process simulator. Nothing here touches a real payment network and no money moves. The
> signature it produces is a genuine HMAC and is still verified server-side."*

and two buttons: **Pay ₹24,000.00 (simulated success)** and **Simulate a failed payment**.
At the bottom, *"This link is valid until …"* and *"Card details are never entered on this
page or stored by RecoverAI."*

Point at the absent chrome before you click anything. This page is outside the console's
route group by construction, not by a conditional — a customer who followed a payment link
from an email cannot click through into the merchant's audit ledger.

**Click:** **Pay ₹24,000.00 (simulated success)**.

**What appears:** a green tick, **Payment received**, **₹24,000.00**, and *"Thank you —
that payment has gone through. RecoverAI has been notified and there is nothing further for
you to do."* with a reference and a timestamp.

Switch back to the console tab. The case is now **Recovered**, and two more ledger entries
have landed: *Payment verified* — "Verified the HMAC signature for payment pay_…" — and
*Recovery succeeded* — "Recovered Rs 24,000.00 from Neha Kulkarni on attempt 1 via
'switch_to_upi'."

Go back to `/` for four seconds. **Recoverable** now reads `₹24K`, **Recovered** reads
`₹24K`, **Recovery rate** reads `100.0%`, and the daily budget bar shows
`₹24,000.00 / ₹2,00,000.00`.

**Say:**

> "Even with no Razorpay account, the simulator signs with a real HMAC-SHA256 and the
> server verifies it before anything is marked recovered — the browser cannot claim a
> payment happened."

---

## Scene 7 — The failure path (3:55 – 4:30)

Do not skip this. A recovery product that can only demonstrate the happy path has not
demonstrated recovery.

### 7a — A guardrail refuses (₹64,999)

**Click:** back to `/`, then **Analyse with RecoverAI** on the **₹64,999.00** row —
*Course bundle: Data Structures*, Sneha Reddy, UPI, *"The customer cancelled the payment
request in the UPI app."*

**What appears:** status pill **Blocked by guardrails**, verdict badge **Denied**. The
agent still made a recommendation — **Customer abandoned → Retry same method**, 53%
propensity — and the checklist overruled it:

**BLOCKING — THESE STOP THE RECOVERY OUTRIGHT — 1**

> **R4_AMOUNT_CEILING** — *"Amount Rs 64,999.00 is above the automated recovery ceiling of
> Rs 50,000.00."*  `observed Rs 64,999.00`  `limit Rs 50,000.00`

Two rules below it still say *require a human signature*, and ten more still say
*satisfied*. The engine takes the most restrictive verdict, so one deny beats everything.

**Say:**

> "The model rated this one at 53%, twice the odds it gave the case we just recovered. The
> engine denied it anyway, and the case is closed with the rule id attached."

### 7b — The agent refuses itself (₹1,661)

**Click:** back to `/`, then **Analyse with RecoverAI** on the **₹1,661.00** row — Neha
Kulkarni, Card, *"This transaction was blocked because it was flagged as suspected fraud."*

**What appears:** status **No action needed**. Classified as **Risk blocked**, recommended
strategy **No recovery**, propensity **1%**. The guardrail card now reads:

> *"All 13 rules are listed. None were consulted: the proposed strategy creates no payment
> attempt, so there was nothing to constrain."*

and lists all thirteen greyed out under **NOT CONSULTED — NO PAYMENT ATTEMPT IS PROPOSED**.

**Say:**

> "Nothing refused this one — the agent decided the correct action was to do nothing, and
> because no payment attempt was proposed there was nothing for the guardrails to permit or
> refuse. Failure handling here is a feature, not an error page."

### Spares, if a question sends you looking

The seeded queue also contains, without you having to construct anything:

- **₹4,977.00** — Kavya Rao, netbanking, issuer down. Denied by **R12_CUSTOMER_RISK_FLAG**:
  a recoverable failure on a customer the merchant has flagged.
- **₹953.00** — EMI, no error fields at all. Classified **Unclassified** at 30% confidence
  and routed to **Escalated to human**, because we do not automate around a failure we
  could not explain.
- **₹231.00** — Arjun Nair, expired card. Denied by **R10_PROPENSITY_FLOOR**: *"Predicted
  success likelihood 13.5% is below the 15.0% floor."* The closest call in the dataset, and
  the reason that rule renders one decimal place — rounding 13.5% and 15.1% both to the
  same whole number would make a denial look identical to an approval on the screen that
  justifies it.

---

## Scene 8 — The proof (4:30 – 4:50)

**Click:** **Audit ledger** in the top navigation, then the **Re-verify** button in the
panel at the top of the page. Let them watch the request go out.

**What appears:**

> **Ledger verified — 39 events, hash chain intact**
> *"Every hash was recomputed from genesis and matched the value stored on its entry. No
> row has been edited, removed or reordered since it was written."*

with the head hash printed in full underneath, and the ledger table below it: **SEQ ·
RECORDED · EVENT · ACTOR · SUMMARY · HASH**, newest first, each row expandable to its
stored payload. Paste the case id into the **Case id** filter and press **Apply** to see
just this recovery.

Thirty-nine is what you get if you have followed this script exactly. A fresh seed writes
twelve, one per failed payment. The one recovery you drove added fourteen of them —
analysis started, failure classified, propensity scored, strategy proposed, agent degraded
to rules, guardrails evaluated, approval requested, guardrails re-evaluated, approval
granted, gateway simulated, order created, link sent, payment verified, recovery succeeded.
The blocked ₹64,999 case added seven and the risk-blocked case added six. If your number is
different it is because you clicked something extra, not because anything is wrong.

**Say:**

> "Each entry stores a SHA-256 over its own contents *including the hash of the entry
> before it*, so editing one historical row breaks its own hash, breaks the next row's
> `prev_hash`, and breaks every hash after that — and this button recomputes the whole
> chain from genesis to prove it."

---

## Scene 9 — The close (4:50 – 5:00)

**Click:** **Guardrails** in the top navigation.

**What appears:** the live limits, read from the server, with the rule id that consumes
each one — 2 attempts, a 900s cooldown, a ₹50,000.00 ceiling, a ₹10,000.00 review
threshold, a ₹2,00,000.00 daily budget, 3 cases per customer per day, a 15% propensity
floor, a 168h freshness window. Then **Never recovered automatically**: *Risk blocked* and
*Unclassified*. Then the full **Rule catalogue**, 13 rules.

Above all of it, an alert:

> **This screen is read-only by design** — *"These values cannot be changed from the
> console or through the API the agent's flow uses … A limit an automated system can raise
> is not a limit."*

**Say, and then stop talking:**

> "There is no edit control on this page, and that is the feature. The AI decides,
> guardrails control, Razorpay executes, and the audit trail proves it."

---

## The 60-second version

When the room is running late, cut to four scenes and one closing line. Have
`/payments/[the ₹24,000 payment]` already open in a tab.

| Keep | Seconds | What to do, and the one thing to land |
|---|---|---|
| **Scene 1** | 10 | The KPI row. "₹1,22,512 failed. That is collectable money." |
| **Scene 3** | 20 | Switch to the payment tab, click **Analyse with RecoverAI**. Bank decline → Switch to UPI, and the rationale. Scroll to the trace bar: "the agent has no tool that moves money." |
| **Scene 4** | 15 | Scroll back up through the 13-rule checklist. "Two require a human signature, and the AI cannot change the amount." |
| **Scene 5 + 6** | 15 | Type a name, **Approve**, **Approve and create order**, **Open customer checkout**, **Pay**, land on **Payment received**. |

Then the positioning line. Skip Scene 2 (the raw gateway error is visible on the case
screen anyway), skip the refusals, skip the ledger — but say *"there is a hash-chained
audit ledger with a verify button, and I would rather show you that than tell you about
it"*, which usually buys the twenty seconds back.

---

## The questions you will be asked

### "Is the data real?"

No, and the application says so in three places rather than leaving you to find out: the
banner on the dashboard, the panel on the customer checkout, and a `gateway_simulated` entry
written into the audit ledger for every order it mints.

The 140 payments come from a deterministic seeder — same database every run, which is what
makes this script repeatable. The failure scenarios are catalogued from Razorpay's actual
error vocabulary: `error_code`, `error_source`, `error_step`, `error_reason`,
`error_description`, with the same flatness real traffic has, where most declines arrive as
`BAD_REQUEST_ERROR` and the signal lives in `error_reason`. The propensity model is trained
on 18,000 synthetic rows whose generative process is documented.

What is *not* synthetic: the taxonomy, the thirteen
rules, the HMAC signature verification, the hash chain, and the Razorpay Orders API client —
add test keys to `backend/.env` and the same flow runs against the real Test Mode API.

### "What if the AI is wrong?"

Then the worst it can do is waste one gateway call on a recovery a human already approved,
for an amount it did not choose, on a customer who is not flagged, under a daily cap.

Concretely: the agent's output is an `AgentRecoveryPlan` — a category, a confidence, a
strategy, a rationale and a customer message. It has no amount field. It reaches money only by
passing thirteen deterministic rules and then a human click. If it misclassifies a bank
decline as a gateway error, the consequence is the wrong retry strategy, not the wrong
amount or the wrong customer. Everything it produces is on the case screen for the operator
to disagree with before approving — including the exact evidence the classification was
drawn from and the message the customer would receive.

And when it cannot classify at all, it does not guess. `UNKNOWN` and `RISK_BLOCKED` are
structurally non-recoverable — an unclassifiable failure is handed to a person, a risk block
goes nowhere at all — because absence of evidence is not evidence that a retry is safe.

### "What stops a double charge?"

Five independent things, and they were designed to overlap:

1. Each attempt gets a deterministic idempotency key, `case_id:attempt_number`. A repeated
   approve reuses the existing attempt row and its existing order instead of asking the
   gateway for a second one.
2. The database enforces it too — unique constraints on the idempotency key and on
   `(case_id, attempt_number)`.
3. One case per payment, by unique constraint.
4. **R6_DUPLICATE_ORDER** denies a proposal while an attempt is still open.
5. The state machine only permits `awaiting_approval → approved`, so a second approve on an
   already-approved case is refused with `invalid_transition`, not silently repeated.

The confirmation dialog's button also disables itself on the same render that shows its
spinner, so an impatient second click lands on a dead control — but that is a courtesy, not
the defence.

### "Why not just let the AI retry?"

Because the AI is the part of this system with the least accountability and the most
plausible-sounding output. Three reasons, in order of how much they matter:

- **Liability.** Somebody's card gets charged. If the answer to "who authorised this" is "a
  language model", there is no answer. The ledger records a named person for every rupee.
- **The failure mode is silent.** A wrong retry does not throw an exception. It succeeds,
  and the customer is annoyed or double-charged, and nobody finds out until support does.
- **Retrying is often the wrong answer.** Of the twelve seeded failures, six end with no
  payment attempt at all. Three because the agent itself proposes none — the fraud block,
  the failure it could not classify, and the insufficient-funds case that needs to wait for
  a salary date rather than be retried in thirty seconds. Three because a guardrail refuses
  — the amount ceiling, the risk-flagged customer, the propensity floor. An agent optimising
  for recovery rate would have attempted all six.

The auto-approval lane exists in the policy engine — an amount ceiling of ₹500 at 80%
propensity — precisely so the design can express graduated autonomy. It ships off, and the
policy screen says so.

### "How do you know the audit log wasn't edited?"

Each row stores a SHA-256 over a canonical JSON serialisation of its own fields — sequence,
event type, actor, case, payment, summary, payload, timestamp — *and* the hash of the row
before it. The first row links to sixty-four zeroes. `GET /api/audit/verify` walks the chain
from genesis and recomputes every hash; if one row was edited, its own hash changes, the
next row's `prev_hash` stops matching, and the endpoint reports the exact sequence number
where verification stopped. Two tests in the suite do exactly that: one rewrites a stored
payload and asserts the break is reported at that precise event, the other deletes an entry
from the middle and asserts the gap is detected.

Be honest about the limit, because a reviewer who spots it before you do will discount
everything else: this makes tampering **evident**, not impossible. Someone with write
access to the SQLite file could recompute the whole chain forward from a forged entry — the
algorithm is in the repository. What they cannot do is change one row and leave the rest
consistent, which is the shape that buggy migrations, well-meaning manual fixes and
accidental double-writes actually take. A production deployment would publish the head hash
somewhere the database operator cannot reach.

### "What happens when Gemini is down?"

Nothing visible breaks, and the fallback is not a stub. The orchestrator catches an
unavailable or failing model and hands the same input to the deterministic planner, which
consults the same taxonomy and the same playbook, calls the same propensity model, and
returns the same `AgentRecoveryPlan` — category, calibrated confidence, strategy, an
operator-facing rationale citing the specific gateway fields it reasoned from, a
customer-facing message and an evidence list. The case is stamped `rule_based`, the UI
shows the badge, and an `agent_degraded` entry goes into the ledger with the reason.

You are watching that path right now. Everything in this demo was produced without a
language model in the loop.

That also makes the model honestly evaluable: because the deterministic answer is always
available for the same input, "did the LLM add anything?" is a question you can answer by
comparing two plans rather than by assertion.

### "Is this production ready?"

No. Here is precisely what is missing, in the order I would fix it:

1. **There is no authentication.** The approval identity is a typed name, recorded faithfully
   and verified not at all. The API has no auth in front of it either. Real deployment needs
   SSO and a role check before anything else on this list.
2. **`RETRY_LATER` parks the case instead of scheduling it.** Insufficient-funds recovery is
   the highest-value case in Indian e-commerce and it needs a job scheduler to wait for a
   salary date. The case is closed as `no_action` with the reasoning recorded, which is
   honest but is not the product.
3. **Link expiry is swept on read, not scheduled.** Cases get an `expires_at` sixty minutes
   out, and the sweep runs when the recovery queue or a checkout page is read. That keeps
   what a caller sees correct without adding a background runtime, but it is not the same
   thing as a scheduler: nothing happens to an expired case until someone looks. A real
   deployment would want the timer, and it is the same missing piece as item 2.
4. **SQLite, one file, one process, no migrations.** The DSN is a standard SQLAlchemy one
   and no code reads raw SQL, so Postgres is a configuration change — but there is no
   migration tooling.
5. **Single merchant, single currency.** Every guardrail limit is denominated in paise;
   multi-currency needs per-currency limits, which is why currency is pinned in config.
6. **The propensity model is trained on synthetic data.** The architecture and evaluation
   are real; the ground truth is not, and every screen that reports the score says which
   model version produced it.

What I would defend as production-shaped: the tool capability boundary, the guardrail
engine, the re-evaluation at approval time, the idempotency design, the hash chain, and the
fact that 141 tests cover them rule by rule.

### "How good is the model, really?"

Gradient boosting on 18,000 synthetic rows, 14,400 train / 3,600 test, positive rate 0.316.
ROC-AUC **0.802**, with five-fold cross-validation at **0.793 ± 0.007** — so it is not
overfitting the split. Accuracy 0.755, precision 0.656, recall 0.470 at a 0.5 threshold.
The decision-tree baseline scores 0.729 ROC-AUC, so the ensemble buys about seven points.

The number that actually matters is not any of those. The model is not used as a classifier
at 0.5 — it is used as a **screen** at the R10 floor of 0.15, and at that operating point
recall is **0.953** and precision is **0.420**. It screens out 1,018 of 3,600 test cases
while keeping 95% of the ones that would have succeeded. For a rule whose job is "do not
bother the customer when this is hopeless", high recall is the correct trade and low
precision is the acceptable cost.

The strongest feature is `hours_since_failure` at 0.165 importance, which is the right
answer: recovery value decays with intent.

### "Why is the amount not just a field on the plan?"

Because a field that exists can be set, and a field that can be set has to be validated
everywhere it travels. The plan schema has no amount and forbids extra keys, so a model
that hallucinated `"amount_paise": 1` would fail validation at the boundary rather than be
caught three layers later. The case copies the amount from the payment row. R9 then checks
it independently, which is belt and braces on purpose — it is the rule that would catch a
future partial-recovery feature added without thinking the policy through.

### "What would you build next?"

Three things, in order:

1. **The scheduler**, which unlocks `RETRY_LATER` and the expiry sweep together. That single
   piece converts the largest failure category in the taxonomy from "parked with an
   explanation" into actual recovered revenue.
2. **Outcome capture, then retraining on real data.** Every case already records the
   proposal, the guardrail verdict, the human decision and the outcome — that is a labelled
   training row. The synthetic model is scaffolding for the first hundred cases; after that
   the system should be learning from its own ledger.
3. **Authentication and roles**, so the name in the audit trail is an identity rather than a
   claim, and so an approval limit can be tied to a person rather than to a config value.

Further out, the interesting one is per-merchant policy: the thirteen rules are pure
functions over a context object, so making the limits per-merchant rather than per-process
is a data change, not an architecture change.

---

## Pre-demo checklist

Run through this ten minutes before, not one minute before.

- [ ] **Fresh seed.** `python dev.py seed`. It drops and rebuilds, so the queue is exactly
      the twelve unanalysed failures this script assumes. If you rehearsed, you must reseed —
      an already-analysed payment shows **View case** instead of **Analyse with RecoverAI**,
      and Scene 3 has nothing to click.
- [ ] **Model trained.** `python dev.py train`. Without the artefact the propensity card
      shows an amber *"Model not trained"* panel and the banner adds a third bullet. Nothing
      breaks, but you will spend twenty seconds explaining it.
- [ ] **Both servers up.** `python dev.py start`, then confirm `http://127.0.0.1:8000/docs`
      loads *and* `http://localhost:3000` loads. If Next.js printed port 3001 because 3000
      was busy, use the URL it printed.
- [ ] **Browser zoom at 100%, window maximised.** The recovery screen goes two-column at
      1024px and stacks below it. Stacked, the sticky approval panel is no longer beside the
      guardrail checklist — and "the rules said this, so I am signing it" is the single most
      useful adjacency in the demo. Zooming to 125% on a 1280-wide laptop is enough to lose
      it.
- [ ] **Light mode.** The theme toggle is top right. Dark mode is fully supported and looks
      fine, but a projector does not.
- [ ] **Tabs to pre-open, left to right:**
      1. `http://localhost:3000/` — the dashboard
      2. `http://localhost:3000/payments/<the ₹24,000 payment>` — for Scene 2, so a slow
         first render never happens on stage. The payment ids are regenerated by every seed,
         so get this one by clicking the ₹24,000 amount in the dashboard table and copying
         the URL.
      3. `http://localhost:3000/policy` — for Scene 9
      Leave the checkout tab to open itself from the approval panel in Scene 6; opening it
      early would show *"This payment is still being reviewed"*, which is correct and
      confusing.
- [ ] **Clear the operator name** if you rehearsed. The approval panel remembers it in
      `localStorage`, and typing it live is Scene 5's whole beat.
- [ ] **Close the terminal window or move it to a second display.** Uvicorn's request log
      scrolling behind the browser is a distraction, not evidence.
- [ ] Optional but worth it: run `python dev.py test` once and leave the `141 passed` line
      in the scrollback. If someone asks about coverage you have it, and if they do not, no
      time was spent.

---

## If something breaks live

The rule for all of these: narrate what you are doing while you fix it. A presenter who
diagnoses their own system in ten seconds looks better than one whose demo never wobbled.

**Every panel says the API is unreachable.** The backend died or never started. Open
`http://127.0.0.1:8000/docs` in a spare tab to confirm, then restart with `python dev.py
backend` in a second terminal — the frontend reconnects on the next click, no reload
needed. If the API is up but the console still cannot reach it, `frontend/.env.local` has
the wrong `NEXT_PUBLIC_API_BASE_URL`; that value is baked in at build time, so the frontend
needs restarting, which is not a thirty-second fix — move to the next scene and come back.

**The dashboard is empty, or you get `no such table: payments`.** Nothing has been seeded.
`python dev.py seed`, then refresh. Fifteen seconds.

**"Analyse with RecoverAI" is missing and the row says "View case".** That payment was
already analysed in a rehearsal. Click **View case** and carry on from Scene 3 — the screen
is identical, you just do not get to show the button. Or pick a different row: the
₹12,500.00 authentication-failure row behaves the same way as the ₹24,000 one and also
trips R5_HIGH_VALUE_REVIEW.

**Approve returns "Request refused" with a `guardrail_denied` code.** This is the system
working, so use it. The panel prints the server's own sentence, which names the rule and the
number that stopped it. Read it aloud: "the guardrails re-ran at approval time and something
changed since the proposal — that is the behaviour this design exists for." Then move to
Scene 7, which was going to show a refusal anyway.

**The propensity card shows the amber "Model not trained" panel.** The artefact is missing.
Say that the score came from the documented heuristic fallback rather than the trained model
and that the UI is telling you so, and continue — every other part of the flow is unaffected.

**"Simulate a failed payment" returns "Unknown failure scenario".** Set the scenario
dropdown back to **Random - drawn from the seeded catalogue**, which is the default and
always works, and pick the amount preset you wanted instead. You do not need this control
for any scene in this script; it is there for questions. The combination worth knowing is
**Card declined by the issuer** with the **₹64,999** preset — labelled *"above the absolute
ceiling - will be denied"* — which manufactures Scene 7a from scratch on demand.

**The whole thing is wedged and you have thirty seconds.** Ctrl+C the `start` terminal,
`python dev.py seed`, `python dev.py start`, and talk over it — the seeder prints a summary
of exactly what it built, including the line `Failure mix -- all 12 left UNANALYSED so the
agent runs live:` and a four-line breakdown of the guardrail material it deliberately
included. That is a reasonable thing to have on screen while you wait.
