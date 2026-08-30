/**
 * Alert - an inline, non-dismissable message block.
 *
 * Used for the things this console has to say plainly rather than encode in a
 * badge: "this case was blocked by guardrail R7", "the audit chain is broken at
 * sequence 412", "the agent could not classify this failure".
 *
 * ARIA role is chosen from the variant rather than being left to the caller:
 * `danger` and `warning` render `role="alert"` (assertive - it interrupts,
 * which is correct for a broken audit chain or a denied approval), everything
 * else renders `role="status"` (polite - announced at the next pause). Getting
 * this backwards means either missed failures or a screen reader that talks
 * over the operator. A caller can still override by passing `role` explicitly.
 */

import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { CircleAlert, CircleCheck, Info, Sparkles, TriangleAlert, type LucideIcon } from 'lucide-react';

import { cn } from '@/lib/utils';

const alertVariants = cva('rounded-lg border p-4 text-sm', {
  variants: {
    variant: {
      neutral:
        'border-slate-200 bg-slate-50 text-slate-800 dark:border-slate-800 dark:bg-slate-900/60 dark:text-slate-200',
      info: 'border-blue-200 bg-blue-50 text-blue-900 dark:border-blue-900 dark:bg-blue-950/60 dark:text-blue-200',
      success:
        'border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-200',
      warning:
        'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-200',
      danger:
        'border-rose-200 bg-rose-50 text-rose-900 dark:border-rose-900 dark:bg-rose-950/60 dark:text-rose-200',
      ai: 'border-sky-200 bg-sky-50 text-sky-900 dark:border-sky-900 dark:bg-sky-950/60 dark:text-sky-200',
    },
  },
  defaultVariants: {
    variant: 'neutral',
  },
});

export type AlertVariant = NonNullable<VariantProps<typeof alertVariants>['variant']>;

const DEFAULT_ICONS: Record<AlertVariant, LucideIcon> = {
  neutral: Info,
  info: Info,
  success: CircleCheck,
  warning: TriangleAlert,
  danger: CircleAlert,
  ai: Sparkles,
};

const ICON_COLOURS: Record<AlertVariant, string> = {
  neutral: 'text-slate-500 dark:text-slate-400',
  info: 'text-blue-600 dark:text-blue-400',
  success: 'text-emerald-600 dark:text-emerald-400',
  warning: 'text-amber-600 dark:text-amber-400',
  danger: 'text-rose-600 dark:text-rose-400',
  ai: 'text-sky-600 dark:text-sky-400',
};

export interface AlertProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof alertVariants> {
  /** Replaces the variant's default icon. */
  icon?: React.ReactNode;
  /** Drops the icon entirely - for a dense stack of alerts where it is repetition. */
  hideIcon?: boolean;
}

const Alert = React.forwardRef<HTMLDivElement, AlertProps>(function Alert(
  { className, variant, icon, hideIcon = false, children, ...props },
  ref,
) {
  const tone: AlertVariant = variant ?? 'neutral';
  const DefaultIcon = DEFAULT_ICONS[tone];
  const assertive = tone === 'danger' || tone === 'warning';

  return (
    <div
      ref={ref}
      role={assertive ? 'alert' : 'status'}
      className={cn(alertVariants({ variant }), className)}
      {...props}
    >
      <div className="flex items-start gap-3">
        {hideIcon ? null : (
          <span className={cn('mt-0.5 shrink-0', ICON_COLOURS[tone])}>
            {/* The icon duplicates information already carried by the text and
                the border colour, so it is hidden from assistive technology. */}
            {icon ?? <DefaultIcon className="h-4 w-4" aria-hidden="true" />}
          </span>
        )}
        <div className="min-w-0 flex-1 space-y-1">{children}</div>
      </div>
    </div>
  );
});

export interface AlertTitleProps extends React.HTMLAttributes<HTMLParagraphElement> {}

const AlertTitle = React.forwardRef<HTMLParagraphElement, AlertTitleProps>(function AlertTitle(
  { className, ...props },
  ref,
) {
  return <p ref={ref} className={cn('font-medium leading-tight', className)} {...props} />;
});

export interface AlertDescriptionProps extends React.HTMLAttributes<HTMLDivElement> {}

const AlertDescription = React.forwardRef<HTMLDivElement, AlertDescriptionProps>(
  function AlertDescription({ className, ...props }, ref) {
    return <div ref={ref} className={cn('text-sm leading-relaxed opacity-90', className)} {...props} />;
  },
);

export { Alert, AlertTitle, AlertDescription, alertVariants };
