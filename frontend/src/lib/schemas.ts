/**
 * Runtime validation for the API boundary.
 *
 * `src/lib/types.ts` mirrors the Python schemas by hand, which is a deliberate
 * choice (see docs/08-DESIGN-DECISIONS.md, D16) but comes with an obvious risk:
 * a TypeScript interface is erased at build time, so if the backend renames a
 * field the compiler is perfectly happy and the value silently becomes
 * `undefined` somewhere deep in a component. On the approval screen, a field
 * that quietly becomes `undefined` is not a rendering glitch -- `can_approve`
 * arriving as `undefined` reads as falsy, and the operator is told a case cannot
 * be approved when policy says it can.
 *
 * These schemas are where that drift is caught: at the boundary, immediately,
 * with a message naming the field.
 *
 * **Why not validate every response.** Zod parsing costs bundle size and a little
 * time per call, and for most payloads a missing field is visibly cosmetic -- a
 * blank cell in a table that a reviewer would notice at a glance. The schemas
 * below cover the responses where silent drift would change a *decision* or
 * undermine a *claim*:
 *
 *   - `RecoveryCase`      the approval screen: amounts, guardrails, can_approve
 *   - `CheckoutSession`   the money screen: what the customer is asked to pay
 *   - `AuditChainVerification`  the integrity claim itself
 *   - `DashboardMetrics`  the headline numbers
 *   - `Policy` / `SystemStatus`  the limits, and the honesty badges
 *
 * Objects are non-strict on purpose: Zod strips unknown keys rather than
 * rejecting them, so the backend adding a field never breaks a deployed client.
 * The direction that matters is a field going *missing*, and that still fails.
 */

import { z } from "zod";

import {
  ACTOR_TYPE_LABEL,
  AGENT_MODE_LABEL,
  AUDIT_EVENT_LABEL,
  FAILURE_CATEGORY_LABEL,
  GATEWAY_MODE_LABEL,
  GUARDRAIL_DECISION_LABEL,
  PAYMENT_METHOD_LABEL,
  PAYMENT_STATUS_LABEL,
  RECOVERY_STATUS_LABEL,
  RECOVERY_STRATEGY_LABEL,
  TOOL_CAPABILITY_LABEL,
  type AuditChainVerification,
  type CheckoutSession,
  type DashboardMetrics,
  type Policy,
  type RecoveryCase,
  type SystemStatus,
} from "@/lib/types";

/**
 * Build a Zod enum from one of the display-label maps in `types.ts`.
 *
 * The label maps are typed `Record<Union, string>`, so their keys are exactly the
 * union members -- which makes them the single source of truth for both the
 * compile-time type and this runtime check. Writing the literals out again here
 * would create a third copy to keep in sync, and the copy that drifts is always
 * the one nobody is looking at.
 *
 * The cast is unavoidable: `z.enum` wants a non-empty tuple of literals and
 * `Object.keys` can only be known to return `string[]`. It is sound here because
 * the input is a `Record` over a string union, which cannot be empty in practice.
 */
function enumOfLabels<T extends string>(labels: Record<T, unknown>) {
  const values = Object.keys(labels) as [T, ...T[]];
  return z.enum(values);
}

const paymentStatus = enumOfLabels(PAYMENT_STATUS_LABEL);
const paymentMethod = enumOfLabels(PAYMENT_METHOD_LABEL);
const failureCategory = enumOfLabels(FAILURE_CATEGORY_LABEL);
const recoveryStrategy = enumOfLabels(RECOVERY_STRATEGY_LABEL);
const recoveryStatus = enumOfLabels(RECOVERY_STATUS_LABEL);
const guardrailDecision = enumOfLabels(GUARDRAIL_DECISION_LABEL);
const actorType = enumOfLabels(ACTOR_TYPE_LABEL);
const auditEventType = enumOfLabels(AUDIT_EVENT_LABEL);
const agentMode = enumOfLabels(AGENT_MODE_LABEL);
const gatewayMode = enumOfLabels(GATEWAY_MODE_LABEL);
const toolCapability = enumOfLabels(TOOL_CAPABILITY_LABEL);

/** Free-form JSON, for payloads and policy snapshots we render but never branch on. */
const jsonRecord = z.record(z.string(), z.unknown());

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

export const customerSchema = z.object({
  id: z.string(),
  name: z.string(),
  email: z.string(),
  phone: z.string(),
  risk_flagged: z.boolean(),
  total_payments: z.number(),
  successful_payments: z.number(),
  prior_success_rate: z.number(),
  lifetime_value_paise: z.number(),
  lifetime_value_rupees: z.number(),
});

export const paymentSchema = z.object({
  id: z.string(),
  customer_id: z.string(),
  customer: customerSchema.nullable(),
  amount_paise: z.number(),
  amount_rupees: z.number(),
  currency: z.string(),
  method: paymentMethod,
  status: paymentStatus,
  description: z.string(),
  razorpay_order_id: z.string().nullable(),
  razorpay_payment_id: z.string().nullable(),
  error_code: z.string().nullable(),
  error_source: z.string().nullable(),
  error_step: z.string().nullable(),
  error_reason: z.string().nullable(),
  error_description: z.string().nullable(),
  is_recovery_attempt: z.boolean(),
  parent_payment_id: z.string().nullable(),
  recovery_case_id: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});

export const guardrailEvaluationSchema = z.object({
  rule_id: z.string(),
  name: z.string(),
  description: z.string(),
  decision: guardrailDecision,
  passed: z.boolean(),
  reason: z.string(),
  observed: z.string().nullable(),
  limit: z.string().nullable(),
  // Defaulted rather than required: this field was added after the first
  // version of the checklist, and an older backend omitting it should degrade to
  // "every rule was consulted" -- the previous behaviour -- rather than blanking
  // the whole approval screen.
  applicable: z.boolean().default(true),
});

export const recoveryAttemptSchema = z.object({
  id: z.string(),
  attempt_number: z.number(),
  strategy: recoveryStrategy,
  amount_paise: z.number(),
  amount_rupees: z.number(),
  status: z.string(),
  gateway_mode: gatewayMode,
  razorpay_order_id: z.string().nullable(),
  razorpay_payment_id: z.string().nullable(),
  failure_reason: z.string().nullable(),
  created_at: z.string(),
  completed_at: z.string().nullable(),
});

export const agentToolCallSchema = z.object({
  id: z.string(),
  run_id: z.string(),
  step: z.number(),
  tool_name: z.string(),
  capability: toolCapability,
  arguments: jsonRecord,
  result: jsonRecord,
  ok: z.boolean(),
  error: z.string().nullable(),
  latency_ms: z.number(),
  created_at: z.string(),
});

export const auditEventSchema = z.object({
  id: z.string(),
  sequence: z.number(),
  case_id: z.string().nullable(),
  payment_id: z.string().nullable(),
  event_type: auditEventType,
  actor_type: actorType,
  actor_id: z.string(),
  summary: z.string(),
  payload: jsonRecord,
  prev_hash: z.string(),
  hash: z.string(),
  created_at: z.string(),
});

// ---------------------------------------------------------------------------
// Decision-critical responses
// ---------------------------------------------------------------------------

export const recoveryCaseSchema = z.object({
  id: z.string(),
  original_payment_id: z.string(),
  customer_id: z.string(),
  customer_name: z.string(),
  status: recoveryStatus,
  failure_category: failureCategory,
  strategy: recoveryStrategy,
  guardrail_decision: guardrailDecision,
  propensity_score: z.number(),
  agent_mode: agentMode,
  amount_paise: z.number(),
  amount_rupees: z.number(),
  attempt_count: z.number(),
  created_at: z.string(),
  updated_at: z.string(),

  classification_confidence: z.number(),
  agent_rationale: z.string(),
  customer_message: z.string(),
  propensity_model_version: z.string(),
  propensity_is_fallback: z.boolean(),

  guardrail_evaluations: z.array(guardrailEvaluationSchema),
  policy_snapshot: jsonRecord,

  approved_by: z.string().nullable(),
  approved_at: z.string().nullable(),
  rejected_by: z.string().nullable(),
  rejected_at: z.string().nullable(),
  rejection_reason: z.string().nullable(),

  recovered_at: z.string().nullable(),
  recovered_amount_paise: z.number(),
  recovered_amount_rupees: z.number(),
  failure_note: z.string().nullable(),
  expires_at: z.string().nullable(),

  original_payment: paymentSchema.nullable(),
  customer: customerSchema.nullable(),
  attempts: z.array(recoveryAttemptSchema),

  // The three fields that make this schema worth its weight. They are computed
  // server-side precisely so the client never re-implements policy; if any of
  // them went missing, `undefined` would read as `false` and the UI would refuse
  // an approval the policy engine had already allowed.
  can_approve: z.boolean(),
  can_reject: z.boolean(),
  approval_blocked_reason: z.string().nullable(),
}) satisfies z.ZodType<RecoveryCase, z.ZodTypeDef, unknown>;

export const checkoutSessionSchema = z.object({
  case_id: z.string(),
  attempt_id: z.string(),
  order_id: z.string(),
  amount_paise: z.number(),
  amount_rupees: z.number(),
  currency: z.string(),
  razorpay_key_id: z.string().nullable(),
  gateway_mode: gatewayMode,
  customer_name: z.string(),
  customer_email: z.string(),
  customer_phone: z.string(),
  description: z.string(),
  expires_at: z.string().nullable(),
}) satisfies z.ZodType<CheckoutSession, z.ZodTypeDef, unknown>;

export const auditChainVerificationSchema = z.object({
  valid: z.boolean(),
  events_checked: z.number(),
  head_hash: z.string().nullable(),
  broken_at_sequence: z.number().nullable(),
  message: z.string(),
}) satisfies z.ZodType<AuditChainVerification, z.ZodTypeDef, unknown>;

export const dashboardMetricsSchema = z.object({
  total_volume_paise: z.number(),
  captured_volume_paise: z.number(),
  failed_volume_paise: z.number(),
  recoverable_volume_paise: z.number(),
  recovered_volume_paise: z.number(),
  total_volume_rupees: z.number(),
  captured_volume_rupees: z.number(),
  failed_volume_rupees: z.number(),
  recoverable_volume_rupees: z.number(),
  recovered_volume_rupees: z.number(),
  total_payments: z.number(),
  failed_payments: z.number(),
  unanalysed_failures: z.number(),
  cases_total: z.number(),
  cases_awaiting_approval: z.number(),
  cases_blocked: z.number(),
  cases_recovered: z.number(),
  cases_failed: z.number(),
  recovery_rate_pct: z.number(),
  failure_rate_pct: z.number(),
  daily_budget_used_paise: z.number(),
  daily_budget_limit_paise: z.number(),
}) satisfies z.ZodType<DashboardMetrics, z.ZodTypeDef, unknown>;

export const policySchema = z.object({
  max_recovery_attempts: z.number(),
  recovery_cooldown_seconds: z.number(),
  high_value_review_threshold_paise: z.number(),
  max_recovery_amount_paise: z.number(),
  daily_recovery_budget_paise: z.number(),
  max_cases_per_customer_per_day: z.number(),
  min_propensity_score: z.number(),
  max_payment_age_hours: z.number(),
  require_human_approval: z.boolean(),
  auto_approve_enabled: z.boolean(),
  auto_approve_max_paise: z.number(),
  auto_approve_min_propensity: z.number(),
  recovery_link_ttl_minutes: z.number(),
  non_recoverable_categories: z.array(failureCategory),
  rules: z.array(
    z.object({
      rule_id: z.string(),
      name: z.string(),
      description: z.string(),
    }),
  ),
}) satisfies z.ZodType<Policy, z.ZodTypeDef, unknown>;

export const systemStatusSchema = z.object({
  app: z.string(),
  version: z.string(),
  environment: z.string(),
  agent_mode: agentMode,
  gemini_model: z.string().nullable(),
  gateway_mode: gatewayMode,
  ml_model_loaded: z.boolean(),
  ml_model_version: z.string().nullable(),
  database: z.string(),
  warnings: z.array(z.string()),
}) satisfies z.ZodType<SystemStatus, z.ZodTypeDef, unknown>;

/**
 * Validate a decoded response, turning a Zod failure into a message that names
 * the offending field.
 *
 * Thrown as a plain `Error` and then wrapped by `apiFetch` into an
 * `ApiRequestError`, so every consumer keeps handling exactly one error type.
 */
export function parseResponse<T>(
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
  data: unknown,
  label: string,
): T {
  const result = schema.safeParse(data);
  if (result.success) {
    return result.data;
  }
  const detail = result.error.issues
    .slice(0, 4)
    .map((issue) => `${issue.path.join(".") || "(root)"}: ${issue.message}`)
    .join("; ");
  throw new Error(
    `The API returned a ${label} that does not match the expected shape (${detail}). ` +
      "The frontend types and the backend schema have drifted - check " +
      "backend/app/domain/schemas.py against frontend/src/lib/types.ts.",
  );
}
