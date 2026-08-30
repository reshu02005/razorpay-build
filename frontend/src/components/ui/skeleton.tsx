/**
 * Skeleton - the placeholder shown while a panel's data is in flight.
 *
 * Two rules it exists to enforce:
 *
 * 1. A skeleton must occupy the same box the real content will, otherwise the
 *    dashboard jumps when four independent panels resolve at different times.
 *    Callers therefore pass explicit height/width classes rather than getting a
 *    one-size-fits-all bar.
 *
 * 2. A skeleton is decorative. It is `aria-hidden`, because a screen reader
 *    reading out six empty grey boxes is worse than silence. The *container*
 *    that swaps skeleton for data is the thing that should carry
 *    `aria-busy="true"` and announce the change.
 */

import * as React from 'react';

import { cn } from '@/lib/utils';

export interface SkeletonProps extends React.HTMLAttributes<HTMLDivElement> {}

const Skeleton = React.forwardRef<HTMLDivElement, SkeletonProps>(function Skeleton(
  { className, ...props },
  ref,
) {
  return (
    <div
      ref={ref}
      aria-hidden="true"
      className={cn('animate-pulse rounded-md bg-slate-200 dark:bg-slate-800', className)}
      {...props}
    />
  );
});

export interface SkeletonTextProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Number of placeholder lines. */
  lines?: number;
}

/**
 * A stack of text-height bars. The last line is rendered short because real
 * paragraphs rarely end flush with the right margin - the small irregularity is
 * what stops the placeholder reading as a broken table.
 */
const SkeletonText = React.forwardRef<HTMLDivElement, SkeletonTextProps>(function SkeletonText(
  { className, lines = 3, ...props },
  ref,
) {
  const count = Math.max(1, Math.floor(lines));

  return (
    <div ref={ref} aria-hidden="true" className={cn('space-y-2', className)} {...props}>
      {Array.from({ length: count }, (_unused, index) => (
        <Skeleton
          key={index}
          className={cn('h-3 w-full', index === count - 1 && count > 1 && 'w-2/3')}
        />
      ))}
    </div>
  );
});

export { Skeleton, SkeletonText };
