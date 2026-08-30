'use client';

/**
 * ModeBanner - says out loud which subsystems are real and which are simulated.
 *
 * This is product design, not a debug aid, and it is the reason the rest of the
 * numbers on this screen can be believed. The app is built to run with zero
 * credentials: with no Razorpay keys an in-process gateway mints orders, and
 * with no Gemini key a deterministic planner produces the recovery plan. Both
 * are genuinely useful - the simulated gateway signs with a real HMAC, so the
 * production signature-verification path still runs - but a demo that quietly
 * pretends to charge a card is misleading. Stating the mode in the first thing a
 * reviewer reads is what makes "₹42,000 recovered" a claim they can trust rather
 * than one they have to take on faith.
 *
 * The banner is symmetrical: when everything *is* live, it says that too. A
 * warning that only ever appears in the degraded case teaches a reader nothing
 * about the case where it is absent.
 */

import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { AGENT_MODE_LABEL, GATEWAY_MODE_LABEL } from "@/lib/types";
import { cn } from "@/lib/utils";

export function ModeBanner({ className }: { className?: string }) {
  const { data: status, loading } = useApi(() => api.getStatus(), []);

  if (status === null) {
    // Still in flight: a placeholder of the banner's own height, so the KPI row
    // below does not jump down once the status lands.
    if (loading) return <Skeleton className={cn("h-[4.5rem] w-full", className)} />;

    /*
     * The request failed. Deliberately silent: /api/status failing means the
     * backend is unreachable, and the dashboard already hands that to the route
     * error boundary with the URL and the command to start it. Two banners
     * describing one outage is noise at exactly the moment a reviewer needs a
     * single clear instruction.
     */
    return null;
  }

  const simulatedGateway = status.gateway_mode === "simulated";
  const ruleBasedAgent = status.agent_mode === "rule_based";
  const heuristicModel = !status.ml_model_loaded;
  const degraded = simulatedGateway || ruleBasedAgent || heuristicModel;

  if (!degraded) {
    return (
      <Alert variant="neutral" className={className}>
        <AlertTitle>Live services</AlertTitle>
        <AlertDescription>
          {GATEWAY_MODE_LABEL[status.gateway_mode]} for execution,{" "}
          {status.gemini_model ?? AGENT_MODE_LABEL[status.agent_mode]} for reasoning
          {status.ml_model_version ? `, propensity model ${status.ml_model_version}` : ""}. No money
          moves without an explicit approval.
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <Alert variant="warning" className={className}>
      <AlertTitle>Running with reduced credentials</AlertTitle>
      <AlertDescription>
        <ul className="space-y-1">
          {simulatedGateway ? (
            <li>
              <strong className="font-semibold">Simulated gateway</strong> - no Razorpay credentials
              configured. Payments are not real.
            </li>
          ) : null}
          {ruleBasedAgent ? (
            <li>
              <strong className="font-semibold">Rule-based planner</strong> - no Gemini key
              configured. Plans come from the deterministic playbook, scored by the same model and
              held to the same guardrails.
            </li>
          ) : null}
          {heuristicModel ? (
            <li>
              <strong className="font-semibold">Heuristic propensity</strong> - the trained model
              artefact was not found, so scores come from the documented fallback.
            </li>
          ) : null}
          {/* Server-authored warnings are rendered verbatim. The backend owns the
              wording; re-phrasing it here would let the UI drift from what the
              API actually reported. */}
          {status.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  );
}
