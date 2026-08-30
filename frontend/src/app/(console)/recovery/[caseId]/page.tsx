'use client';

/**
 * /recovery/[caseId] - the recovery decision console.
 *
 * This screen is the product. Everything else in the console exists to get an
 * operator here, and the layout is arranged to make one argument in one glance:
 *
 *     the AI recommended  ->  the rules decided  ->  a human approved
 *                          -> and every step is on the record
 *
 * The left column is that chain, top to bottom: the recorded failure, then the
 * agent's classification and recommendation, then the model's estimate of
 * success, then all thirteen guardrails with their verdicts. Read downwards it
 * answers "why is this being proposed?".
 *
 * The right column is the record, and it is sticky. Approval and the audit
 * ledger stay in view while the operator scrolls the reasoning, because the
 * decision and the evidence for it should never be on separate screens - and
 * because the ledger updating underneath the button is the visible proof that
 * the decision was written down.
 *
 * The full-width panel underneath holds the two things a reviewer digs into
 * rather than scans: the raw tool-call trace, and the policy snapshot that was
 * frozen onto this case when it was proposed.
 */

import { use, useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { ArrowLeft, ListChecks, RefreshCw, ShieldCheck, Sparkles, Wrench } from 'lucide-react';

import { AgentTrace } from '@/components/recovery/agent-trace';
import { AiVerdictCard } from '@/components/recovery/ai-verdict-card';
import { ApprovalPanel } from '@/components/recovery/approval-panel';
import { AuditTimeline } from '@/components/recovery/audit-timeline';
import { CaseStatusPill } from '@/components/recovery/case-status-pill';
import { FailureDetail } from '@/components/recovery/failure-detail';
import { GuardrailChecklist } from '@/components/recovery/guardrail-checklist';
import { PropensityGauge, topFactorsFromTrace } from '@/components/recovery/propensity-gauge';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useApi, usePolling } from '@/hooks/useApi';
import { api } from '@/lib/api';
import { formatDateTime, formatRupees } from '@/lib/format';
import { AGENT_MODE_LABEL, type RecoveryStatus } from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * States whose next change happens somewhere other than this tab.
 *
 * Once an order is live the customer completes (or abandons) the payment in the
 * Razorpay window or on the `/checkout` page, and the case moves to `recovered`
 * on the server with nothing happening here to trigger a re-render. There is no
 * websocket in this stack, so polling is the honest way to see it. Every other
 * state changes only because somebody acted on this screen, and polling those
 * would be a request every few seconds to be told nothing has happened.
 */
const LIVE_STATUSES: readonly RecoveryStatus[] = ['approved', 'executing', 'awaiting_payment'];
const POLL_INTERVAL_MS = 5000;

/** The audit ledger is capped rather than paged: one case's chain is short. */
const AUDIT_LIMIT = 200;

function LoadingState() {
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="space-y-6">
        <Skeleton className="h-72 w-full" />
        <Skeleton className="h-64 w-full" />
        <Skeleton className="h-56 w-full" />
      </div>
      <div className="space-y-6">
        <Skeleton className="h-80 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    </div>
  );
}

export default function RecoveryCasePage({
  params,
}: {
  params: Promise<{ caseId: string }>;
}) {
  /*
   * `params` is a Promise in the Next 15 App Router, and `React.use()` is what
   * unwraps it. This is a genuine breaking change from Next 14, where `params`
   * was a plain object - code copied from a Next 14 tutorial destructures it
   * directly, compiles, and then renders `undefined` into the URL at runtime
   * with no error to explain why. Unwrapping it here, once, is the fix.
   */
  const { caseId } = use(params);

  /*
   * The poll interval is derived from the last status we saw rather than read
   * out of the query below, because the query has to be declared with an
   * interval already in hand. The extra state is one render behind the data,
   * which is harmless: the worst case is one additional poll after a case goes
   * terminal, or one skipped poll immediately after it goes live.
   */
  const [liveStatus, setLiveStatus] = useState<RecoveryStatus | null>(null);
  const pollMs = liveStatus !== null && LIVE_STATUSES.includes(liveStatus) ? POLL_INTERVAL_MS : 0;

  const caseQuery = usePolling(() => api.getCase(caseId), [caseId], pollMs);
  const auditQuery = usePolling(
    () => api.listAuditEvents({ caseId, limit: AUDIT_LIMIT }),
    [caseId],
    pollMs,
  );
  // The trace is written once, when the agent runs, and never changes again -
  // so it is fetched once and never polled.
  const traceQuery = useApi(() => api.getCaseTrace(caseId), [caseId]);

  const recoveryCase = caseQuery.data;

  useEffect(() => {
    if (recoveryCase === null) return;
    setLiveStatus(recoveryCase.status);
  }, [recoveryCase]);

  const { refresh: refreshCase } = caseQuery;
  const { refresh: refreshAudit } = auditQuery;

  /*
   * After a decision, both the case and the ledger are refetched rather than
   * patched from the mutation's response. The approve endpoint returns the new
   * case, but it does not return the new ledger entries, and a screen showing a
   * fresh status above a stale audit trail is exactly the inconsistency this
   * page exists to disprove.
   */
  const handleDecision = useCallback(() => {
    refreshCase();
    refreshAudit();
  }, [refreshCase, refreshAudit]);

  const trace = traceQuery.data ?? [];
  const auditEvents = auditQuery.data ?? [];

  if (recoveryCase === null) {
    return (
      <div className="space-y-6">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-sm text-slate-500 transition-colors hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          Dashboard
        </Link>

        {caseQuery.loading ? (
          <LoadingState />
        ) : (
          <Alert variant="danger">
            <AlertTitle>This recovery case could not be loaded</AlertTitle>
            <AlertDescription>
              {caseQuery.error ?? `No case was returned for id ${caseId}.`}
            </AlertDescription>
          </Alert>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <header className="space-y-4">
        <Link
          href={`/payments/${recoveryCase.original_payment_id}`}
          className="inline-flex items-center gap-1.5 text-sm text-slate-500 transition-colors hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          Original payment
        </Link>

        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="font-display text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
              Recovery decision
            </h1>
            <p className="mt-1 font-mono text-xs text-slate-500 dark:text-slate-400">
              {recoveryCase.id}
            </p>
          </div>

          <div className="flex items-center gap-2">
            <CaseStatusPill status={recoveryCase.status} size="lg" />
            {/* The icon spins rather than being swapped for the Button's own
                spinner: at `size="icon"` there is only room for one glyph. */}
            <Button
              variant="ghost"
              size="icon"
              aria-label="Refresh this case"
              disabled={caseQuery.loading}
              onClick={handleDecision}
            >
              <RefreshCw
                className={cn('h-4 w-4', caseQuery.loading && 'animate-spin')}
                aria-hidden="true"
              />
            </Button>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-slate-500 dark:text-slate-400">
          <span className="font-medium text-slate-900 dark:text-slate-100">
            {formatRupees(recoveryCase.amount_paise)}
          </span>
          <span className="truncate">
            {recoveryCase.customer_name || recoveryCase.customer_id}
          </span>
          <Badge variant={recoveryCase.agent_mode === 'llm' ? 'ai' : 'neutral'}>
            {AGENT_MODE_LABEL[recoveryCase.agent_mode]}
          </Badge>
          <span>
            Attempt{recoveryCase.attempt_count === 1 ? '' : 's'}: {recoveryCase.attempt_count}
          </span>
          <span>Opened {formatDateTime(recoveryCase.created_at)}</span>
          <span>Updated {formatDateTime(recoveryCase.updated_at)}</span>
          {pollMs > 0 ? (
            // Said out loud, because a number that changes on its own without
            // explanation reads as a glitch rather than as live data.
            <span className="inline-flex items-center gap-1.5 text-slate-400 dark:text-slate-500">
              <RefreshCw className="h-3 w-3 animate-spin" aria-hidden="true" />
              live - checking for the customer&apos;s payment
            </span>
          ) : null}
        </div>
      </header>

      {/* A refresh that fails leaves the last good case on screen and reports
          the failure above it, rather than blanking a page mid-review. */}
      {caseQuery.error === null ? null : (
        <Alert variant="warning">
          <AlertTitle>The last refresh failed</AlertTitle>
          <AlertDescription>
            {caseQuery.error} Everything below is the last successful read.
          </AlertDescription>
        </Alert>
      )}

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        {/* Left: the decision, read top to bottom. */}
        <div className="space-y-6">
          <FailureDetail
            payment={recoveryCase.original_payment}
            customer={recoveryCase.customer}
          />

          <AiVerdictCard
            failureCategory={recoveryCase.failure_category}
            confidence={recoveryCase.classification_confidence}
            strategy={recoveryCase.strategy}
            rationale={recoveryCase.agent_rationale}
            customerMessage={recoveryCase.customer_message}
            agentMode={recoveryCase.agent_mode}
          />

          <PropensityGauge
            score={recoveryCase.propensity_score}
            modelVersion={recoveryCase.propensity_model_version}
            isFallback={recoveryCase.propensity_is_fallback}
            topFactors={topFactorsFromTrace(trace)}
          />

          <GuardrailChecklist
            evaluations={recoveryCase.guardrail_evaluations}
            decision={recoveryCase.guardrail_decision}
          />
        </div>

        {/* Right: the record. Sticky so the decision and its ledger stay put
            while the reasoning on the left is scrolled. */}
        <div className="space-y-6 lg:sticky lg:top-20">
          <ApprovalPanel recoveryCase={recoveryCase} onDecision={handleDecision} />
          <AuditTimeline events={auditEvents} />
        </div>
      </div>

      <Tabs defaultValue="trace">
        <TabsList className="border-b border-slate-200 dark:border-slate-800">
          <TabsTrigger value="trace">
            <Wrench className="h-4 w-4" aria-hidden="true" />
            Agent trace
            <span className="font-mono text-2xs text-slate-400 dark:text-slate-500">
              {trace.length}
            </span>
          </TabsTrigger>
          <TabsTrigger value="policy">
            <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            Policy snapshot
          </TabsTrigger>
        </TabsList>

        <TabsContent value="trace" className="pt-4">
          {traceQuery.loading && traceQuery.data === null ? (
            <Skeleton className="h-48 w-full" />
          ) : (
            <AgentTrace steps={trace} />
          )}
        </TabsContent>

        <TabsContent value="policy" className="pt-4">
          <PolicySnapshotPanel snapshot={recoveryCase.policy_snapshot} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

/**
 * The guardrail limits exactly as they stood when this case was proposed.
 *
 * Rendered raw, as JSON, on purpose. Guardrail limits are configuration: a
 * merchant will raise the daily budget next month and lower the propensity
 * floor the month after. Without a snapshot, every historical decision would
 * silently be re-read against today's numbers, and "why was this allowed?"
 * would be answerable but wrong. Formatting the snapshot into a friendly table
 * would invite exactly that re-interpretation, because a table implies a schema
 * that is stable and these keys are not - they are whatever the policy engine
 * froze on the day. The raw object is the honest presentation.
 *
 * Amounts here are integer paise, the unit of record everywhere upstream of the
 * API edge. They are deliberately not converted: this is the stored value, not
 * a display value.
 */
function PolicySnapshotPanel({ snapshot }: { snapshot: Record<string, unknown> }) {
  const isEmpty = Object.keys(snapshot).length === 0;

  return (
    <Card>
      <CardHeader
        action={
          <Badge variant="neutral" icon={<ListChecks className="h-3.5 w-3.5" aria-hidden="true" />}>
            Frozen at proposal
          </Badge>
        }
      >
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-slate-500 dark:text-slate-400" aria-hidden="true" />
          Policy snapshot
        </CardTitle>
        <CardDescription>
          The limits that were in force when this case was created. Money values are integer
          paise, as stored.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isEmpty ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">
            No policy snapshot was stored on this case.
          </p>
        ) : (
          <pre className="overflow-x-auto rounded-lg border border-slate-200 bg-slate-50 p-4 font-mono text-2xs leading-relaxed text-slate-800 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200">
            {JSON.stringify(snapshot, null, 2)}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}
