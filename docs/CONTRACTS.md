# RecoverAI — Internal Build Contract

> This file is the **single source of truth for module interfaces**. Every module
> is written against these signatures. If you change one, change it here first.
> Read `backend/app/domain/enums.py`, `backend/app/domain/schemas.py`,
> `backend/app/config.py` and `backend/app/db/models.py` — they already exist and
> are authoritative.

---

## 0. Non-negotiable invariants

| # | Invariant | Enforced by |
|---|---|---|
| I1 | The LLM has **no tool that moves money**. Order creation is unreachable from the agent loop. | `ToolCapability`, `tests/test_agent_tool_safety.py` |
| I2 | The recovery amount always equals the original payment amount. The agent cannot set it. | `AgentRecoveryPlan` has no amount field; rule `R9_AMOUNT_INTEGRITY` |
| I3 | Money is **integer paise** everywhere except the API edge. | `paise_to_rupees()` used only in response builders |
| I4 | Guardrails are re-evaluated **at approval time**, not just at proposal time. | `RecoveryService.approve()` |
| I5 | Every money-moving action requires an explicit human approval. | `R13_HUMAN_APPROVAL` + `settings.require_human_approval` |
| I6 | The audit ledger is append-only and hash-chained. | `app/audit/ledger.py`, `GET /api/audit/verify` |
| I7 | The app runs with **zero credentials**: no Gemini key → rule-based planner; no Razorpay keys → simulated gateway. Both are labelled in the UI. | `AgentMode`, `GatewayMode` |
| I8 | Every state change goes through `RecoveryService._transition`, validated against `ALLOWED_TRANSITIONS`. | `InvalidStateTransition` |
| I9 | Windows-first: `pathlib` only, no shell-outs, no POSIX-only syntax. | `dev.py`, `.gitattributes` |

---

## 1. `app/domain/errors.py`

```python
class RecoverAIError(Exception):
    code: str           # stable machine code, e.g. "guardrail_denied"
    http_status: int
    def __init__(self, message: str, *, detail: dict | None = None) -> None: ...

class NotFoundError(RecoverAIError):          code="not_found";            http_status=404
class DuplicateCaseError(RecoverAIError):     code="duplicate_case";       http_status=409
class InvalidStateTransition(RecoverAIError):  code="invalid_transition";   http_status=409
class GuardrailDenied(RecoverAIError):        code="guardrail_denied";     http_status=403
class ApprovalRequired(RecoverAIError):       code="approval_required";    http_status=403
class GatewayError(RecoverAIError):           code="gateway_error";        http_status=502
class SignatureVerificationError(RecoverAIError): code="invalid_signature"; http_status=400
class ConfigurationError(RecoverAIError):     code="configuration_error";  http_status=500
```
`app/main.py` registers one exception handler that renders any `RecoverAIError`
as `ErrorOut` with its `http_status`.

## 2. `app/db/base.py`

```python
engine                                   # created from settings.database_url
SessionLocal: sessionmaker[Session]
def get_db() -> Iterator[Session]        # FastAPI dependency; closes on exit
def init_db() -> None                    # Base.metadata.create_all(engine)
def reset_db() -> None                   # drop_all + create_all (seed/tests only)
```
SQLite requires `connect_args={"check_same_thread": False}` and a
`PRAGMA foreign_keys=ON` event listener.

## 3. `app/audit/ledger.py`

```python
GENESIS_HASH: str = "0" * 64

def compute_event_hash(*, sequence: int, prev_hash: str, event_type: str,
                       actor_type: str, actor_id: str, case_id: str | None,
                       payment_id: str | None, summary: str,
                       payload: dict, created_at: datetime) -> str
    # sha256 over a canonical JSON blob (sort_keys=True, ISO-8601 UTC timestamps)

class AuditLedger:
    def __init__(self, db: Session) -> None
    def record(self, *, event_type: AuditEventType, actor_type: ActorType,
               summary: str, actor_id: str = "system",
               payload: dict | None = None, case_id: str | None = None,
               payment_id: str | None = None) -> AuditEvent
        # allocates the next sequence, links prev_hash, flushes (does NOT commit)
    def head(self) -> AuditEvent | None
    def verify_chain(self) -> AuditChainVerificationOut
```

## 4. `app/policy/rules.py`

```python
@dataclass(frozen=True)
class GuardrailEvaluation:
    rule_id: str; name: str; description: str
    decision: GuardrailDecision; passed: bool; reason: str
    observed: str | None = None; limit: str | None = None

@dataclass(frozen=True)
class GuardrailContext:
    payment: Payment; customer: Customer
    strategy: RecoveryStrategy; failure_category: FailureCategory
    propensity_score: float; amount_paise: int; attempt_number: int
    now: datetime; last_attempt_at: datetime | None
    open_attempt_exists: bool
    daily_recovery_total_paise: int; customer_cases_today: int
    settings: Settings

RuleFn = Callable[[GuardrailContext], GuardrailEvaluation]

@dataclass(frozen=True)
class Rule:
    rule_id: str; name: str; description: str; fn: RuleFn

RULES: tuple[Rule, ...]     # evaluated in declaration order
```

### The 13 rules (rule_id → decision when violated)

| id | name | violation → | logic |
|---|---|---|---|
| `R1_MAX_ATTEMPTS` | Maximum recovery attempts | DENY | `attempt_number > settings.max_recovery_attempts` |
| `R2_COOLDOWN` | Attempt cooldown | DENY | `last_attempt_at` and `now - last_attempt_at < cooldown` |
| `R3_RECOVERABLE_CATEGORY` | Recoverable failure category | DENY | `not failure_category.is_recoverable` |
| `R4_AMOUNT_CEILING` | Absolute amount ceiling | DENY | `amount_paise > settings.max_recovery_amount_paise` |
| `R5_HIGH_VALUE_REVIEW` | High-value review | REQUIRE_APPROVAL | `amount_paise >= settings.high_value_review_threshold_paise` |
| `R6_DUPLICATE_ORDER` | No duplicate open order | DENY | `open_attempt_exists` |
| `R7_DAILY_BUDGET` | Daily recovery budget | DENY | `daily_recovery_total_paise + amount_paise > settings.daily_recovery_budget_paise` |
| `R8_CUSTOMER_VELOCITY` | Per-customer velocity | DENY | `customer_cases_today >= settings.max_cases_per_customer_per_day` |
| `R9_AMOUNT_INTEGRITY` | Amount integrity | DENY | `amount_paise != payment.amount_paise` |
| `R10_PROPENSITY_FLOOR` | Minimum success likelihood | DENY | `propensity_score < settings.min_propensity_score` |
| `R11_PAYMENT_FRESHNESS` | Payment freshness | DENY | `age(payment) > settings.max_payment_age_hours` |
| `R12_CUSTOMER_RISK_FLAG` | Customer risk flag | DENY | `customer.risk_flagged` |
| `R13_HUMAN_APPROVAL` | Explicit human approval | REQUIRE_APPROVAL | always, unless the auto-approve lane qualifies |

**Short-circuit:** when `not ctx.strategy.moves_money`, every rule returns
`ALLOW` with `reason="Not applicable: this strategy creates no payment attempt."`
The *service* then routes the case to `NO_ACTION` or `ESCALATED`.

**Auto-approve lane (R13):** returns `ALLOW` only when
`settings.auto_approve_enabled and not settings.require_human_approval
and amount_paise <= auto_approve_max_paise and propensity >= auto_approve_min_propensity`.
Default config → always `REQUIRE_APPROVAL`.

## 5. `app/policy/engine.py`

```python
@dataclass(frozen=True)
class GuardrailVerdict:
    decision: GuardrailDecision
    evaluations: list[GuardrailEvaluation]
    blocking_rules: list[str]
    approval_rules: list[str]
    summary: str

class PolicyEngine:
    def __init__(self, settings: Settings) -> None
    def evaluate(self, ctx: GuardrailContext) -> GuardrailVerdict   # most-restrictive-wins
    def policy_snapshot(self) -> dict[str, Any]                      # frozen into the case row
    def rule_catalogue(self) -> list[dict[str, str]]                 # for GET /api/policy
```

## 6. `app/ml/`

```python
# features.py
CATEGORICAL_FEATURES = ["failure_category", "payment_method", "proposed_strategy"]
NUMERIC_FEATURES = ["amount_rupees", "attempt_number", "customer_prior_success_rate",
                    "customer_total_payments", "hours_since_failure", "is_same_method_retry"]
FEATURE_COLUMNS = CATEGORICAL_FEATURES + NUMERIC_FEATURES
TARGET = "recovered"
def build_feature_row(*, failure_category, payment_method, proposed_strategy,
                      amount_paise, attempt_number, customer_prior_success_rate,
                      customer_total_payments, hours_since_failure) -> dict[str, Any]
    # derives is_same_method_retry from (payment_method, proposed_strategy)

# dataset.py
CATEGORY_BASE_RATE: dict[FailureCategory, float]     # documented ground truth
STRATEGY_FIT: dict[tuple[FailureCategory, RecoveryStrategy], float]
def generate_dataset(n_samples: int = 18000, seed: int = 42) -> pd.DataFrame

# train.py — runnable as `python -m app.ml.train`
def train_and_save(n_samples=DEFAULT_N_SAMPLES, seed=42, out_dir=MODELS_DIR) -> dict   # returns metrics
def main() -> None
# writes models/propensity_model.joblib and models/metrics.json
# metrics.json: {model_version, trained_at, n_samples, algorithm, accuracy, precision,
#                recall, f1, roc_auc, cv_roc_auc_mean, cv_roc_auc_std,
#                baseline: {...DecisionTree...}, feature_importances: {...},
#                confusion_matrix: [[tn, fp], [fn, tp]], threshold}

# predictor.py
MODEL_FILENAME = "propensity_model.joblib"
class PropensityPredictor:
    is_loaded: bool
    model_version: str
    def predict(self, features: dict[str, Any]) -> PropensityResultOut
def get_predictor() -> PropensityPredictor          # lru_cache singleton
def reset_predictor() -> None                        # tests
```
`predict()` must **never raise**. If the artefact is missing/corrupt it uses the
documented heuristic and returns `is_fallback=True`.

## 7. `app/agent/taxonomy.py`

```python
@dataclass(frozen=True)
class TaxonomyMatch:
    category: FailureCategory; confidence: float
    matched_on: str                # "error_code" | "reason" | "description" | "source" | "default"
    evidence: list[str]

ERROR_CODE_MAP: dict[str, FailureCategory]     # Razorpay codes, uppercased keys
REASON_MAP: dict[str, FailureCategory]         # Razorpay `error.reason` values
DESCRIPTION_PATTERNS: tuple[tuple[str, FailureCategory], ...]   # lowercase substrings

def classify_error(*, error_code=None, error_reason=None, error_description=None,
                   error_source=None, error_step=None, method=None) -> TaxonomyMatch

@dataclass(frozen=True)
class Playbook:
    category: FailureCategory
    primary_strategy: RecoveryStrategy
    alternate_strategy: RecoveryStrategy
    reasoning: str
    customer_message: str
    typical_success_rate: float

PLAYBOOK: dict[FailureCategory, Playbook]      # covers EVERY FailureCategory member
```

## 8. `app/agent/tools.py`

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str; description: str; capability: ToolCapability
    parameters: dict[str, Any]                 # JSON Schema (Gemini function declaration)
    fn: Callable[..., dict[str, Any]]          # returns a JSON-serialisable dict

class ToolRegistry:
    def __init__(self, db: Session, settings: Settings, payment: Payment) -> None
    def specs(self) -> list[ToolSpec]
    def get(self, name: str) -> ToolSpec
    def call(self, name: str, arguments: dict) -> dict     # never raises; returns {"error": ...}

TERMINAL_TOOL = "submit_recovery_plan"
```
Six read-only tools + one terminal proposal tool:
`get_payment_details`, `get_customer_history`, `classify_failure_code`,
`score_recovery_propensity`, `get_recovery_policy`, `check_recovery_eligibility`,
`submit_recovery_plan` (capability `WRITE_PROPOSAL`).
**No tool may have `capability == ToolCapability.FINANCIAL`.**

## 9. `app/agent/llm.py`

```python
class LLMUnavailable(RuntimeError): ...

@dataclass
class LLMStep:
    step: int; tool_name: str; arguments: dict; result: dict
    ok: bool; error: str | None; latency_ms: int

class GeminiClient:
    def __init__(self, settings: Settings) -> None
    @property
    def available(self) -> bool          # key present AND google.genai importable
    def run_tool_loop(self, *, system_prompt: str, user_prompt: str,
                      registry: ToolRegistry, max_steps: int,
                      on_step: Callable[[LLMStep], None]) -> AgentRecoveryPlan
        # raises LLMUnavailable on any failure -> orchestrator falls back
```
`import google.genai` is wrapped in try/except at module import; a missing package
sets `available = False` rather than breaking the app.

## 10. `app/agent/rule_planner.py`

```python
def plan_from_rules(*, payment: Payment, customer: Customer,
                    match: TaxonomyMatch, propensity: PropensityResultOut) -> AgentRecoveryPlan
```
Deterministic. Uses `PLAYBOOK`, downgrades to `MANUAL_REVIEW` for
`UNKNOWN`/`RISK_BLOCKED`, and to `RETRY_LATER` for `INSUFFICIENT_FUNDS`.

## 11. `app/agent/orchestrator.py`

```python
@dataclass
class AnalysisResult:
    plan: AgentRecoveryPlan
    run: AgentRunOut
    propensity: PropensityResultOut
    taxonomy: TaxonomyMatch

class RecoveryAgent:
    def __init__(self, db: Session, settings: Settings) -> None
    def analyze(self, payment: Payment, *, force_rule_based: bool = False) -> AnalysisResult
```
Persists one `AgentToolCall` row per step (including the rule-based path, which
records synthetic steps so the trace view is never empty).

## 12. `app/payments/gateway.py`

```python
@dataclass(frozen=True)
class GatewayOrder:  id: str; amount_paise: int; currency: str; receipt: str; status: str
@dataclass(frozen=True)
class GatewayPayment: id: str; order_id: str; amount_paise: int; status: str; method: str

class PaymentGateway(Protocol):
    mode: GatewayMode
    key_id: str | None
    def create_order(self, *, amount_paise: int, currency: str, receipt: str,
                     notes: dict[str, str]) -> GatewayOrder
    def fetch_payment(self, payment_id: str) -> GatewayPayment
    def verify_payment_signature(self, *, order_id: str, payment_id: str, signature: str) -> bool

class RazorpayGateway:    # httpx Basic auth against https://api.razorpay.com/v1
class SimulatedGateway:   # in-process; same HMAC-SHA256 verification path
    def simulate_payment(self, order_id: str, *, succeed: bool = True) -> tuple[str, str]
        # returns (payment_id, signature) — a REAL HMAC over "order_id|payment_id"

def get_gateway(settings: Settings) -> PaymentGateway    # lru_cache
def reset_gateway() -> None
```
Signature algorithm (identical in both gateways, per Razorpay's spec):
`hmac_sha256(key=secret, msg=f"{order_id}|{payment_id}").hexdigest()`, compared
with `hmac.compare_digest`.

## 13. `app/payments/webhook.py`

```python
def verify_webhook_signature(*, body: bytes, signature: str, secret: str) -> bool
def parse_payment_failed(event: dict) -> dict   # -> normalised failure fields
```

## 14. `app/services/`

```python
class PaymentService:
    def __init__(self, db: Session) -> None
    def list_payments(self, *, status: str = "all", limit: int = 100, offset: int = 0) -> list[PaymentOut]
    def get_payment(self, payment_id: str) -> Payment                 # raises NotFoundError
    def to_out(self, payment: Payment) -> PaymentOut
    def simulate_failure(self, body: SimulateFailureIn) -> PaymentOut
    def record_webhook_failure(self, normalised: dict) -> Payment

class RecoveryService:
    def __init__(self, db: Session, settings: Settings) -> None
    def analyze_payment(self, payment_id: str, body: AnalyzeIn) -> RecoveryCaseOut
    def list_cases(self, *, status: str = "all", limit: int = 100) -> list[RecoveryCaseSummaryOut]
    def get_case(self, case_id: str) -> RecoveryCase
    def to_out(self, case: RecoveryCase) -> RecoveryCaseOut
    def trace(self, case_id: str) -> list[AgentToolCallOut]
    def approve(self, case_id: str, body: ApproveIn) -> RecoveryCaseOut     # re-evaluates guardrails
    def reject(self, case_id: str, body: RejectIn) -> RecoveryCaseOut
    def checkout_session(self, case_id: str) -> CheckoutSessionOut
    def verify_payment(self, case_id: str, body: VerifyPaymentIn) -> RecoveryCaseOut
    def simulate_checkout(self, case_id: str, *, succeed: bool) -> RecoveryCaseOut   # simulated mode only
    def mark_attempt_failed(self, case_id: str, body: MarkAttemptFailedIn) -> RecoveryCaseOut
    def expire_stale_cases(self) -> int

class MetricsService:
    def __init__(self, db: Session) -> None
    def dashboard(self) -> DashboardMetricsOut
    def failure_breakdown(self) -> list[FailureBreakdownItem]
```

### `approve()` order of operations (this sequence is the product)
1. Load case; assert `status == AWAITING_APPROVAL` (else `InvalidStateTransition`).
2. **Re-evaluate every guardrail** against live state. Time has passed since the
   proposal: budgets move, attempts accumulate, cases expire.
3. `DENY` → transition to `BLOCKED`, audit `RECOVERY_BLOCKED`, raise `GuardrailDenied`.
4. Transition `APPROVED`; audit `APPROVAL_GRANTED` with the operator's name.
5. Transition `EXECUTING`. Create the `RecoveryAttempt` row with idempotency key
   `f"{case_id}:{attempt_number}"`. If that key already exists, **reuse** the row
   (idempotent replay) instead of creating a second order.
6. Call `gateway.create_order(...)` with `amount_paise = case.amount_paise`
   (from the payment, never from the plan).
7. Transition `AWAITING_PAYMENT`; set `expires_at`; audit `RECOVERY_ORDER_CREATED`
   and `RECOVERY_LINK_SENT` (with the customer link URL).
8. Commit once, at the end.

## 15. REST API

Base path `/api`. All errors use `ErrorOut`.

| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/health` | → `{"status":"ok"}` |
| GET | `/api/status` | → `SystemStatusOut` |
| GET | `/api/metrics/dashboard` | → `DashboardMetricsOut` |
| GET | `/api/metrics/failure-breakdown` | → `list[FailureBreakdownItem]` |
| GET | `/api/payments?status=&limit=&offset=` | → `list[PaymentOut]` |
| GET | `/api/payments/{payment_id}` | → `PaymentOut` |
| POST | `/api/payments/simulate-failure` | `SimulateFailureIn` → `PaymentOut` (201) |
| GET | `/api/customers/{customer_id}` | → `CustomerOut` |
| POST | `/api/recovery/payments/{payment_id}/analyze` | `AnalyzeIn` → `RecoveryCaseOut` (201) |
| GET | `/api/recovery/cases?status=&limit=` | → `list[RecoveryCaseSummaryOut]` |
| GET | `/api/recovery/cases/{case_id}` | → `RecoveryCaseOut` |
| GET | `/api/recovery/cases/{case_id}/trace` | → `list[AgentToolCallOut]` |
| POST | `/api/recovery/cases/{case_id}/approve` | `ApproveIn` → `RecoveryCaseOut` |
| POST | `/api/recovery/cases/{case_id}/reject` | `RejectIn` → `RecoveryCaseOut` |
| GET | `/api/recovery/cases/{case_id}/checkout` | → `CheckoutSessionOut` |
| POST | `/api/recovery/cases/{case_id}/verify` | `VerifyPaymentIn` → `RecoveryCaseOut` |
| POST | `/api/recovery/cases/{case_id}/simulate-checkout?succeed=bool` | → `RecoveryCaseOut` |
| POST | `/api/recovery/cases/{case_id}/mark-failed` | `MarkAttemptFailedIn` → `RecoveryCaseOut` |
| GET | `/api/audit?case_id=&limit=` | → `list[AuditEventOut]` |
| GET | `/api/audit/verify` | → `AuditChainVerificationOut` |
| GET | `/api/policy` | → `PolicyOut` |
| POST | `/api/webhooks/razorpay` | raw body + `X-Razorpay-Signature` → `{"received": true}` |

## 16. Frontend

Next.js 15 App Router, TypeScript strict, Tailwind v3, shadcn-style components
vendored into `src/components/ui/` (no CLI, no network at build time), Zod for
response validation at the API boundary, `lucide-react` for icons.
System font stack — **no `next/font/google`**, so an offline build still works.

| Route | Purpose |
|---|---|
| `/` | Merchant dashboard: KPIs, failure breakdown, failed payments, recovery queue |
| `/payments/[paymentId]` | Payment detail + "Analyse with RecoverAI" |
| `/recovery/[caseId]` | The decision screen: classification, propensity, guardrail checklist, agent trace, approve/reject, audit timeline |
| `/checkout/[caseId]` | Customer-facing recovery page (Razorpay Checkout or simulated) |
| `/audit` | Full ledger + chain verification |
| `/policy` | Read-only guardrail configuration |

`src/lib/types.ts` mirrors `app/domain/schemas.py` exactly (same string unions as
the Python enums). `src/lib/api.ts` is the only place `fetch` is called.
