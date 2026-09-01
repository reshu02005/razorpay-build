# RecoverAI

When a payment fails, most apps just show "Payment failed, please try again" and leave it at that. I wanted to build something that actually figures out *why* it failed, decides if it's worth retrying, and helps the customer finish paying — but without letting the AI touch money directly.

That's the main idea behind RecoverAI. The AI suggests what to do, a separate set of rules checks whether that suggestion is allowed, and a person still has to approve it before anything is executed.

## Why I built it this way

The difficult part isn't making the AI suggest a recovery action. The difficult part is making sure the AI can work with payment data without being able to execute payments itself.

For example, if a card is declined, the AI might suggest trying UPI instead. That's only a recommendation. Before anything happens, it goes through the policy engine, which checks things like previous attempts, time since the last attempt, payment amount, daily limits, and other rules.

If everything passes, a human approves the action. The rules are checked again immediately before the payment order is created, so we don't rely on an old decision if something has changed.

Most importantly, the AI has no tool for creating a payment order. It also doesn't provide the payment amount. The amount always comes from the original failed payment.

---

## How it works

```text
Payment fails
     │
     ▼
AI analyzes the failure
     │
     ├── What caused the failure?
     ├── Has this customer had similar issues?
     ├── Is retrying likely to work?
     └── What recovery option makes sense?
     │
     ▼
AI suggests a recovery plan
     │
     ▼
Policy engine checks 13 rules
     │
     ├── Blocked → log the decision
     │
     └── Needs approval
              │
              ▼
        Human approves
              │
              ▼
     Rules checked again
              │
              ▼
      Razorpay creates order
      using original amount
              │
              ▼
       Customer completes payment
              │
              ▼
      Server verifies signature
              │
              ▼
       Recovery is recorded
```

The second policy check is intentional. Something can change between the initial analysis and the final approval — for example, a customer might reach a retry limit or the daily budget might already be used.

---

## Guardrails

The AI is kept separate from payment execution. The main protections currently implemented are:

| #  | Protection                          | How it is enforced                                     |
| -- | ----------------------------------- | ------------------------------------------------------ |
| 1  | AI cannot move money                | AI has no payment-creation tool                        |
| 2  | AI cannot change the amount         | AI output has no amount field                          |
| 3  | Duplicate payments are prevented    | Unique attempt ID + database constraint + policy check |
| 4  | Retry risk is limited               | Maximum 2 attempts and 15-minute gap                   |
| 5  | Payment limits are enforced         | ₹50,000 per payment and ₹2,00,000 per day              |
| 6  | Customer retry limit                | Maximum 3 recovery cases per customer per day          |
| 7  | Fraud cases aren't retried          | Fraud-flagged payments are blocked from recovery       |
| 8  | Human approval is required          | Automatic approval is disabled                         |
| 9  | Audit history is tamper-evident     | Log entries are connected using hashes                 |
| 10 | Browser cannot fake payment success | Signature is verified on the server                    |

There are 13 policy checks in total. The remaining checks cover the same areas of retry timing, payment state, limits, and recovery eligibility.

---

## Tech Stack

* **Backend:** Python, FastAPI, SQLAlchemy, SQLite
* **AI:** Gemini function calling with a rule-based fallback
* **ML:** scikit-learn
* **Payments:** Razorpay Test Mode + local simulated gateway
* **Frontend:** Next.js, TypeScript, Tailwind CSS
* **Testing:** pytest
* **CI:** GitHub Actions

The ML setup currently compares a Gradient Boosting model with a simple Decision Tree.

---

## Project Structure

```text
backend/
└── app/
    ├── domain/       # Core types and errors
    ├── db/           # Database models and demo data
    ├── agent/        # AI logic and tools
    ├── policy/       # Policy and guardrail checks
    ├── ml/           # Recovery propensity model
    ├── payments/     # Razorpay + simulated gateway
    ├── audit/        # Tamper-evident audit log
    ├── services/     # Business logic
    └── api/          # API routes

backend/tests/        # Backend tests

frontend/
└── src/
    ├── app/          # Dashboard, checkout, audit pages, etc.
    ├── components/   # UI components
    └── lib/          # API client and shared types

docs/                 # Detailed project documentation
```

---

## Pages in the App

| Route            | What it does                                             |
| ---------------- | -------------------------------------------------------- |
| `/`              | Dashboard showing failed and recovered payments          |
| `/payments/[id]` | Shows payment details and failure information            |
| `/recovery/[id]` | Shows the AI recommendation, policy checks, and approval |
| `/checkout/[id]` | Customer-facing payment retry page                       |
| `/audit`         | Shows the audit log and integrity check                  |
| `/policy`        | Shows the current rules and limits                       |

---

## Running the Project

### Windows

```bash
dev.bat demo
dev.bat start
```

You can also run the setup scripts directly:

```text
SETUP-WINDOWS.bat
START-WINDOWS.bat
```

### macOS / Linux

```bash
python3 dev.py demo
python3 dev.py start
```

Then open:

```text
http://localhost:3000
```

### Requirements

* Python 3.10–3.13
* Node.js 18.18+

To check whether your environment is ready:

```bash
python dev.py doctor
```

---

## Running Without API Keys

The project is designed to work locally even without external API keys.

| If you don't have | What happens                      |
| ----------------- | --------------------------------- |
| Gemini API key    | Rule-based recovery logic is used |
| Razorpay keys     | Local simulated gateway is used   |
| Trained ML model  | A heuristic estimate is used      |

The UI shows which mode is currently active.

To enable Gemini and Razorpay, create `backend/.env`:

```env
GEMINI_API_KEY=...
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
```

Only Razorpay test keys are accepted. The application refuses to start if live Razorpay keys are provided.

---

## Commands

```bash
python dev.py doctor   # Check the environment
python dev.py setup    # Install dependencies
python dev.py seed     # Reset demo data
python dev.py train    # Train the ML model
python dev.py test     # Run tests
python dev.py start    # Start the application
python dev.py demo     # Setup + seed + train
```

On Windows, use `dev.bat` instead of `python dev.py`.

---

## Testing

The backend tests cover the policy rules and payment flow.

Run:

```bash
python dev.py test
```

For the frontend:

```bash
npx tsc --noEmit
```

and:

```bash
npm run build
```

GitHub Actions runs these checks on both Windows and Linux without requiring API keys.

There is also a specific test that makes sure the AI does not have access to a money-moving/payment-creation tool.

---

## Limitations

This is a project/demo implementation, so there are a few things that are intentionally not production-ready.

### ML data is synthetic

The ML model is trained on generated data because there isn't a suitable public dataset of real payment recovery attempts. The data-generation process is documented in `docs/05-ML-MODEL.md`.

The model and evaluation pipeline are real, but the training labels are synthetic.

### Retry scheduling isn't implemented yet

The **"Retry later"** option currently records the decision and reason. It doesn't actually schedule a future retry.

A production version would need a proper job scheduler for this.

### One merchant and one currency

The current implementation is designed around one merchant and Indian currency values stored in paise.

Supporting multiple merchants and currencies would require additional changes.

### No authentication

There is currently no login or role-based access control. The person approving a payment enters their name manually.

The application is intended to run locally on `127.0.0.1`, so this is acceptable for the current demo. A production version would need proper authentication, authorization, and protection for customer information.

---

## Documentation

More detailed information is available in the `docs/` directory:

* `01-ARCHITECTURE.md` — System architecture
* `02-AGENT-DESIGN.md` — AI agent design
* `03-GUARDRAILS.md` — The 13 policy rules
* `04-API-REFERENCE.md` — API endpoints
* `05-ML-MODEL.md` — ML model and training
* `06-SETUP.md` — Setup and troubleshooting
* `07-DEMO-SCRIPT.md` — Demo walkthrough
* `08-DESIGN-DECISIONS.md` — Technical decisions

---

## What's important about the design

The main separation in RecoverAI is:

**AI recommends → Rules decide → Human approves → Payment system executes**

The AI is useful for analyzing failed payments and suggesting recovery actions, but it never gets direct control over the payment system.

That separation is the core design decision behind the project.

---

**Made by Reshu Kumari**
