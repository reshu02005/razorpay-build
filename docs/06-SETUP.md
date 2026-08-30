# Setup & Troubleshooting

Written primarily for **Windows**, because that is where this project is expected to be run
and where the sharp edges are. macOS and Linux notes follow each section.

---

## What you need

| | Version | Where |
|---|---|---|
| **Python** | 3.10 – 3.13 (3.12 or 3.13 recommended) | <https://www.python.org/downloads/> |
| **Node.js** | 18.18 or newer (LTS is ideal) | <https://nodejs.org/> |

Nothing else. No Docker, no database server, no `make`, no build tools, no API keys.

> **Windows: tick "Add python.exe to PATH" in the Python installer.** If you miss it,
> `python` will not be found and Windows may open the Microsoft Store instead. If that
> happens, re-run the installer, choose *Modify*, and enable the PATH option — or use
> `py` instead of `python`, which the installer always registers.

After installing either tool, **open a new terminal**. PATH changes do not apply to windows
that were already open — this is the single most common "I installed it and it still says
not found" cause.

---

## Install and run

### Windows — the short way

1. Double-click **`SETUP-WINDOWS.bat`** and wait. It creates the virtual environment,
   installs both dependency sets, builds the demo database and trains the ML model.
2. Double-click **`START-WINDOWS.bat`**.
3. Open <http://localhost:3000>.

### Windows — from a terminal

Open PowerShell or Command Prompt in the project folder:

```bat
dev.bat doctor
dev.bat demo
dev.bat start
```

### macOS / Linux

```bash
python3 dev.py doctor
python3 dev.py demo
python3 dev.py start
```

`doctor` reports what is present, what is missing, and the exact command or download link
to fix each gap. Run it first; run it again any time something misbehaves.

Two servers start:

| | URL | |
|---|---|---|
| Console | <http://localhost:3000> | What you look at |
| API | <http://127.0.0.1:8000> | Interactive docs at [`/docs`](http://127.0.0.1:8000/docs) |

Stop both with `Ctrl+C`.

---

## Running without any credentials

This is the default and it is fully supported. The app runs end to end with an empty
`.env`, and the interface says so:

| Missing | Behaviour | Badge in the UI |
|---|---|---|
| `GEMINI_API_KEY` | A deterministic planner produces the recovery plan, using the same failure taxonomy, the same ML score and the same guardrails. | `Rule-based` |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | An in-process gateway creates orders and mints **real HMAC signatures**, so genuine server-side signature verification still runs. | `Simulated` |
| Trained model file | A documented heuristic produces the propensity score. | `Heuristic estimate` |

Nothing is hidden or faked silently. If you want to see the fully-credentialed behaviour,
add keys as below.

---

## Adding Google Gemini (optional)

1. Get a free key at <https://aistudio.google.com/apikey>.
2. Open `backend/.env` and set:

   ```ini
   GEMINI_API_KEY=your_key_here
   GEMINI_MODEL=gemini-2.5-flash
   ```

3. Restart the backend. The badge changes from `Rule-based` to `Gemini`, and the agent trace
   on a recovery case will show the model's real tool calls in the order it made them.

If the key is wrong, or the API is unreachable, or the model returns something unusable, the
run falls back to the rule-based planner and records why. Analysis never fails outright.

---

## Adding Razorpay Test Mode (optional)

1. Sign in at <https://dashboard.razorpay.com/> and **switch the account to Test Mode**
   (the toggle at the top of the dashboard).
2. Go to **Settings → API Keys → Generate Test Key**.
3. Put both values in `backend/.env`:

   ```ini
   RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxx
   RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxx
   ```

4. Restart the backend. The `Simulated` banner disappears and approving a case creates a
   **real order** on your Razorpay Test account.

5. On the customer checkout screen, pay with a Razorpay test instrument:

   | | |
   |---|---|
   | Card | `4111 1111 1111 1111`, any future expiry, any CVV |
   | UPI (success) | `success@razorpay` |
   | UPI (failure) | `failure@razorpay` |

   Razorpay's current test-instrument list is at
   <https://razorpay.com/docs/payments/payments/test-card-details/>.

> A key beginning `rzp_live_` is **rejected at start-up**, deliberately. This project is a
> Test Mode demonstration and it should not be possible to point it at real money by
> changing one line of configuration.

### Webhooks (optional, advanced)

`POST /api/webhooks/razorpay` accepts Razorpay's `payment.failed` events with HMAC
signature verification. It needs a publicly reachable URL, so during local development
you would tunnel with something like ngrok, then set the same secret in both the Razorpay
dashboard and `RAZORPAY_WEBHOOK_SECRET`. The demo does not require this — the
`Simulate a failed payment` button covers the same ingestion path.

---

## Everyday commands

```bash
python dev.py doctor     # Diagnose the environment
python dev.py setup      # venv + pip install + npm install + .env files
python dev.py seed       # Rebuild the demo database from scratch (deterministic)
python dev.py train      # Retrain the propensity model and print its metrics
python dev.py test       # Run the backend test suite
python dev.py backend    # API only
python dev.py frontend   # Console only
python dev.py start      # Both, in one terminal
```

On Windows use `dev.bat` in place of `python dev.py`.

---

## Troubleshooting

### `'python' is not recognized` / the Microsoft Store opens

PATH was not set, or the Store alias is intercepting the command.

- Use `py` instead of `python` (the launcher is always registered by the python.org installer).
- Or re-run the installer → *Modify* → enable **Add python.exe to PATH**.
- Or turn off the aliases: **Settings → Apps → Advanced app settings → App execution
  aliases** → switch off `python.exe` and `python3.exe`.

Then **open a new terminal**.

### `'npm' is not recognized`

Node.js is not installed, or the terminal predates the install. Install the LTS build from
<https://nodejs.org/> and open a new terminal. Verify with `node --version`.

### `pip install` fails while building numpy or scikit-learn

You are almost certainly on a Python version with no prebuilt wheels yet — typically the
newest release, e.g. 3.14. Install **Python 3.13**, then rebuild the environment:

```bat
rmdir /s /q .venv
py -3.13 dev.py setup
```

```bash
rm -rf .venv && python3.13 dev.py setup
```

Wheels mean nothing is compiled, so no Visual C++ Build Tools are required.

### `PowerShell says running scripts is disabled on this system`

This affects `.ps1` scripts. The project uses `.bat` files, which are unaffected — use
`dev.bat`, or run `python dev.py ...` directly.

### The console loads but every panel says the API is unreachable

The backend is not running, or it is on a different port.

1. Open <http://127.0.0.1:8000/docs>. If it does not load, the API is down — start it with
   `dev.bat backend` and read the error it prints.
2. If the API works but the console still cannot reach it, check
   `frontend/.env.local`:

   ```ini
   NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
   ```

   `NEXT_PUBLIC_` values are baked in at build time, so **restart the frontend** after
   changing it.

### Port 3000 or 8000 is already in use

Next.js will offer the next free port and print it — use the URL it prints, and update
`NEXT_PUBLIC_API_BASE_URL` if the API moved. To free a port on Windows:

```bat
netstat -ano | findstr :8000
taskkill /PID <the_pid> /F
```

```bash
lsof -ti:8000 | xargs kill
```

### The dashboard is empty

The database has not been seeded:

```bash
python dev.py seed
```

### `no such table: payments`

Same cause. `python dev.py seed` creates the schema and the demo data.

### The propensity score says "Heuristic estimate"

The model has not been trained yet. Run `python dev.py train`. This is expected on a fresh
clone and the app is fully functional without it.

### Everything is broken and I want to start over

Nothing outside the project folder is touched, so a reset is safe:

```bat
rmdir /s /q .venv
rmdir /s /q frontend\node_modules
del backend\data\recoverai.db
dev.bat demo
```

```bash
rm -rf .venv frontend/node_modules backend/data/recoverai.db
python3 dev.py demo
```

---

## What gets written where

| Path | | Committed? |
|---|---|---|
| `.venv/` | Python virtual environment | No |
| `frontend/node_modules/` | Node dependencies | No |
| `backend/data/recoverai.db` | SQLite database | No |
| `backend/models/*.joblib`, `metrics.json` | Trained model and its metrics | No |
| `backend/.env`, `frontend/.env.local` | Local configuration, may hold keys | **No — never commit** |

All of it is regenerated by `python dev.py demo`, and all of it is inside the project
folder. Deleting the folder removes every trace of the project from the machine.
