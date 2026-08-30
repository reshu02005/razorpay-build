/**
 * FailureBreakdown - failed volume and count, split by why the payment failed.
 *
 * This panel is the visual form of the product's central claim: the correct
 * recovery action differs per failure reason, so the first thing a merchant
 * should see about their failures is which *kind* they are. A single "₹4.2L
 * failed" number hides the fact that half of it is a payday problem and a
 * tenth of it is fraud that must never be retried.
 *
 * The bars are plain divs with a percentage width, and that is a decision, not
 * a shortcut. There are at most eleven categories and usually four, all sharing
 * one axis; a charting library would add a runtime dependency, a bundle, an SSR
 * story and a theming surface to draw eleven rectangles. Tailwind widths render
 * identically on the server and the client, inherit the theme tokens for free,
 * and cost nothing offline.
 *
 * The bar is scaled against the largest category rather than against the total,
 * because the question being asked here is "which of these dominates", and
 * against-the-total scaling leaves every bar a stub as soon as one category runs
 * away with the volume.
 *
 * No `'use client'`: pure presentation.
 */

import { ChartNoAxesColumn } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { formatRupees } from "@/lib/format";
import { FAILURE_CATEGORY_LABEL, type FailureBreakdownItem, type FailureCategory } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Mirrors `FailureCategory.is_recoverable` on the server.
 *
 * These two are the hard "never automate" cases: `risk_blocked` means a risk
 * engine flagged the transaction, and `unknown` means the classifier could not
 * reason about it at all. They are drawn in danger tone so that a category no
 * automation will ever touch is visually distinct from one that is simply
 * waiting for a decision.
 *
 * Kept as a two-element list rather than derived from the API because it is a
 * display hint, not policy: `/policy` renders the authoritative
 * `non_recoverable_categories` straight from the server.
 */
const NEVER_RECOVERABLE: readonly FailureCategory[] = ["risk_blocked", "unknown"];

export function FailureBreakdown({
  items,
  className,
}: {
  items: FailureBreakdownItem[];
  className?: string;
}) {
  const ordered = [...items].sort((a, b) => b.volume_paise - a.volume_paise);
  // Guarded against zero so a breakdown of entirely ₹0 rows cannot divide by it.
  const peak = ordered.reduce((max, item) => Math.max(max, item.volume_paise), 0) || 1;
  const totalCount = ordered.reduce((sum, item) => sum + item.count, 0);

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle>Why payments failed</CardTitle>
        <CardDescription>
          {/* "Analysed" is doing real work in this sentence. A failure has no
              category until the agent has classified it, so this panel counts
              cases and not raw failures - which means its total is lower than
              the "Failed" tile above until every failure has been analysed.
              Saying so is the difference between a reviewer reading two
              consistent numbers and a reviewer catching us out on an
              inconsistency that was never there. */}
          {totalCount > 0
            ? `${totalCount} analysed ${totalCount === 1 ? "failure" : "failures"} by category. Each category implies a different recovery action. Failures nobody has analysed yet are not counted here.`
            : "Nothing analysed yet. Run the agent on a failed payment and its category will appear here."}
        </CardDescription>
      </CardHeader>

      <CardContent>
        {ordered.length === 0 ? (
          <EmptyState
            size="sm"
            icon={<ChartNoAxesColumn className="h-5 w-5" />}
            title="No failures recorded yet"
            description="Once a payment fails, its category appears here with the share of lost volume it accounts for."
          />
        ) : (
          <ul className="space-y-4">
            {ordered.map((item) => {
              const share = (item.volume_paise / peak) * 100;
              const blocked = NEVER_RECOVERABLE.includes(item.category);

              return (
                <li key={item.category}>
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="truncate text-sm font-medium">
                      {FAILURE_CATEGORY_LABEL[item.category]}
                    </span>
                    <span className="shrink-0 font-mono text-sm tabular-nums">
                      {formatRupees(item.volume_paise)}
                    </span>
                  </div>

                  <div
                    className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-muted"
                    role="presentation"
                  >
                    <div
                      className={cn("h-full rounded-full", blocked ? "bg-danger/70" : "bg-ai")}
                      // Inline width because the value is data. A Tailwind class
                      // cannot express an arbitrary computed percentage without
                      // the JIT seeing the literal string at build time.
                      style={{ width: `${share}%` }}
                    />
                  </div>

                  <div className="mt-1 flex items-center justify-between gap-3 text-xs text-muted-foreground">
                    <span>
                      {item.count} {item.count === 1 ? "payment" : "payments"}
                    </span>
                    {blocked ? (
                      <span className="text-danger-strong">Never auto-recovered</span>
                    ) : item.recovered_count > 0 ? (
                      <span className="text-success-strong">
                        {item.recovered_count} recovered
                      </span>
                    ) : (
                      <span>none recovered yet</span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
