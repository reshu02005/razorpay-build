/**
 * The RecoverAI HTTP client -- the only module in the app that calls `fetch`.
 *
 * Keeping every request behind one function means there is exactly one place
 * that knows the base URL, one place that turns an `ErrorOut` envelope into a
 * throwable, one place that decides caching, and one place to look when a
 * request 404s. Components import `api`, never `fetch`.
 *
 * Response shapes are the interfaces in `@/lib/types`, which are a hand-written
 * mirror of the Pydantic models. This module casts the parsed JSON to them; the
 * Zod schemas in `@/lib/schemas` are what actually prove a response matches at
 * runtime, and screens that care about drift validate there. That layering is
 * intentional: the transport boundary stays free of a build-order dependency on
 * the validation layer, so a broken schema can never stop the app from talking
 * to the server.
 */
import type {
  AgentToolCall,
  AnalyzeRequest,
  ApiError,
  ApproveRequest,
  AuditChainVerification,
  AuditEvent,
  CheckoutSession,
  Customer,
  FailureScenario,
  DashboardMetrics,
  FailureBreakdownItem,
  MarkAttemptFailedRequest,
  Payment,
  PaymentStatusFilter,
  Policy,
  RecoveryCase,
  RecoveryCaseSummary,
  RecoveryStatus,
  RejectRequest,
  SimulateFailureRequest,
  SystemStatus,
  VerifyPaymentRequest,
} from "@/lib/types";

import type { z } from "zod";

import {
  auditChainVerificationSchema,
  checkoutSessionSchema,
  dashboardMetricsSchema,
  parseResponse,
  policySchema,
  recoveryCaseSchema,
  systemStatusSchema,
} from "@/lib/schemas";

/**
 * A non-2xx response, carrying the server's `ErrorOut` envelope.
 *
 * `code` is the stable machine-readable token (e.g. `guardrail_denied`), so a
 * screen can branch on the reason without string-matching the prose. `message`
 * is already written for a human and is safe to render as-is.
 */
export class ApiRequestError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public detail?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

/**
 * Where the FastAPI backend lives.
 *
 * The fallback is not laziness. `NEXT_PUBLIC_*` values are inlined at build
 * time, so a reviewer who clones the repo and runs `npm run dev` without first
 * copying `.env.local.example` would otherwise get requests to `undefined/api/...`
 * and a blank dashboard with no obvious cause. `http://127.0.0.1:8000` is the
 * backend's own default host and port (`backend/app/config.py`), so the zero-
 * configuration path just works. A trailing slash is stripped so that callers
 * can always pass paths beginning with `/`.
 *
 * 127.0.0.1 rather than `localhost`: on Windows, `localhost` can resolve to
 * `::1` first while uvicorn is bound to IPv4, which shows up as a connection
 * refused that looks like the backend is down when it is not.
 */
export const API_BASE_URL: string = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000"
).replace(/\/+$/, "");

/** Shape-check for the `ErrorOut` envelope without trusting the response body. */
function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { error?: unknown }).error === "string" &&
    typeof (value as { message?: unknown }).message === "string"
  );
}

/**
 * Performs one request and returns the decoded body.
 *
 * @param path Absolute path on the API, e.g. `/api/recovery/cases`.
 * @throws {ApiRequestError} on any non-2xx response, or when the network fails.
 */
export async function apiFetch<T>(
  path: string,
  init?: RequestInit,
  // `z.ZodType<T, ZodTypeDef, unknown>`, not `z.ZodType<T>`: the input to a
  // response schema is raw decoded JSON, and a schema that defaults or coerces a
  // field has an input type that differs from its output type. Pinning both ends
  // to `T` would reject exactly those schemas -- the ones doing the most work.
  schema?: z.ZodType<T, z.ZodTypeDef, unknown>,
): Promise<T> {
  const url = `${API_BASE_URL}${path}`;

  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  // Only declare a JSON body when there is one; sending Content-Type on a GET
  // makes some proxies treat it as a body-bearing request.
  if (init?.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers,
      /*
       * Every endpoint here is live operational state: an approval that has
       * already been granted, a case that has already moved to `recovered`, a
       * budget that has already been spent. Serving any of it from a cache is a
       * correctness bug, not a performance trade-off -- an operator acting on a
       * stale `can_approve` is the exact failure this disables.
       */
      cache: "no-store",
    });
  } catch (cause) {
    /*
     * `fetch` rejects (rather than resolving with a status) when the connection
     * itself fails: backend not started, wrong port, CORS pre-flight refused.
     * This message is the single most valuable line in the file for a first-time
     * reviewer -- the default "TypeError: Failed to fetch" says nothing about
     * which URL was tried or what to do next, and "is the backend running?" is
     * the answer roughly every time.
     */
    throw new ApiRequestError(
      0,
      "network_error",
      `Cannot reach the RecoverAI API at ${API_BASE_URL} - is the backend running?`,
      { url, cause: cause instanceof Error ? cause.message : String(cause) },
    );
  }

  if (!response.ok) {
    // The body may be an ErrorOut, a FastAPI validation error, or an HTML error
    // page from something in front of the API. Read it defensively.
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }

    if (isApiError(body)) {
      throw new ApiRequestError(
        response.status,
        body.error,
        body.message,
        body.detail ?? undefined,
      );
    }

    throw new ApiRequestError(
      response.status,
      "http_error",
      `Request failed (${response.status} ${response.statusText || "error"}) for ${path}`,
      body === null ? undefined : { body },
    );
  }

  // 204, or any success with no body: there is nothing to decode. Callers that
  // reach this branch have declared `T` as void/undefined for that endpoint.
  if (response.status === 204 || response.headers.get("Content-Length") === "0") {
    return undefined as T;
  }

  const decoded: unknown = await response.json();

  // Decision-critical responses are validated against a Zod schema; the rest are
  // cast to their mirrored interface. See src/lib/schemas.ts for why the line is
  // drawn where it is -- in short, a field going missing on the approval screen
  // changes what an operator is allowed to do, whereas a field going missing in
  // a table is a blank cell somebody notices immediately.
  //
  // A drift failure is re-thrown as an ApiRequestError so that every consumer
  // still handles exactly one error type, and status 0 marks it as a
  // client-side fault rather than something the server reported.
  if (schema) {
    try {
      return parseResponse(schema, decoded, path);
    } catch (cause) {
      throw new ApiRequestError(
        0,
        "response_shape_error",
        cause instanceof Error ? cause.message : String(cause),
        { url, path },
      );
    }
  }

  return decoded as T;
}

/** Serialises defined query parameters; omits `undefined` so URLs stay clean. */
function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

/** Path segments come from route params and ids, so they are always escaped. */
const seg = (value: string): string => encodeURIComponent(value);

const json = (body: unknown): string => JSON.stringify(body);

/**
 * The typed surface of the REST API, one method per endpoint in the contract.
 *
 * Methods are grouped by resource and named for the operation an operator would
 * describe, not for the HTTP verb -- `approveCase`, not `postCaseApprove`.
 */
export const api = {
  // --- System ------------------------------------------------------------
  /**
   * Which subsystems are live vs simulated. The console renders this verbatim in
   * the header: a demo that hides the fact it is running on a simulated gateway
   * is a demo that lies.
   */
  getStatus: (): Promise<SystemStatus> => apiFetch<SystemStatus>("/api/status", undefined, systemStatusSchema),

  // --- Metrics -----------------------------------------------------------
  getDashboard: (): Promise<DashboardMetrics> =>
    apiFetch<DashboardMetrics>("/api/metrics/dashboard", undefined, dashboardMetricsSchema),

  getFailureBreakdown: (): Promise<FailureBreakdownItem[]> =>
    apiFetch<FailureBreakdownItem[]>("/api/metrics/failure-breakdown"),

  // --- Payments ----------------------------------------------------------
  listPayments: (params?: {
    status?: PaymentStatusFilter;
    limit?: number;
    offset?: number;
  }): Promise<Payment[]> =>
    apiFetch<Payment[]>(
      `/api/payments${query({
        status: params?.status,
        limit: params?.limit,
        offset: params?.offset,
      })}`,
    ),

  getPayment: (paymentId: string): Promise<Payment> =>
    apiFetch<Payment>(`/api/payments/${seg(paymentId)}`),

  /**
   * Manufactures a realistic failed payment. Every field is optional: an empty
   * POST draws from the seeded scenario catalogue, because a reviewer cannot
   * make a real card decline on demand.
   */
  /**
   * The scenario catalogue the simulate endpoint accepts.
   *
   * Fetched so the picker is always in step with the server. Hard-coding these
   * keys in the component is what previously made six of its eight options 404.
   */
  listFailureScenarios: (): Promise<FailureScenario[]> =>
    apiFetch<FailureScenario[]>("/api/payments/failure-scenarios"),

  simulateFailure: (body: SimulateFailureRequest = {}): Promise<Payment> =>
    apiFetch<Payment>("/api/payments/simulate-failure", {
      method: "POST",
      body: json(body),
    }),

  // --- Customers ---------------------------------------------------------
  getCustomer: (customerId: string): Promise<Customer> =>
    apiFetch<Customer>(`/api/customers/${seg(customerId)}`),

  // --- Recovery ----------------------------------------------------------
  /**
   * Runs the agent over a failed payment and opens a case. Returns the full
   * case, so the caller can navigate straight to the decision screen without a
   * follow-up read.
   */
  analyzePayment: (paymentId: string, body: AnalyzeRequest = {}): Promise<RecoveryCase> =>
    apiFetch<RecoveryCase>(`/api/recovery/payments/${seg(paymentId)}/analyze`, {
      method: "POST",
      body: json(body),
    },
      recoveryCaseSchema),

  listCases: (params?: {
    status?: RecoveryStatus | "all";
    limit?: number;
  }): Promise<RecoveryCaseSummary[]> =>
    apiFetch<RecoveryCaseSummary[]>(
      `/api/recovery/cases${query({ status: params?.status, limit: params?.limit })}`,
    ),

  getCase: (caseId: string): Promise<RecoveryCase> =>
    apiFetch<RecoveryCase>(`/api/recovery/cases/${seg(caseId)}`, undefined, recoveryCaseSchema),

  /** The agent's tool-call trace, for the explainability panel. */
  getCaseTrace: (caseId: string): Promise<AgentToolCall[]> =>
    apiFetch<AgentToolCall[]>(`/api/recovery/cases/${seg(caseId)}/trace`),

  /**
   * Grants approval. Guardrails are re-evaluated server-side at this moment, so
   * a case that looked approvable when the page loaded can still be refused --
   * the response is the authoritative post-decision case either way.
   */
  approveCase: (caseId: string, body: ApproveRequest): Promise<RecoveryCase> =>
    apiFetch<RecoveryCase>(`/api/recovery/cases/${seg(caseId)}/approve`, {
      method: "POST",
      body: json(body),
    },
      recoveryCaseSchema),

  rejectCase: (caseId: string, body: RejectRequest): Promise<RecoveryCase> =>
    apiFetch<RecoveryCase>(`/api/recovery/cases/${seg(caseId)}/reject`, {
      method: "POST",
      body: json(body),
    },
      recoveryCaseSchema),

  // --- Customer-facing checkout ------------------------------------------
  getCheckoutSession: (caseId: string): Promise<CheckoutSession> =>
    apiFetch<CheckoutSession>(`/api/recovery/cases/${seg(caseId)}/checkout`, undefined, checkoutSessionSchema),

  /**
   * Hands Razorpay's success payload back for server-side HMAC verification.
   * The browser's "payment succeeded" callback is a claim; only this call can
   * move a case to `recovered`.
   */
  verifyPayment: (caseId: string, body: VerifyPaymentRequest): Promise<RecoveryCase> =>
    apiFetch<RecoveryCase>(`/api/recovery/cases/${seg(caseId)}/verify`, {
      method: "POST",
      body: json(body),
    },
      recoveryCaseSchema),

  /** Simulated-gateway equivalent of completing (or abandoning) checkout. */
  simulateCheckout: (caseId: string, succeed = true): Promise<RecoveryCase> =>
    apiFetch<RecoveryCase>(
      `/api/recovery/cases/${seg(caseId)}/simulate-checkout${query({ succeed })}`,
      { method: "POST" },
    ),

  /** Forces the live attempt to fail, to exercise the failure path on stage. */
  markAttemptFailed: (
    caseId: string,
    body: MarkAttemptFailedRequest = {},
  ): Promise<RecoveryCase> =>
    apiFetch<RecoveryCase>(`/api/recovery/cases/${seg(caseId)}/mark-failed`, {
      method: "POST",
      body: json(body),
    },
      recoveryCaseSchema),

  // --- Audit -------------------------------------------------------------
  listAuditEvents: (params?: { caseId?: string; limit?: number }): Promise<AuditEvent[]> =>
    apiFetch<AuditEvent[]>(
      `/api/audit${query({ case_id: params?.caseId, limit: params?.limit })}`,
    ),

  /**
   * Recomputes the ledger's hash chain from genesis. A demo that only claims
   * immutability proves nothing; this is the endpoint that lets a reviewer
   * check it.
   */
  verifyAuditChain: (): Promise<AuditChainVerification> =>
    apiFetch<AuditChainVerification>("/api/audit/verify", undefined, auditChainVerificationSchema),

  // --- Policy ------------------------------------------------------------
  /** Read-only: policy is not editable through the API the agent's flow uses. */
  getPolicy: (): Promise<Policy> => apiFetch<Policy>("/api/policy", undefined, policySchema),
};
