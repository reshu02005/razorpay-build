/**
 * StatTile - one KPI in the dashboard's headline row.
 *
 * Money contract: this component never formats currency. Amounts arrive from
 * the API as both paise (int) and rupees (float), and the single place allowed
 * to turn either into a display string is the shared formatter in
 * `@/lib/format`. StatTile therefore takes an already-formatted `value`. If it
 * did its own division the codebase would have two rounding rules for the same
 * number, and the one on screen would eventually disagree with the one in the
 * audit ledger.
 *
 * Empty-value contract: a KPI must never render "NaN", "undefined", "₹NaN" or
 * an empty box. A dashboard that shows NaN in the recovered-volume tile is
 * worse than one that shows nothing, because the operator cannot tell a bug
 * from a real number. Anything unrenderable collapses to an em dash. By default
 * a genuine zero collapses too - on this product "₹0 recovered" and "nothing
 * has happened yet" are the same message, and a row of zeroes reads as broken.
 * Callers that need a hard zero on screen (a counter that is meaningfully zero,
 * such as "0 breaches") pass `dashOnZero={false}`.
 */

import * as React from 'react';
import { Minus, TrendingDown, TrendingUp, type LucideIcon } from 'lucide-react';

import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

export type StatTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info' | 'ai';
export type TrendDirection = 'up' | 'down' | 'flat';

/** Colour is applied to the icon chip only - the number itself stays slate so a
 *  row of tiles reads as one block of data rather than a traffic light. */
const TONE_CHIP: Record<StatTone, string> = {
  neutral: 'bg-slate-100 text-slate-500 dark:bg-slate-900 dark:text-slate-400',
  success: 'bg-emerald-50 text-emerald-600 dark:bg-emerald-950 dark:text-emerald-400',
  warning: 'bg-amber-50 text-amber-600 dark:bg-amber-950 dark:text-amber-400',
  danger: 'bg-rose-50 text-rose-600 dark:bg-rose-950 dark:text-rose-400',
  info: 'bg-blue-50 text-blue-600 dark:bg-blue-950 dark:text-blue-400',
  ai: 'bg-sky-50 text-sky-600 dark:bg-sky-950 dark:text-sky-400',
};

const EM_DASH = '—';

/**
 * Decides whether a value is unrenderable (or a zero we would rather show as a
 * dash). Strings are inspected because the caller hands us a *formatted* value:
 * by the time it gets here "₹0.00" is a string, not the number 0.
 */
function isBlankValue(value: React.ReactNode, dashOnZero: boolean): boolean {
  if (value === undefined || value === null || value === false) return true;

  if (typeof value === 'number') {
    // NaN and Infinity are always a bug upstream; they must not reach the screen.
    if (!Number.isFinite(value)) return true;
    return dashOnZero && value === 0;
  }

  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (trimmed === '' || trimmed.toLowerCase() === 'nan') return true;
    if (!dashOnZero) return false;
    // Strip currency symbols, separators and units, then ask whether what is
    // left is numerically zero: "₹0.00", "0", "0.0%" all collapse, while
    // "₹1,200.00" and "12 of 30" do not.
    const numeric = Number(trimmed.replace(/[^0-9.-]/g, ''));
    return Number.isFinite(numeric) && numeric === 0;
  }

  // A ReactNode we cannot inspect (an element, a fragment): trust the caller.
  return false;
}

function resolveDirection(trendPct: number): TrendDirection {
  if (trendPct > 0) return 'up';
  if (trendPct < 0) return 'down';
  return 'flat';
}

const TREND_ICONS: Record<TrendDirection, LucideIcon> = {
  up: TrendingUp,
  down: TrendingDown,
  flat: Minus,
};

export interface StatTileProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Short, lowercase-ish caption: "Recovered volume", "Recovery rate". */
  label: string;
  /**
   * The already-formatted headline value. Money must come through the shared
   * formatter; this component will not divide by 100.
   */
  value: React.ReactNode;
  /** Secondary line under the value: "of ₹4.2L failed", "last 24h". */
  subLabel?: string;
  /** Signed percentage change against the previous period. */
  trendPct?: number;
  /** Overrides the arrow direction derived from the sign of `trendPct`. */
  trendDirection?: TrendDirection;
  /** What the trend is measured against: "vs last week". */
  trendLabel?: string;
  /**
   * Set for metrics where a rise is bad (failure rate, denied cases). Flips the
   * trend colouring so "up" reads rose and "down" reads emerald.
   */
  invertTrend?: boolean;
  /** Decorative icon, usually a lucide glyph at `h-4 w-4`. */
  icon?: React.ReactNode;
  tone?: StatTone;
  /** Renders placeholders instead of the value while the metric is in flight. */
  loading?: boolean;
  /** See the empty-value contract above. */
  dashOnZero?: boolean;
}

const StatTile = React.forwardRef<HTMLDivElement, StatTileProps>(function StatTile(
  {
    className,
    label,
    value,
    subLabel,
    trendPct,
    trendDirection,
    trendLabel,
    invertTrend = false,
    icon,
    tone = 'neutral',
    loading = false,
    dashOnZero = true,
    ...props
  },
  ref,
) {
  const blank = isBlankValue(value, dashOnZero);

  // Both the arrow and the "+12.4%" string are derived inside a single inline
  // guard so a NaN or an omitted trend simply produces no trend row at all,
  // rather than an arrow pointing at nothing.
  const trendText =
    trendPct !== undefined && Number.isFinite(trendPct)
      ? `${trendPct > 0 ? '+' : ''}${trendPct.toFixed(1)}%`
      : undefined;

  const direction: TrendDirection | undefined =
    trendDirection ??
    (trendPct !== undefined && Number.isFinite(trendPct) ? resolveDirection(trendPct) : undefined);

  const TrendIcon: LucideIcon | undefined = direction ? TREND_ICONS[direction] : undefined;

  // "Good" is direction-dependent, not colour-dependent: a falling failure rate
  // is the win. `invertTrend` is what encodes that per metric.
  const trendClass =
    direction === 'flat' || direction === undefined
      ? 'text-slate-500 dark:text-slate-400'
      : (direction === 'up') !== invertTrend
        ? 'text-emerald-600 dark:text-emerald-400'
        : 'text-rose-600 dark:text-rose-400';

  return (
    <div
      ref={ref}
      className={cn(
        'rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-950',
        className,
      )}
      aria-busy={loading || undefined}
      {...props}
    >
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs font-medium text-slate-500 dark:text-slate-400">{label}</p>
        {icon ? (
          <span
            aria-hidden="true"
            className={cn('flex h-7 w-7 shrink-0 items-center justify-center rounded-md', TONE_CHIP[tone])}
          >
            {icon}
          </span>
        ) : null}
      </div>

      {loading ? (
        <div className="mt-3 space-y-2">
          <Skeleton className="h-7 w-24" />
          <Skeleton className="h-3 w-16" />
        </div>
      ) : (
        <>
          <p
            className={cn(
              'mt-2 font-mono text-2xl font-semibold tabular-nums tracking-tight',
              // A dash is an absence, not a value - it is muted so it never
              // competes with a real number in the tile next to it.
              blank ? 'text-slate-300 dark:text-slate-700' : 'text-slate-900 dark:text-slate-50',
            )}
          >
            {blank ? EM_DASH : value}
          </p>

          {subLabel || (trendText && TrendIcon) ? (
            <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
              {trendText && TrendIcon ? (
                <span className={cn('inline-flex items-center gap-1 font-medium', trendClass)}>
                  <TrendIcon className="h-3 w-3" aria-hidden="true" />
                  {trendText}
                </span>
              ) : null}
              {trendLabel ? (
                <span className="text-slate-400 dark:text-slate-500">{trendLabel}</span>
              ) : null}
              {subLabel ? (
                <span className="text-slate-500 dark:text-slate-400">{subLabel}</span>
              ) : null}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
});

export { StatTile };
