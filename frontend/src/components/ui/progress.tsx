/**
 * Progress - an accessible meter.
 *
 * Two very different jobs on this product, hence the `tone` prop:
 *
 * 1. The propensity score on a recovery case. That number is a model output, so
 *    it is drawn in the `ai` tone and is never coloured green-for-high: a high
 *    propensity is not "good", it is just "likely". Colouring it as a success
 *    would nudge the operator toward approving, which is exactly the bias this
 *    screen exists to avoid.
 *
 * 2. The daily budget gauge. That one *is* a threshold, so callers pass
 *    `tone="warning"` / `tone="danger"` as the spend approaches the guardrail
 *    limit. The component does not decide the thresholds itself - the guardrail
 *    values live in the policy engine, and re-deriving them in the UI would be a
 *    second source of truth for a number that governs money.
 *
 * The bar is a real `role="progressbar"` with value/min/max, so it is readable
 * by assistive technology rather than being a decorative div. `valueText` lets
 * the caller give a human phrasing ("₹4,200 of ₹10,000 used today"), which is
 * far more useful announced than "42".
 */

import * as React from 'react';

import { cn } from '@/lib/utils';

export type ProgressTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info' | 'ai';
export type ProgressSize = 'sm' | 'md' | 'lg';

const TONE_CLASSES: Record<ProgressTone, string> = {
  neutral: 'bg-slate-900 dark:bg-slate-100',
  success: 'bg-emerald-500',
  warning: 'bg-amber-500',
  danger: 'bg-rose-500',
  info: 'bg-blue-500',
  ai: 'bg-sky-500',
};

const SIZE_CLASSES: Record<ProgressSize, string> = {
  sm: 'h-1.5',
  md: 'h-2',
  lg: 'h-3',
};

export interface ProgressProps extends Omit<React.HTMLAttributes<HTMLDivElement>, 'children'> {
  /** Current value in the same unit as `max`. Non-finite input is treated as 0. */
  value: number;
  /** Upper bound. Defaults to 100 so a raw percentage just works. */
  max?: number;
  tone?: ProgressTone;
  size?: ProgressSize;
  /** Accessible name, e.g. "Recovery propensity". Required for a standalone meter. */
  label?: string;
  /** Human phrasing of the value for screen readers, e.g. "62 percent". */
  valueText?: string;
  /**
   * Renders a moving stripe with no announced value - for the case where work
   * has started but the total is genuinely unknown.
   */
  indeterminate?: boolean;
}

const Progress = React.forwardRef<HTMLDivElement, ProgressProps>(function Progress(
  {
    className,
    value,
    max = 100,
    tone = 'neutral',
    size = 'md',
    label,
    valueText,
    indeterminate = false,
    ...props
  },
  ref,
) {
  // Defensive clamping happens here, once. A propensity score arriving as 1.0
  // against a max of 1, or a budget that has been overspent past its limit,
  // must not paint a bar wider than its track or a negative width.
  const safeMax = Number.isFinite(max) && max > 0 ? max : 100;
  const safeValue = Number.isFinite(value) ? Math.min(Math.max(value, 0), safeMax) : 0;
  const percent = (safeValue / safeMax) * 100;

  return (
    <div
      ref={ref}
      role="progressbar"
      aria-label={label}
      // An indeterminate progressbar deliberately omits `aria-valuenow`; that
      // absence is the standard signal for "unknown", and reporting a fake 0
      // would be read out as "0 percent complete".
      aria-valuenow={indeterminate ? undefined : Math.round(safeValue * 100) / 100}
      aria-valuemin={0}
      aria-valuemax={safeMax}
      aria-valuetext={indeterminate ? undefined : valueText}
      className={cn(
        'w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800',
        SIZE_CLASSES[size],
        className,
      )}
      {...props}
    >
      <div
        className={cn(
          'h-full rounded-full transition-[width] duration-300 ease-out',
          TONE_CLASSES[tone],
          indeterminate && 'w-1/3 animate-pulse',
        )}
        style={indeterminate ? undefined : { width: `${percent}%` }}
      />
    </div>
  );
});

export { Progress };
