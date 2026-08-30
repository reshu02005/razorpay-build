'use client';

/**
 * The merchant dashboard - the first screen a reviewer sees, and the only one
 * that has to answer "what is this product for?" without being asked.
 *
 * It reads top to bottom as one argument:
 *
 *   1. What mode am I running in       honest banner, before any number
 *   2. Where did the money go          KPI row, left to right
 *   3. What is the automation allowed  daily recovery budget gauge
 *   4. Why did payments fail           category breakdown
 *   5. What needs a human right now    recovery queue
 *   6. What has not been looked at     failed payments, each with one action
 *
 * ---------------------------------------------------------------------------
 * Loading strategy (stated once here rather than at every call site)
 * ---------------------------------------------------------------------------
 * All four resources are fetched together through a single `useApi` call, so
 * the screen has one loading state and one refresh. Fetching them separately
 * would give four independent spinners that resolve in an arbitrary order and
 * make the page visibly reflow twice on every load.
 *
 * While the first response is outstanding the page renders skeletons shaped
 * like the content that is coming, not a spinner: a full-page spinner throws
 * away the layout and makes a fast load flash, whereas a skeleton keeps the
 * page height stable so nothing jumps when the data lands.
 *
 * Nothing here polls. This is a console someone reads while deciding whether to
 * approve a charge, and a background refetch that reorders the queue under the
 * cursor mid-decision is worse than slightly stale numbers. The data is
 * re-read on the two occasions it actually changed: after simulating a failure,
 * and when the operator asks. (The customer checkout screen does poll - there
 * the state genuinely changes off-screen.)
 *
 * ---------------------------------------------------------------------------
 * Failure handling
 * ---------------------------------------------------------------------------
 * A first load that fails is re-thrown into the route error boundary rather
 * than handled here. `app/error.tsx` is the one place that explains what to
 * check and how to start the backend; duplicating that guidance in a second
 * component would give the project two copies of the same instructions to keep
 * in step. A *refresh* that fails is different - the screen still holds usable
 * data - so that surfaces as a banner above content that stays put.
 */

import { useCallback } from "react";
import { RefreshCw } from "lucide-react";

import { FailedPaymentsTable } from "@/components/dashboard/failed-payments-table";
import { FailureBreakdown } from "@/components/dashboard/failure-breakdown";
import { KpiRow } from "@/components/dashboard/kpi-row";
import { RecoveryQueue } from "@/components/dashboard/recovery-queue";
import { SimulateFailureButton } from "@/components/dashboard/simulate-failure-button";
import { ModeBanner } from "@/components/layout/mode-banner";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { formatPercent, formatRupees } from "@/lib/format";
import type {
  DashboardMetrics,
  FailureBreakdownItem,
  Payment,
  RecoveryCaseSummary,
} from "@/lib/types";

interface DashboardSnapshot {
  metrics: DashboardMetrics;
  breakdown: FailureBreakdownItem[];
  failedPayments: Payment[];
  cases: RecoveryCaseSummary[];
}

/**
 * One round trip's worth of dashboard state.
 *
 * `Promise.all` rather than sequential awaits: the four endpoints are
 * independent reads against a local process, and serialising them would make
 * the slowest path four times the latency of the slowest call for no benefit.
 */
async function loadDashboard(): Promise<DashboardSnapshot> {
  const [metrics, breakdown, failedPayments, cases] = await Promise.all([
    api.getDashboard(),
    api.getFailureBreakdown(),
    api.listPayments({ status: "failed", limit: 50 }),
    api.listCases(),
  ]);

  return { metrics, breakdown, failedPayments, cases };
}

export default function DashboardPage() {
  const { data, error, loading, refresh } = useApi(loadDashboard, []);

  const handleSimulated = useCallback(() => {
    refresh();
  }, [refresh]);

  // Nothing on screen and the API is unreachable: hand it to the error
  // boundary, which is where the diagnostics live.
  if (data === null && error !== null) {
    throw new Error(error);
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">Revenue recovery</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Failed payments, why they failed, and what the agent proposes doing about them.
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <Button
            variant="outline"
            onClick={refresh}
            loading={loading && data !== null}
            loadingText="Refreshing…"
            leadingIcon={<RefreshCw className="h-4 w-4" />}
          >
            Refresh
          </Button>
          <SimulateFailureButton onSimulated={handleSimulated} />
        </div>
      </header>

      <ModeBanner />

      {/* A refresh failed but the screen still holds the previous response.
          Shown above the content it belongs to, and phrased as "these numbers
          are from the last successful read" rather than as a fatal error. */}
      {error !== null && data !== null ? (
        <Alert variant="warning">
          <AlertTitle>Could not refresh</AlertTitle>
          <AlertDescription>
            {error} The figures below are from the last successful read.
          </AlertDescription>
        </Alert>
      ) : null}

      {data === null ? (
        <DashboardSkeleton />
      ) : (
        <>
          <KpiRow metrics={data.metrics} />

          <BudgetGauge metrics={data.metrics} />

          {/*
           * `min-w-0` on both panels is load-bearing, not decoration. A grid
           * item's automatic minimum size is its min-content width, and both of
           * these contain tokens that refuse to wrap - a status badge, a
           * monospace rupee amount. Without it the track is sized to that
           * min-content and the whole page scrolls sideways on a phone, which
           * is the one layout bug that makes a console unusable on the device
           * an on-call operator actually has to hand.
           */}
          <div className="grid gap-4 lg:grid-cols-2">
            <FailureBreakdown items={data.breakdown} className="min-w-0" />
            <RecoveryQueue cases={data.cases} className="min-w-0" />
          </div>

          <FailedPaymentsTable payments={data.failedPayments} />
        </>
      )}
    </div>
  );
}

/**
 * The daily recovery budget, spent against its cap.
 *
 * This is the blast-radius limit made visible. Every guardrail in the system is
 * enforced server-side and most of them are invisible until one fires, but this
 * one is a running total, and showing it is what turns "the automation is
 * bounded" from a claim in a README into something an operator can watch. If
 * the agent, the data or an operator goes wrong, the worst case for today is
 * the number on the right of this bar.
 *
 * The tone escalates with utilisation rather than sitting on one colour: amber
 * once most of the day's envelope is committed, rose when it is nearly gone,
 * because "you are about to stop being able to recover anything today" is
 * something to learn before the first denial, not from it.
 */
function BudgetGauge({ metrics }: { metrics: DashboardMetrics }) {
  const { daily_budget_used_paise: used, daily_budget_limit_paise: limit } = metrics;
  // The limit is a configured policy value and is never zero in practice, but a
  // misconfiguration must not divide by zero and paint a NaN-wide bar.
  const utilisation = limit > 0 ? (used / limit) * 100 : 0;
  const remaining = Math.max(limit - used, 0);

  const tone = utilisation >= 90 ? "danger" : utilisation >= 70 ? "warning" : "neutral";

  return (
    <Card>
      <CardHeader
        action={
          <span className="font-mono text-sm tabular-nums">
            {formatRupees(used)}{" "}
            <span className="text-muted-foreground">/ {formatRupees(limit)}</span>
          </span>
        }
      >
        <CardTitle>Daily recovery budget</CardTitle>
        <CardDescription>
          The total value of recovery orders this system may create today. It is a cap on the
          damage any single bad day can do, and it is enforced server-side.
        </CardDescription>
      </CardHeader>

      <CardContent>
        <Progress
          value={used}
          max={limit > 0 ? limit : 100}
          tone={tone}
          label="Daily recovery budget used"
          valueText={`${formatPercent(utilisation)} of today's budget used`}
        />
        <p className="mt-2 text-xs text-muted-foreground">
          {formatPercent(utilisation)} committed · {formatRupees(remaining)} left before further
          recoveries are denied for the day.
        </p>
      </CardContent>
    </Card>
  );
}

/**
 * First-load placeholder, shaped like the real page.
 *
 * The tile count, the two-column split and the table height all match what
 * replaces them, so the layout does not move when the data arrives. A skeleton
 * that is the wrong shape is just a slower spinner.
 */
function DashboardSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading dashboard</span>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {Array.from({ length: 6 }, (_unused, index) => (
          <Skeleton key={index} className="h-[6.5rem] w-full" />
        ))}
      </div>

      <Skeleton className="h-[9rem] w-full" />

      <div className="grid gap-4 lg:grid-cols-2">
        <Skeleton className="h-80 w-full" />
        <Skeleton className="h-80 w-full" />
      </div>

      <Skeleton className="h-96 w-full" />
    </div>
  );
}
