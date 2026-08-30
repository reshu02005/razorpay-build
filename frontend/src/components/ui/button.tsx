'use client';

/**
 * Button - the console's only interactive primitive.
 *
 * ---------------------------------------------------------------------------
 * Why there is no "shadcn" package in package.json
 * ---------------------------------------------------------------------------
 * shadcn/ui is not a dependency; it is a *distribution model*. The authentic
 * usage is to copy a component's source into your own repository and own it
 * from then on, exactly as this file does. Nothing is installed, nothing is
 * versioned against us, and every class name here is editable in place. That is
 * why these files live under `src/components/ui/` as ordinary source rather
 * than under `node_modules/`, and why no CLI was run to produce them.
 *
 * A second consequence, deliberate here: Radix UI is not installed either, so
 * the primitives that would normally be Radix wrappers (tabs, dialog, tooltip)
 * are small accessible implementations written by hand in this folder.
 *
 * ---------------------------------------------------------------------------
 * The `loading` prop
 * ---------------------------------------------------------------------------
 * This console approves real money movement. A merchant under time pressure
 * double-clicking "Approve" is not an edge case, it is the expected behaviour.
 * `loading` sets `disabled` and `aria-busy` on the same render that shows the
 * spinner, so the second click lands on a disabled element and never reaches
 * the handler. That is the cheapest possible defence against a double-approval
 * and it costs one prop at the call site. It does not replace the server-side
 * state-machine check (an approval on an already-approved case must still be
 * rejected by the API) - it just stops the request from ever being sent twice.
 */

import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';

import { Spinner } from '@/components/ui/spinner';
import { cn } from '@/lib/utils';

const buttonVariants = cva(
  [
    'inline-flex items-center justify-center gap-2 whitespace-nowrap',
    'rounded-lg border text-sm font-medium',
    'transition-colors duration-150',
    // A visible focus ring is non-negotiable: the approve/reject flow has to be
    // completable from the keyboard alone.
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2',
    'focus-visible:ring-offset-white dark:focus-visible:ring-offset-slate-950',
    // `pointer-events-none` while disabled also suppresses the hover styling,
    // so a disabled button never looks clickable.
    'disabled:pointer-events-none disabled:opacity-50',
    'select-none',
  ].join(' '),
  {
    variants: {
      variant: {
        default:
          'border-transparent bg-slate-900 text-slate-50 hover:bg-slate-800 focus-visible:ring-slate-500 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white',
        secondary:
          'border-transparent bg-slate-100 text-slate-900 hover:bg-slate-200 focus-visible:ring-slate-400 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700',
        outline:
          'border-slate-200 bg-white text-slate-900 hover:bg-slate-50 focus-visible:ring-slate-400 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-100 dark:hover:bg-slate-900',
        ghost:
          'border-transparent bg-transparent text-slate-600 hover:bg-slate-100 hover:text-slate-900 focus-visible:ring-slate-400 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100',
        // Emerald is reserved for "this moves the case forward" - approve,
        // confirm recovery. It is never used for plain navigation.
        success:
          'border-transparent bg-emerald-600 text-white hover:bg-emerald-500 focus-visible:ring-emerald-500 dark:bg-emerald-600 dark:hover:bg-emerald-500',
        // Rose is reserved strictly for destructive or denying actions
        // (reject, mark failed). Never for "cancel".
        danger:
          'border-transparent bg-rose-600 text-white hover:bg-rose-500 focus-visible:ring-rose-500 dark:bg-rose-600 dark:hover:bg-rose-500',
      },
      size: {
        sm: 'h-8 px-3 text-xs',
        md: 'h-9 px-4',
        lg: 'h-11 px-6 text-base',
        icon: 'h-9 w-9 p-0',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'md',
    },
  },
);

export type ButtonVariant = NonNullable<VariantProps<typeof buttonVariants>['variant']>;
export type ButtonSize = NonNullable<VariantProps<typeof buttonVariants>['size']>;

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /** Shows the spinner, disables the button and sets `aria-busy`. */
  loading?: boolean;
  /**
   * Optional replacement label while `loading` is true ("Approving…"). Telling
   * the operator what is happening beats a bare spinner during a money action.
   */
  loadingText?: React.ReactNode;
  /** Icon rendered before the label. Hidden while loading (the spinner takes its slot). */
  leadingIcon?: React.ReactNode;
  /** Icon rendered after the label. */
  trailingIcon?: React.ReactNode;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    className,
    variant,
    size,
    loading = false,
    loadingText,
    leadingIcon,
    trailingIcon,
    disabled,
    children,
    // Default to `type="button"`. An untyped <button> inside a <form> submits
    // it, which in this app would mean an accidental POST from a filter row.
    type = 'button',
    ...props
  },
  ref,
) {
  const isDisabled = disabled === true || loading;

  return (
    <button
      ref={ref}
      type={type}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? (
        <Spinner size={size === 'lg' ? 'md' : 'xs'} />
      ) : (
        leadingIcon
      )}
      {loading && loadingText !== undefined ? loadingText : children}
      {!loading && trailingIcon}
    </button>
  );
});

export { Button, buttonVariants };
