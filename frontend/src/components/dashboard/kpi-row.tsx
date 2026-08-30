/**
 * KpiRow - the dashboard's headline numbers.
 *
 * The order is the argument. A merchant reads left to right, and the story this
 * product exists to tell is:
 *
 *     this much came in -> this much was collected -> this much failed ->
 *     this much of the failure is recoverable -> this much came back ->
 *     and here is that as a rate.
 *
 * Volume first, then the loss, then the addressable part of the loss, then the
 * result. Putting "recovery rate" first would lead with a ratio nobody can act
 * on; putting counts first would lead with a number that is not money. Every
 * tile after "Failed" is there to answer the question the previous tile raises.
 *
 * Amounts are rendered compactly (₹4.2L rather than ₹4,20,000.00) because six
 * tiles of full-precision INR do not fit a laptop viewport without wrapping, and
 * a KPI is for orientation - the exact rupee lives in the tables below.
 *
 * No `'use client'`: this is pure presentation with no hooks or handlers.
 */

import {
  BadgeIndianRupee,
  CircleCheck,
  CircleX,
  LifeBuoy,
  Percent,
  Wallet,
} from "lucide-react";

import { StatTile } from "@/components/ui/stat-tile";
import { formatCompactRupees, formatPercent } from "@/lib/format";
import type { DashboardMetrics } from "@/lib/types";

export function KpiRow({ metrics }: { metrics: DashboardMetrics }) {
  const capturedPayments = metrics.total_payments - metrics.failed_payments;

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <StatTile
        label="Total volume"
        value={formatCompactRupees(metrics.total_volume_paise)}
        subLabel={`${metrics.total_payments} payments`}
        icon={<Wallet className="h-4 w-4" />}
      />

      <StatTile
        label="Captured"
        value={formatCompactRupees(metrics.captured_volume_paise)}
        subLabel={`${capturedPayments} collected`}
        tone="success"
        icon={<CircleCheck className="h-4 w-4" />}
      />

      {/* "of all payments", not "of volume": `failure_rate_pct` is
          failed_payments / total_payments, a count ratio. The two diverge sharply
          here because large payments fail more often - they are the ones that hit
          issuer limits and attract risk scrutiny - so labelling a count ratio as a
          volume share understated the failed volume by more than half. */}
      <StatTile
        label="Failed"
        value={formatCompactRupees(metrics.failed_volume_paise)}
        subLabel={`${metrics.failed_payments} payments · ${formatPercent(metrics.failure_rate_pct)} of all payments`}
        tone="danger"
        icon={<CircleX className="h-4 w-4" />}
      />

      {/* Recoverable is tinted as model output, not as a fact: it is the subset
          of failed volume that the classifier and the guardrail engine together
          judged worth attempting. It is a projection, and the colour says so. */}
      <StatTile
        label="Recoverable"
        value={formatCompactRupees(metrics.recoverable_volume_paise)}
        subLabel="passed the guardrails"
        tone="ai"
        icon={<LifeBuoy className="h-4 w-4" />}
      />

      <StatTile
        label="Recovered"
        value={formatCompactRupees(metrics.recovered_volume_paise)}
        subLabel={`${metrics.cases_recovered} cases closed`}
        tone="success"
        icon={<BadgeIndianRupee className="h-4 w-4" />}
      />

      {/*
       * `dashOnZero` is off here. Everywhere else a zero collapses to an em dash
       * because "₹0 recovered" and "nothing has happened yet" are the same
       * message - but a recovery rate of 0% against a non-empty recoverable
       * pool is a real, meaningful result, and hiding it would flatter the
       * product exactly where it should not.
       */}
      <StatTile
        label="Recovery rate"
        value={formatPercent(metrics.recovery_rate_pct)}
        subLabel="of recoverable volume"
        tone={metrics.recovery_rate_pct > 0 ? "success" : "neutral"}
        dashOnZero={false}
        icon={<Percent className="h-4 w-4" />}
      />
    </div>
  );
}
