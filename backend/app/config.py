"""
Application configuration -- one place, one source of truth.

Design rules this file follows (they matter more than the values):

1.  **Defaults live at exactly one layer.** Every default in the system is
    declared here. No other module may re-default a value it was handed; if a
    required value is missing at the point of use, that code raises instead of
    quietly substituting something. This prevents the classic finance bug where
    a limit is enforced as 2 in one file and 5 in another.

2.  **The app must boot with zero credentials.** No Gemini key, no Razorpay key,
    no network -- it still runs, using the rule-based agent and the simulated
    gateway. A reviewer should be able to clone, install and run. Degraded modes
    are surfaced in the API and the UI, never hidden.

3.  **Paths are computed, never hard-coded.** Everything hangs off
    ``PROJECT_ROOT`` via ``pathlib``, so the identical code runs on Windows
    (``C:\\Users\\...\\Reshu_Project``) and on macOS/Linux.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------
# config.py lives at  <root>/backend/app/config.py
#   parents[0] -> <root>/backend/app
#   parents[1] -> <root>/backend      <- BACKEND_DIR
#   parents[2] -> <root>              <- PROJECT_ROOT
BACKEND_DIR: Path = Path(__file__).resolve().parents[1]
PROJECT_ROOT: Path = BACKEND_DIR.parent
DATA_DIR: Path = BACKEND_DIR / "data"
MODELS_DIR: Path = BACKEND_DIR / "models"

# Created eagerly so a first run on a clean checkout cannot fail on a missing
# directory. ``parents=True`` + ``exist_ok=True`` is safe to call repeatedly.
DATA_DIR.mkdir(parents=True, exist_ok=True)
MODELS_DIR.mkdir(parents=True, exist_ok=True)

#: Minimum interpreter we support. 3.10 gives us ``X | Y`` unions and structural
#: pattern matching; we cap the *advice* at 3.13 because scikit-learn/numpy
#: wheels for 3.14 are not universally published yet and a fresher's laptop
#: should never be asked to compile numpy from source.
MIN_PYTHON = (3, 10)
MAX_TESTED_PYTHON = (3, 13)


class Settings(BaseSettings):
    """
    Typed configuration, populated from (in ascending priority):
    class defaults  ->  ``backend/.env``  ->  real environment variables.

    Every field carries a comment explaining *why* the default is what it is.
    """

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Unknown keys in .env are ignored rather than fatal
    )

    # -- Application ------------------------------------------------------
    app_name: str = "RecoverAI"
    app_env: str = Field(default="development", description="development | production")
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"

    #: Origins allowed to call the API from a browser. The Next.js dev server
    #: runs on 3000; 3001 is included because Next silently increments the port
    #: when 3000 is busy, which is a common and confusing first-run failure.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001"

    # -- Persistence ------------------------------------------------------
    #: SQLite keeps the project single-command runnable: no server to install,
    #: no Docker required, and the file is portable between Windows and macOS.
    #: The DSN shape is standard SQLAlchemy, so swapping in Postgres later is a
    #: one-line change with no code edits.
    database_url: str = Field(default_factory=lambda: f"sqlite:///{(DATA_DIR / 'recoverai.db').as_posix()}")
    sql_echo: bool = False

    # -- Google Gemini (the reasoning engine) ------------------------------
    #: Absent key is a supported configuration, not an error: the orchestrator
    #: falls back to the deterministic rule-based planner and stamps the case
    #: with ``AgentMode.RULE_BASED`` so the UI can say so out loud.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: float = 30.0
    #: Hard ceiling on the agent's tool-calling loop. Bounds cost and latency and
    #: guarantees termination even if the model keeps asking for tools.
    agent_max_steps: int = 8

    # -- Razorpay (the execution engine) -----------------------------------
    #: Test-mode keys look like ``rzp_test_...``. When blank we use the in-process
    #: simulator. A production-looking ``rzp_live_`` key is rejected outright --
    #: this project must never be pointed at real money.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    #: Accept webhook deliveries that carry no valid signature.
    #:
    #: OFF, and it must stay off anywhere reachable from the internet: the
    #: webhook endpoint creates payments and customers, so an unauthenticated one
    #: lets anybody put rows into the merchant's approval queue. It exists as a
    #: named switch purely so that ingesting a Razorpay dashboard test event
    #: locally does not require configuring a secret -- and so that the decision
    #: is greppable rather than an implicit consequence of leaving a field blank.
    allow_unsigned_webhooks: bool = False

    razorpay_timeout_seconds: float = 20.0
    #: Currency is fixed because every guardrail limit below is denominated in
    #: paise. Supporting multi-currency would require per-currency limits, which
    #: is deliberately out of scope.
    currency: str = "INR"

    # -- Frontend hand-off --------------------------------------------------
    #: Used to build the customer-facing recovery link written into the audit log.
    frontend_base_url: str = "http://localhost:3000"

    # =====================================================================
    # GUARDRAIL POLICY
    # ---------------------------------------------------------------------
    # These are the limits the AI cannot argue its way past. They are read by
    # app/policy/engine.py and exposed read-only at GET /api/policy so the
    # frontend can render the exact policy that was in force for a decision.
    #
    # All money is in PAISE (integer). Never floats for money: 0.1 + 0.2 != 0.3
    # in binary floating point, and a rounding error in a payment system is a
    # real defect, not a curiosity.
    # =====================================================================

    #: R1 -- how many recovery attempts one failed payment may ever generate.
    #: Two is the industry norm: the second attempt captures most transient
    #: failures, the third mostly annoys the customer and burns gateway calls.
    max_recovery_attempts: int = 2

    #: R2 -- minimum gap between attempts on the same case. Stops a retry storm
    #: and gives an issuer-side transient fault time to clear.
    recovery_cooldown_seconds: int = 900  # 15 minutes

    #: R5 -- above this, a human must look at it even if everything else is green.
    high_value_review_threshold_paise: int = 1_000_000  # Rs 10,000

    #: R4 -- absolute ceiling. Above this the agent may not propose an automated
    #: recovery at all; it must escalate to manual review.
    max_recovery_amount_paise: int = 5_000_000  # Rs 50,000

    #: R7 -- total value of recovery orders this system may create in one day.
    #: A blast-radius cap: if the agent, the data or an operator goes wrong, the
    #: worst-case exposure is bounded and known in advance.
    daily_recovery_budget_paise: int = 20_000_000  # Rs 2,00,000

    #: R8 -- per-customer velocity cap, so one unlucky customer is never chased
    #: repeatedly in a single day.
    max_cases_per_customer_per_day: int = 3

    #: R10 -- below this predicted success probability, attempting recovery is
    #: judged not worth the customer friction. Set from the ML model's
    #: precision/recall trade-off; see docs/05-ML-MODEL.md.
    min_propensity_score: float = 0.15

    #: R11 -- payments older than this are stale; card details and intent have
    #: both likely moved on.
    max_payment_age_hours: int = 168  # 7 days

    #: R13 -- the master switch. When True (the default and the only setting used
    #: in the demo) *every* money-moving action requires an explicit human click.
    require_human_approval: bool = True

    #: Optional low-risk auto-approval lane, OFF by default. Kept in the codebase
    #: because a real merchant would eventually want it, and because it shows the
    #: policy engine is expressive enough to encode graduated autonomy -- but the
    #: submitted demo runs with a human in the loop for every rupee.
    auto_approve_enabled: bool = False
    auto_approve_max_paise: int = 50_000       # Rs 500
    auto_approve_min_propensity: float = 0.80

    #: How long a generated recovery link stays valid before the sweeper expires
    #: the case. Bounds how long a payable order can sit open.
    recovery_link_ttl_minutes: int = 60

    # ---------------------------------------------------------------------
    # Validators
    # ---------------------------------------------------------------------

    @field_validator("razorpay_key_id")
    @classmethod
    def _reject_live_keys(cls, v: str) -> str:
        """
        Refuse to start against a live Razorpay account.

        This is a safety interlock, not a style choice. The project is explicitly
        a Test Mode demonstration; booting with a ``rzp_live_`` key would put real
        customer money behind an AI-authored recommendation.
        """
        if v.startswith("rzp_live_"):
            raise ValueError(
                "Refusing to start with a LIVE Razorpay key. RecoverAI is a Test Mode "
                "project by design. Use a key that starts with 'rzp_test_'."
            )
        return v

    @field_validator("min_propensity_score", "auto_approve_min_propensity")
    @classmethod
    def _probability_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"Probability thresholds must be within [0, 1], got {v}")
        return v

    @field_validator("max_recovery_attempts")
    @classmethod
    def _attempts_sane(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_recovery_attempts must be at least 1")
        if v > 5:
            # Not a hard technical limit -- a policy statement. Chasing a customer
            # more than five times is a support problem, not a payments problem.
            raise ValueError("max_recovery_attempts above 5 is not a supported policy")
        return v

    # ---------------------------------------------------------------------
    # Derived helpers (computed, never stored -- so they cannot drift)
    # ---------------------------------------------------------------------

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def gemini_enabled(self) -> bool:
        """True when a Gemini key is present; drives ``AgentMode``."""
        return bool(self.gemini_api_key.strip())

    @property
    def razorpay_enabled(self) -> bool:
        """True when both Razorpay credentials are present; drives ``GatewayMode``."""
        return bool(self.razorpay_key_id.strip() and self.razorpay_key_secret.strip())

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def check_python_version() -> str | None:
    """
    Return a human-readable warning when the interpreter is outside the tested
    range, or ``None`` when it is fine.

    Called by ``dev.py`` and at API start-up. It warns rather than exits: a newer
    Python usually works, and hard-failing a reviewer's environment over a minor
    version would be worse than a printed caution.
    """
    v = sys.version_info
    if (v.major, v.minor) < MIN_PYTHON:
        return (
            f"Python {v.major}.{v.minor} is too old. RecoverAI needs "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer."
        )
    if (v.major, v.minor) > MAX_TESTED_PYTHON:
        return (
            f"Python {v.major}.{v.minor} is newer than the tested range "
            f"({MIN_PYTHON[0]}.{MIN_PYTHON[1]}-{MAX_TESTED_PYTHON[0]}.{MAX_TESTED_PYTHON[1]}). "
            "If scikit-learn or numpy fail to install, use Python 3.13."
        )
    return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Process-wide singleton.

    Cached so that the ``.env`` file is parsed once and so every module observes
    the identical object -- if configuration could differ between two callers,
    the guardrail limits would not be trustworthy.

    Tests clear this with ``get_settings.cache_clear()``.
    """
    return Settings()


#: Convenience import for modules that just want the values.
settings = get_settings()
