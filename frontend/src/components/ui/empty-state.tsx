/**
 * EmptyState - what a panel shows when it has nothing to show.
 *
 * The wording rule this component exists to enforce: an empty state must say
 * *why* it is empty and what would fill it, never just "No data". In this
 * console the two are completely different situations - "no failed payments in
 * the last 24 hours" is good news, while "no recovery cases yet - analyse a
 * failed payment to create one" is an instruction. A shared "No data" would
 * flatten both into a shrug, and an operator scanning for something to act on
 * would have to go and check.
 *
 * The border is dashed on purpose: a solid border reads as a populated card at
 * a glance, and the difference between "loaded and empty" and "still loading"
 * has to be obvious. Loading states use <Skeleton />, not this.
 */

import * as React from 'react';

import { cn } from '@/lib/utils';

export type EmptyStateSize = 'sm' | 'md';

export interface EmptyStateProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Usually a lucide icon at `h-5 w-5`. Purely decorative. */
  icon?: React.ReactNode;
  /** One short sentence naming what is missing. */
  title: string;
  /** One sentence explaining why, or what to do about it. */
  description?: string;
  /** Optional primary action - the thing that would make this panel non-empty. */
  action?: React.ReactNode;
  size?: EmptyStateSize;
}

const EmptyState = React.forwardRef<HTMLDivElement, EmptyStateProps>(function EmptyState(
  { className, icon, title, description, action, size = 'md', ...props },
  ref,
) {
  return (
    <div
      ref={ref}
      className={cn(
        'flex flex-col items-center justify-center rounded-lg border border-dashed border-slate-200 text-center dark:border-slate-800',
        size === 'sm' ? 'gap-2 px-4 py-8' : 'gap-3 px-6 py-14',
        className,
      )}
      {...props}
    >
      {icon ? (
        <span
          aria-hidden="true"
          className="flex h-9 w-9 items-center justify-center rounded-full bg-slate-100 text-slate-400 dark:bg-slate-900 dark:text-slate-500"
        >
          {icon}
        </span>
      ) : null}
      <p className="text-sm font-medium text-slate-700 dark:text-slate-200">{title}</p>
      {description ? (
        <p className="max-w-sm text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          {description}
        </p>
      ) : null}
      {action ? <div className="pt-1">{action}</div> : null}
    </div>
  );
});

export { EmptyState };
