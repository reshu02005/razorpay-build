/**
 * Hand-maintained TypeScript mirror of `backend/app/domain/schemas.py` and
 * `backend/app/domain/enums.py`.
 *
 * The Python side is authoritative. Nothing here is generated, and nothing here
 * is checked against the server at build time -- if a field is renamed in
 * FastAPI, this file will happily keep compiling against the old name. The thing
 * that actually catches that drift is `src/lib/schemas.ts`, which re-declares the
 * same shapes as Zod schemas and validates real responses at the API boundary.
 * These interfaces exist for editor completion and compile-time safety; the Zod
 * layer exists for truth at runtime. Keep the two in step.
 *
 * Every union below is a set of string literals rather than a TypeScript `enum`.
 * The Python enums subclass `str`, so the wire format is the literal token, and a
 * string-literal union compares directly against it with no conversion layer.
 *
 * The label and tone maps at the bottom are typed `Record<Union, string>` on
 * purpose. That is not decoration: it means adding a member to a backend enum
 * and mirroring it here without also giving it a label is a *compile error*, not
 * a screen that silently renders a raw token like `upi_timeout` to an operator.
 * If the compiler complains about a missing key in one of those maps, the fix is
 * to write the human-readable copy, never to loosen the type.
 */

// ---------------------------------------------------------------------------
// Enums (mirrors of app/domain/enums.py)
// ---------------------------------------------------------------------------

/** Lifecycle of a single payment attempt; mirrors Razorpay's own vocabulary. */
export type PaymentStatus =
  | "created"
  | "authorized"
  | "captured"
  | "failed"
  | "refunded";

/**
 * Why a payment failed, normalised into categories that imply *different*
 * recovery actions. The taxonomy is keyed by what should be done about a
 * failure, not by what the gateway happens to call it.
 */
export type FailureCategory =
  | "bank_decline"
  | "insufficient_funds"
  | "upi_timeout"
  | "session_expired"
  | "gateway_error"
  | "network_error"
  | "authentication_failed"
  | "invalid_instrument"
  | "risk_blocked"
  | "customer_abandoned"
  | "unknown";

/** Instrument used for the attempt. Drives which recovery strategies apply. */
export type PaymentMethod =
  | "card"
  | "upi"
  | "netbanking"
  | "wallet"
  | "emi"
  | "unknown";

/**
 * The action the agent may recommend. Note the absence of anything that sets an
 * amount or moves money -- the agent can only recommend how to re-present the
 * same amount to the same customer.
 */
export type RecoveryStrategy =
  | "retry_same_method"
  | "switch_to_upi"
  | "switch_to_card"
  | "switch_to_netbanking"
  | "retry_later"
  | "manual_review"
  | "no_recovery";

/**
 * State machine for one recovery case (one case == one failed payment).
 *
 * The legal transitions live in `ALLOWED_TRANSITIONS` on the Python side and are
 * deliberately NOT mirrored here. Re-implementing them in React would create a
 * second, divergent copy of the rules; instead the server precomputes
 * `can_approve` / `can_reject` / `approval_blocked_reason` on `RecoveryCase` and
 * the UI just renders them.
 */
export type RecoveryStatus =
  | "proposed"
  | "blocked"
  | "awaiting_approval"
  | "rejected"
  | "approved"
  | "executing"
  | "awaiting_payment"
  | "recovered"
  | "failed"
  | "expired"
  | "no_action"
  | "escalated";

/** Verdict of a single guardrail rule, and of the engine as a whole. */
export type GuardrailDecision = "allow" | "require_approval" | "deny";

/** Who caused an audit event. Answers "who approved it?" in the ledger. */
export type ActorType = "agent" | "human" | "system" | "webhook";

/** The closed set of entries that can appear in the tamper-evident ledger. */
export type AuditEventType =
  | "payment_failed"
  | "analysis_started"
  | "failure_classified"
  | "propensity_scored"
  | "strategy_proposed"
  | "guardrails_evaluated"
  | "recovery_blocked"
  | "approval_requested"
  | "approval_granted"
  | "approval_rejected"
  | "recovery_order_created"
  | "recovery_link_sent"
  | "payment_verified"
  | "recovery_succeeded"
  | "recovery_failed"
  | "recovery_expired"
  | "webhook_received"
  | "agent_degraded"
  | "gateway_simulated";

/**
 * Security classification for agent tools. `financial` is mirrored even though
 * it must never appear in a trace: if one ever does, the explainability panel
 * should be able to render it in red rather than crash on an unknown token.
 */
export type ToolCapability = "read_only" | "write_proposal" | "financial";

/** Which reasoning engine produced a plan. Surfaced in the UI for honesty. */
export type AgentMode = "llm" | "rule_based";

/** Which payment backend is live. Surfaced in the UI so a demo never lies. */
export type GatewayMode = "razorpay_test" | "simulated";

/** Query filter accepted by `GET /api/payments`. */
export type PaymentStatusFilter =
  | "all"
  | "failed"
  | "captured"
  | "created"
  | "authorized"
  | "refunded";

// ---------------------------------------------------------------------------
// Response shapes (server -> client; the `*Out` models)
// ---------------------------------------------------------------------------

/** `CustomerOut` */
export interface Customer {
  id: string;
  name: string;
  email: string;
  phone: string;
  risk_flagged: boolean;
  total_payments: number;
  successful_payments: number;
  /** 0..1 -- historical success rate, an input to the propensity model. */
  prior_success_rate: number;
  lifetime_value_paise: number;
  lifetime_value_rupees: number;
}

/**
 * `PaymentOut`
 *
 * Money arrives twice: `*_paise` is the integer of record, `*_rupees` is the
 * server's presentation conversion. The UI formats from paise via
 * `formatRupees` in `@/lib/format`; the rupee field is here to mirror the
 * contract, not to be divided or multiplied in a component.
 */
export interface Payment {
  id: string;
  customer_id: string;
  customer: Customer | null;

  amount_paise: number;
  amount_rupees: number;
  currency: string;
  method: PaymentMethod;
  status: PaymentStatus;
  description: string;

  razorpay_order_id: string | null;
  razorpay_payment_id: string | null;

  error_code: string | null;
  error_source: string | null;
  error_step: string | null;
  error_reason: string | null;
  error_description: string | null;

  is_recovery_attempt: boolean;
  parent_payment_id: string | null;

  /**
   * Set on failed payments that have already been analysed. Lets the payments
   * table choose between "Analyse" and "View case" without a second request.
   */
  recovery_case_id: string | null;

  /** ISO-8601 timestamp. */
  created_at: string;
  /** ISO-8601 timestamp. */
  updated_at: string;
}

/**
 * `GuardrailEvaluationOut`
 *
 * `observed` and `limit` are display strings so a new rule can be added on the
 * server and rendered correctly here with no frontend change at all.
 */
export interface FailureScenario {
  key: string;
  label: string;
  method: PaymentMethod;
  expected_category: FailureCategory;
  error_reason: string;
  error_description: string;
}

export interface GuardrailEvaluation {
  rule_id: string;
  name: string;
  description: string;
  decision: GuardrailDecision;
  passed: boolean;
  reason: string;
  observed: string | null;
  limit: string | null;
  /**
   * False when the proposed strategy creates no payment attempt, so the rule had
   * nothing to constrain. Such rows still arrive with `decision: 'allow'` and
   * `passed: true`, because the rule did not object - it was never consulted.
   * Drawing them as green ticks would tell an operator that thirteen checks
   * examined a case none of them looked at, so the checklist greys them instead.
   */
  applicable: boolean;
}

/** `GuardrailVerdictOut` -- aggregate of every rule; most restrictive wins. */
export interface GuardrailVerdict {
  decision: GuardrailDecision;
  evaluations: GuardrailEvaluation[];
  /** rule_ids that returned DENY. */
  blocking_rules: string[];
  /** rule_ids that forced human approval. */
  approval_rules: string[];
  summary: string;
}

/** `AgentToolCallOut` -- one step of the reasoning trace. */
export interface AgentToolCall {
  id: string;
  run_id: string;
  step: number;
  tool_name: string;
  capability: ToolCapability;
  /** Free-form JSON; rendered as a payload, never destructured by field name. */
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
  ok: boolean;
  error: string | null;
  latency_ms: number;
  created_at: string;
}

/**
 * `AgentRecoveryPlan` -- the structured object the agent must produce.
 *
 * There is no amount field, by design: the recovery amount is copied from the
 * original payment by the service layer, so the agent is structurally incapable
 * of influencing it.
 */
export interface AgentRecoveryPlan {
  failure_category: FailureCategory;
  /** 0..1 */
  confidence: number;
  strategy: RecoveryStrategy;
  /** Shown verbatim in the approval UI -- the human judges the real reasoning. */
  rationale: string;
  /** What the customer would be told. Different audience, different tone. */
  customer_message: string;
  evidence: string[];
}

/** `AgentRunOut` -- metadata about one analysis run. */
export interface AgentRun {
  run_id: string;
  mode: AgentMode;
  model: string | null;
  steps: number;
  total_latency_ms: number;
  /** Set when the LLM path was unavailable and the rule engine took over. */
  degraded_reason: string | null;
}

/** `PropensityResultOut` -- prediction from the recovery-propensity model. */
export interface PropensityResult {
  /** 0..1 -- P(recovery attempt succeeds). */
  score: number;
  model_version: string;
  top_factors: string[];
  /** True when the heuristic fallback produced the score, not a trained model. */
  is_fallback: boolean;
}

/** `RecoveryAttemptOut` -- one execution of an approved plan. */
export interface RecoveryAttempt {
  id: string;
  attempt_number: number;
  strategy: RecoveryStrategy;
  amount_paise: number;
  amount_rupees: number;
  /**
   * Free string on the server (`status: str`), not the case state machine.
   * Typed as `string` here rather than narrowed to a union we invented, because
   * inventing a union the backend does not enforce is exactly the kind of drift
   * this file is meant to avoid.
   */
  status: string;
  gateway_mode: GatewayMode;
  razorpay_order_id: string | null;
  razorpay_payment_id: string | null;
  failure_reason: string | null;
  created_at: string;
  completed_at: string | null;
}

/** `RecoveryCaseSummaryOut` -- row shape for list views. No trace, no rules. */
export interface RecoveryCaseSummary {
  id: string;
  original_payment_id: string;
  customer_id: string;
  customer_name: string;
  status: RecoveryStatus;
  failure_category: FailureCategory;
  strategy: RecoveryStrategy;
  guardrail_decision: GuardrailDecision;
  /** 0..1 */
  propensity_score: number;
  agent_mode: AgentMode;
  amount_paise: number;
  amount_rupees: number;
  attempt_count: number;
  created_at: string;
  updated_at: string;
}

/** `RecoveryCaseOut` -- everything the decision screen needs in one response. */
export interface RecoveryCase extends RecoveryCaseSummary {
  /** 0..1 -- how sure the agent is of `failure_category`. */
  classification_confidence: number;
  agent_rationale: string;
  customer_message: string;
  propensity_model_version: string;
  propensity_is_fallback: boolean;

  guardrail_evaluations: GuardrailEvaluation[];
  /** Policy values frozen at proposal time, for the audit record. */
  policy_snapshot: Record<string, unknown>;

  approved_by: string | null;
  approved_at: string | null;
  rejected_by: string | null;
  rejected_at: string | null;
  rejection_reason: string | null;

  recovered_at: string | null;
  recovered_amount_paise: number;
  recovered_amount_rupees: number;
  failure_note: string | null;
  expires_at: string | null;

  original_payment: Payment | null;
  customer: Customer | null;
  attempts: RecoveryAttempt[];

  /**
   * Precomputed by the server from the case's status and its **stored**
   * guardrail verdict. The approve/reject buttons bind straight to these flags:
   * deriving the enabled state in React would turn the frontend into a second,
   * divergent copy of the policy engine, and the copy that drifts is always the
   * one that lets a bad approval through.
   *
   * Note what these are not. They reflect the verdict recorded when the case was
   * proposed, not a fresh evaluation on this read. The binding re-evaluation
   * happens inside `approve()`, at the moment money would move -- which is why
   * an approval can still be refused after this said `can_approve: true`, and
   * why the panel renders the server's refusal message verbatim when it is.
   */
  can_approve: boolean;
  can_reject: boolean;
  /** Why approval is unavailable, in the operator's language. */
  approval_blocked_reason: string | null;
}

/** `CheckoutSessionOut` -- everything the customer recovery page needs. */
export interface CheckoutSession {
  case_id: string;
  attempt_id: string;
  order_id: string;
  amount_paise: number;
  amount_rupees: number;
  currency: string;
  /**
   * Razorpay publishable key, or `null` in simulated mode. Safe in the browser:
   * the key id is public by design and the secret never leaves the server.
   */
  razorpay_key_id: string | null;
  gateway_mode: GatewayMode;
  customer_name: string;
  customer_email: string;
  customer_phone: string;
  description: string;
  expires_at: string | null;
}

/** `AuditEventOut` -- one link in the hash chain. */
export interface AuditEvent {
  id: string;
  /** Monotonic position in the ledger; genesis is the lowest. */
  sequence: number;
  case_id: string | null;
  payment_id: string | null;
  event_type: AuditEventType;
  actor_type: ActorType;
  actor_id: string;
  summary: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  hash: string;
  created_at: string;
}

/** `AuditChainVerificationOut` -- result of recomputing the chain from genesis. */
export interface AuditChainVerification {
  valid: boolean;
  events_checked: number;
  head_hash: string | null;
  /** First sequence number whose recomputed hash did not match, if any. */
  broken_at_sequence: number | null;
  message: string;
}

/** `DashboardMetricsOut` -- merchant-level numbers for the landing dashboard. */
export interface DashboardMetrics {
  total_volume_paise: number;
  captured_volume_paise: number;
  failed_volume_paise: number;
  /** Failed volume that passed guardrails as recoverable. */
  recoverable_volume_paise: number;
  recovered_volume_paise: number;

  total_volume_rupees: number;
  captured_volume_rupees: number;
  failed_volume_rupees: number;
  recoverable_volume_rupees: number;
  recovered_volume_rupees: number;

  total_payments: number;
  failed_payments: number;
  /** Failed payments with no case yet -- the dashboard's primary call to action. */
  unanalysed_failures: number;

  cases_total: number;
  cases_awaiting_approval: number;
  cases_blocked: number;
  cases_recovered: number;
  cases_failed: number;

  /** Already a percentage (0..100), not a fraction. Render with `formatPercent`. */
  recovery_rate_pct: number;
  failure_rate_pct: number;

  daily_budget_used_paise: number;
  daily_budget_limit_paise: number;
}

/** `FailureBreakdownItem` -- one row of the failure-category chart. */
export interface FailureBreakdownItem {
  category: FailureCategory;
  count: number;
  volume_paise: number;
  volume_rupees: number;
  recovered_count: number;
}

/**
 * One entry of `PolicyOut.rules`. The server types this as `dict[str, str]` with
 * a documented set of keys, so the index signature is kept to stay faithful to
 * the contract while the three known keys stay strongly typed.
 */
export interface PolicyRule {
  rule_id: string;
  name: string;
  description: string;
  [key: string]: string;
}

/** `PolicyOut` -- the active guardrail configuration, read-only by design. */
export interface Policy {
  max_recovery_attempts: number;
  recovery_cooldown_seconds: number;
  high_value_review_threshold_paise: number;
  max_recovery_amount_paise: number;
  daily_recovery_budget_paise: number;
  max_cases_per_customer_per_day: number;
  /** 0..1 */
  min_propensity_score: number;
  max_payment_age_hours: number;
  require_human_approval: boolean;
  auto_approve_enabled: boolean;
  auto_approve_max_paise: number;
  /** 0..1 */
  auto_approve_min_propensity: number;
  recovery_link_ttl_minutes: number;
  non_recoverable_categories: FailureCategory[];
  rules: PolicyRule[];
}

/** `SystemStatusOut` -- honest self-report of live vs simulated subsystems. */
export interface SystemStatus {
  app: string;
  version: string;
  environment: string;
  agent_mode: AgentMode;
  gemini_model: string | null;
  gateway_mode: GatewayMode;
  ml_model_loaded: boolean;
  ml_model_version: string | null;
  database: string;
  warnings: string[];
}

/** `ErrorOut` -- the uniform envelope on every non-2xx response. */
export interface ApiError {
  /** Stable machine-readable code, e.g. `guardrail_denied`. */
  error: string;
  message: string;
  detail: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Request bodies (client -> server; the `*In` models)
// ---------------------------------------------------------------------------

/**
 * `SimulateFailureIn` -- demo helper. Every field is optional; an empty POST
 * draws from the seeded scenario catalogue, because a reviewer cannot make a
 * real card decline on demand.
 */
export interface SimulateFailureRequest {
  customer_id?: string;
  amount_paise?: number;
  method?: PaymentMethod;
  /** Named scenario from the seed catalogue, e.g. `bank_decline_card`. */
  scenario?: string;
  description?: string;
}

/** `AnalyzeIn` */
export interface AnalyzeRequest {
  /** Force the deterministic planner even when a Gemini key is configured. */
  force_rule_based?: boolean;
}

/** `ApproveIn` */
export interface ApproveRequest {
  /** Operator identity, recorded in the audit trail. 1..120 chars. */
  approved_by: string;
  note?: string;
}

/** `RejectIn` */
export interface RejectRequest {
  rejected_by: string;
  /** Required: a rejection with no reason teaches the next reviewer nothing. */
  reason: string;
}

/** `VerifyPaymentIn` -- the payload Razorpay Checkout hands back on success. */
export interface VerifyPaymentRequest {
  razorpay_order_id: string;
  razorpay_payment_id: string;
  /** Verified server-side with HMAC-SHA256; a client claim is not proof. */
  razorpay_signature: string;
}

/** `MarkAttemptFailedIn` -- demo helper to exercise the failure path. */
export interface MarkAttemptFailedRequest {
  reason?: string;
}

// ---------------------------------------------------------------------------
// Display maps
// ---------------------------------------------------------------------------

/**
 * The five meanings colour is allowed to carry in this console.
 *
 * `info` is the AI tint (sky), `neutral` is slate. Components translate a tone
 * into classes in exactly one place (`toneClasses` in `@/lib/utils`), so no
 * screen picks a hue for a status by hand.
 */
export type Tone = "success" | "warning" | "danger" | "neutral" | "info";

/**
 * Typed as `Record<FailureCategory, string>`: adding a category to the Python
 * enum, mirroring it above, and forgetting the label breaks the build here
 * instead of leaking `insufficient_funds` into the UI.
 */
export const FAILURE_CATEGORY_LABEL: Record<FailureCategory, string> = {
  bank_decline: "Bank decline",
  insufficient_funds: "Insufficient funds",
  upi_timeout: "UPI timeout",
  session_expired: "Session expired",
  gateway_error: "Gateway error",
  network_error: "Network error",
  authentication_failed: "Authentication failed",
  invalid_instrument: "Invalid instrument",
  risk_blocked: "Risk blocked",
  customer_abandoned: "Customer abandoned",
  unknown: "Unclassified",
};

export const PAYMENT_STATUS_LABEL: Record<PaymentStatus, string> = {
  created: "Created",
  authorized: "Authorized",
  captured: "Captured",
  failed: "Failed",
  refunded: "Refunded",
};

/**
 * `authorized` is amber rather than green: the money is held, not collected, and
 * showing it as a success would overstate what the merchant actually has.
 */
export const PAYMENT_STATUS_TONE: Record<PaymentStatus, Tone> = {
  created: "neutral",
  authorized: "warning",
  captured: "success",
  failed: "danger",
  refunded: "neutral",
};

export const PAYMENT_METHOD_LABEL: Record<PaymentMethod, string> = {
  card: "Card",
  upi: "UPI",
  netbanking: "Netbanking",
  wallet: "Wallet",
  emi: "EMI",
  unknown: "Unknown",
};

export const RECOVERY_STRATEGY_LABEL: Record<RecoveryStrategy, string> = {
  retry_same_method: "Retry same method",
  switch_to_upi: "Switch to UPI",
  switch_to_card: "Switch to card",
  switch_to_netbanking: "Switch to netbanking",
  retry_later: "Retry later",
  manual_review: "Manual review",
  no_recovery: "No recovery",
};

export const RECOVERY_STATUS_LABEL: Record<RecoveryStatus, string> = {
  proposed: "Proposed",
  blocked: "Blocked by guardrails",
  awaiting_approval: "Awaiting approval",
  rejected: "Rejected",
  approved: "Approved",
  executing: "Creating order",
  awaiting_payment: "Awaiting payment",
  recovered: "Recovered",
  failed: "Failed",
  expired: "Expired",
  no_action: "No action needed",
  escalated: "Escalated to human",
};

/**
 * Only `recovered` earns green -- it is the one state where money actually
 * arrived. `blocked` and `rejected` are red because a guardrail or a human said
 * no; `no_action` is neutral because "nothing to do" is a correct outcome, not a
 * failure, and colouring it red would train operators to ignore red.
 */
export const RECOVERY_STATUS_TONE: Record<RecoveryStatus, Tone> = {
  proposed: "info",
  blocked: "danger",
  awaiting_approval: "warning",
  rejected: "danger",
  approved: "info",
  executing: "info",
  awaiting_payment: "warning",
  recovered: "success",
  failed: "danger",
  expired: "neutral",
  no_action: "neutral",
  escalated: "warning",
};

export const GUARDRAIL_DECISION_LABEL: Record<GuardrailDecision, string> = {
  allow: "Allowed",
  require_approval: "Approval required",
  deny: "Denied",
};

export const GUARDRAIL_DECISION_TONE: Record<GuardrailDecision, Tone> = {
  allow: "success",
  require_approval: "warning",
  deny: "danger",
};

export const ACTOR_TYPE_LABEL: Record<ActorType, string> = {
  agent: "Agent",
  human: "Operator",
  system: "System",
  webhook: "Webhook",
};

/**
 * Ledger entries read as a narrative in the audit timeline, so the labels are
 * written as past-tense events rather than as noun phrases.
 */
export const AUDIT_EVENT_LABEL: Record<AuditEventType, string> = {
  payment_failed: "Payment failed",
  analysis_started: "Analysis started",
  failure_classified: "Failure classified",
  propensity_scored: "Propensity scored",
  strategy_proposed: "Strategy proposed",
  guardrails_evaluated: "Guardrails evaluated",
  recovery_blocked: "Recovery blocked",
  approval_requested: "Approval requested",
  approval_granted: "Approval granted",
  approval_rejected: "Approval rejected",
  recovery_order_created: "Recovery order created",
  recovery_link_sent: "Recovery link sent",
  payment_verified: "Payment verified",
  recovery_succeeded: "Recovery succeeded",
  recovery_failed: "Recovery failed",
  recovery_expired: "Recovery expired",
  webhook_received: "Webhook received",
  agent_degraded: "Agent degraded to rules",
  gateway_simulated: "Gateway simulated",
};

export const AGENT_MODE_LABEL: Record<AgentMode, string> = {
  llm: "Gemini",
  rule_based: "Rule-based",
};

export const GATEWAY_MODE_LABEL: Record<GatewayMode, string> = {
  razorpay_test: "Razorpay test mode",
  simulated: "Simulated gateway",
};

export const TOOL_CAPABILITY_LABEL: Record<ToolCapability, string> = {
  read_only: "Read-only",
  write_proposal: "Writes a proposal",
  financial: "Financial",
};

/**
 * A `financial` capability must never appear in a trace -- the agent has no such
 * tool, and a test fails the build if one is ever registered. It is mapped to
 * `danger` so that if the impossible happens it is impossible to miss.
 */
export const TOOL_CAPABILITY_TONE: Record<ToolCapability, Tone> = {
  read_only: "neutral",
  write_proposal: "info",
  financial: "danger",
};
