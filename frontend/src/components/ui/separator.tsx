/**
 * Separator - a one-pixel rule.
 *
 * The only interesting thing about it is the accessibility default. Most rules
 * in this console are pure decoration between two blocks that are already
 * visually distinct, and announcing "separator" for each one just adds noise to
 * a screen reader pass. So `decorative` defaults to true and the element is
 * removed from the accessibility tree; set `decorative={false}` when the rule is
 * genuinely the only thing telling the user that two groups are different
 * (for example between two unrelated action groups in a toolbar).
 */

import * as React from 'react';

import { cn } from '@/lib/utils';

export type SeparatorOrientation = 'horizontal' | 'vertical';

export interface SeparatorProps extends React.HTMLAttributes<HTMLDivElement> {
  orientation?: SeparatorOrientation;
  /** When true (default) the rule is hidden from assistive technology. */
  decorative?: boolean;
}

const Separator = React.forwardRef<HTMLDivElement, SeparatorProps>(function Separator(
  { className, orientation = 'horizontal', decorative = true, ...props },
  ref,
) {
  return (
    <div
      ref={ref}
      role={decorative ? 'none' : 'separator'}
      // `aria-orientation` is only meaningful on a real separator role.
      aria-orientation={decorative ? undefined : orientation}
      className={cn(
        'shrink-0 bg-slate-200 dark:bg-slate-800',
        orientation === 'horizontal' ? 'h-px w-full' : 'h-full w-px',
        className,
      )}
      {...props}
    />
  );
});

export { Separator };
