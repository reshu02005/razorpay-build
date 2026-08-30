'use client';

/**
 * RecoveryQueue - the cases that need a person.
 *
 * An operator's queue should be sorted by what needs them, not by when it
 * happened. So this list shows only the three states where a human is the
 * bottleneck or the audience:
 *
 *   awaiting_approval  the agent proposed a recovery and is waiting on a click
 *   blocked            a guardrail refused; someone should know why
 *   escalated          the agent judged the case needs a human, off-platform
 *
 * Everything else is excluded on purpose. `recovered`, `rejected` and
 * `no_action` are finished, and `executing` / `awaiting_payment` are the system
 * working - surfacing them here would train an operator to scroll past rows
 * that do not need them, which is how a real queue stops being read at all.
 *
 * Within the list, approvals lead and are tinted amber, then blocked and
 * escalated in slate. Amber is the console's "you need to look at this" colour
 * and is deliberately not red: a queue of pending approvals is normal
 * operation, and rendering it as a wall of errors would burn out the one colour
 * that should mean something has gone wrong.
 *
 * Oldest first inside each group. A case that has been waiting two hours is
 * more urgent than one raised a minute ago, and a queue sorted newest-first
 * quietly starves its own tail.
 *
 * Client component: relative timestamps read the wall clock, which differs
 * between the server render and hydration.
 */

import Link from "next/link";
import { ChevronRight, ListChecks } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { formatConfidence, formatRelativeTime, formatRupees } from "@/lib/format";
import {
  FAILURE_CATEGORY_LABEL,
  RECOVERY_STATUS_LABEL,
  RECOVERY_STATUS_TONE,
  RECOVERY_STRATEGY_LABEL,
  type RecoveryCaseSummary,
  type RecoveryStatus,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Rank decides both which statuses appear and their order. `Partial` is what
 * makes membership meaningful: a status with no entry is not in the queue, so
 * adding a state to the enum without deciding whether an operator must act on
 * it leaves it out rather than silently sorting it to the top.
 */
const QUEUE_ORDER: Partial<Record<RecoveryStatus, number>> = {
  awaiting_approval: 0,
  blocked: 1,
  escalated: 2,
};

function queueRank(status: RecoveryStatus): number | undefined {
  return QUEUE_ORDER[status];
}

export function RecoveryQueue({
  cases,
  className,
}: {
  cases: RecoveryCaseSummary[];
  className?: string;
}) {
  const queued = cases
    .filter((item) => queueRank(item.status) !== undefined)
    .sort((a, b) => {
      const byState = (queueRank(a.status) ?? 0) - (queueRank(b.status) ?? 0);
      if (byState !== 0) return byState;
      return new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
    });

  const awaiting = queued.filter((item) => item.status === "awaiting_approval").length;

  return (
    <Card className={className}>
      <CardHeader
        action={
          awaiting > 0 ? (
            <Badge variant="warning" dot>
              {awaiting} awaiting you
            </Badge>
          ) : null
        }
      >
        <CardTitle>Recovery queue</CardTitle>
        <CardDescription>
          Cases where the agent has done everything it is allowed to do on its own.
        </CardDescription>
      </CardHeader>

      <CardContent>
        {queued.length === 0 ? (
          <EmptyState
            size="sm"
            icon={<ListChecks className="h-5 w-5" />}
            title="Nothing is waiting on you"
            description="Cases arrive here when the agent proposes a recovery that needs approval, when a guardrail blocks one, or when it decides a human should take over."
          />
        ) : (
          // Capped height with its own scroll: the queue must never push the
          // failed-payments table below the fold on a busy day.
          <ul className="max-h-96 space-y-2 overflow-y-auto">
            {queued.map((item) => {
              const needsApproval = item.status === "awaiting_approval";

              return (
                <li key={item.id}>
                  <Link
                    href={`/recovery/${item.id}`}
                    className={cn(
                      "flex items-center gap-3 rounded-lg border p-3 transition-colors",
                      needsApproval
                        ? "border-warning/30 bg-warning-subtle hover:border-warning/50"
                        : "border-border hover:bg-muted",
                    )}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">
                          {item.customer_name || item.customer_id}
                        </span>
                        <Badge variant={RECOVERY_STATUS_TONE[item.status]}>
                          {RECOVERY_STATUS_LABEL[item.status]}
                        </Badge>
                      </div>
                      <p className="mt-0.5 truncate text-xs text-muted-foreground">
                        {FAILURE_CATEGORY_LABEL[item.failure_category]} ·{" "}
                        {RECOVERY_STRATEGY_LABEL[item.strategy]} ·{" "}
                        {formatConfidence(item.propensity_score)} likely to succeed
                      </p>
                    </div>

                    <div className="shrink-0 text-right">
                      <p className="font-mono text-sm font-medium tabular-nums">
                        {formatRupees(item.amount_paise)}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {formatRelativeTime(item.created_at)}
                      </p>
                    </div>

                    <ChevronRight
                      className="h-4 w-4 shrink-0 text-muted-foreground"
                      aria-hidden="true"
                    />
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
