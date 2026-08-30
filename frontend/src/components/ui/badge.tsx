/**
 * Badge - a compact, coloured status token.
 *
 * The variants are named after *meaning*, not colour, because the mapping from
 * a backend enum to a tone is a product decision that must live in one place:
 * a recovery status of `recovered` is `success` forever, even if the palette
 * changes. Screens should map enum -> variant once and never hand-pick a class.
 *
 *   neutral  slate    - informational, no judgement (a method, an id, a count)
 *   success  emerald  - recovered, captured, guardrail allowed
 *   warning  amber    - awaiting approval, awaiting payment, caution
 *   danger   rose     - failed, blocked, rejected, denied
 *   info     blue     - a system fact worth noticing (executing, expired)
 *   ai       sky      - produced by the agent (classification, strategy, score)
 *
 * `info` and `ai` are both cool blues on purpose - they read as related - but
 * they are kept distinct so a reader can always tell "the system says" from
 * "the model says". That distinction matters on a screen where a human is
 * being asked to trust an LLM's recommendation with money.
 */

import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';

import { cn } from '@/lib/utils';

const badgeVariants = cva(
  'inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium leading-5 whitespace-nowrap',
  {
    variants: {
      variant: {
        neutral:
          'border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300',
        success:
          'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-300',
        warning:
          'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300',
        danger:
          'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-300',
        info: 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-300',
        ai: 'border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-900 dark:bg-sky-950 dark:text-sky-300',
      },
    },
    defaultVariants: {
      variant: 'neutral',
    },
  },
);

/** Dot colours are a shade stronger than the text so they survive at 6px. */
const DOT_COLOURS: Record<BadgeVariant, string> = {
  neutral: 'bg-slate-400 dark:bg-slate-500',
  success: 'bg-emerald-500',
  warning: 'bg-amber-500',
  danger: 'bg-rose-500',
  info: 'bg-blue-500',
  ai: 'bg-sky-500',
};

export type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>['variant']>;

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {
  /**
   * Renders a small leading dot. Use it in dense lists where the badge is the
   * only status signal in the row - the dot gives a second, colour-independent
   * cue (position) for anyone who cannot separate emerald from amber.
   */
  dot?: boolean;
  /** Optional leading icon; ignored when `dot` is set so the token stays small. */
  icon?: React.ReactNode;
}

const Badge = React.forwardRef<HTMLSpanElement, BadgeProps>(function Badge(
  { className, variant, dot = false, icon, children, ...props },
  ref,
) {
  const tone: BadgeVariant = variant ?? 'neutral';

  return (
    <span ref={ref} className={cn(badgeVariants({ variant }), className)} {...props}>
      {dot ? (
        <span aria-hidden="true" className={cn('h-1.5 w-1.5 shrink-0 rounded-full', DOT_COLOURS[tone])} />
      ) : (
        icon
      )}
      {children}
    </span>
  );
});

export { Badge, badgeVariants };
