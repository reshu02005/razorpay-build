/**
 * Card - the surface every panel in the console sits on.
 *
 * One border, one radius, `shadow-sm` at most. The dashboard stacks a dozen of
 * these on one screen; anything heavier (drop shadows, gradients, coloured
 * backgrounds) turns an operator's field of view into noise. Meaning is carried
 * by the badges and tiles *inside* the card, never by the card itself.
 *
 * No hooks and no handlers here, so there is no `'use client'` directive: these
 * pieces can be rendered directly from a server component.
 */

import * as React from 'react';

import { cn } from '@/lib/utils';

export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {}

const Card = React.forwardRef<HTMLDivElement, CardProps>(function Card(
  { className, ...props },
  ref,
) {
  return (
    <div
      ref={ref}
      className={cn(
        'rounded-lg border border-slate-200 bg-white text-slate-900',
        'dark:border-slate-800 dark:bg-slate-950 dark:text-slate-100',
        className,
      )}
      {...props}
    />
  );
});

export interface CardHeaderProps extends React.HTMLAttributes<HTMLDivElement> {
  /**
   * Right-aligned slot for a control that belongs to the card (a filter, a
   * "view all" link). Keeping it in the header rather than floating above the
   * card is what stops the page turning into a field of stray buttons.
   */
  action?: React.ReactNode;
}

const CardHeader = React.forwardRef<HTMLDivElement, CardHeaderProps>(function CardHeader(
  { className, action, children, ...props },
  ref,
) {
  return (
    <div
      ref={ref}
      className={cn('flex items-start justify-between gap-4 px-5 pb-3 pt-5', className)}
      {...props}
    >
      <div className="min-w-0 space-y-1">{children}</div>
      {action ? <div className="flex shrink-0 items-center gap-2">{action}</div> : null}
    </div>
  );
});

export interface CardTitleProps extends React.HTMLAttributes<HTMLHeadingElement> {}

const CardTitle = React.forwardRef<HTMLHeadingElement, CardTitleProps>(function CardTitle(
  { className, ...props },
  ref,
) {
  return (
    <h3
      ref={ref}
      className={cn('text-sm font-semibold leading-none tracking-tight', className)}
      {...props}
    />
  );
});

export interface CardDescriptionProps extends React.HTMLAttributes<HTMLParagraphElement> {}

const CardDescription = React.forwardRef<HTMLParagraphElement, CardDescriptionProps>(
  function CardDescription({ className, ...props }, ref) {
    return (
      <p
        ref={ref}
        className={cn('text-xs leading-relaxed text-slate-500 dark:text-slate-400', className)}
        {...props}
      />
    );
  },
);

export interface CardContentProps extends React.HTMLAttributes<HTMLDivElement> {}

const CardContent = React.forwardRef<HTMLDivElement, CardContentProps>(function CardContent(
  { className, ...props },
  ref,
) {
  return <div ref={ref} className={cn('px-5 pb-5', className)} {...props} />;
});

export interface CardFooterProps extends React.HTMLAttributes<HTMLDivElement> {}

const CardFooter = React.forwardRef<HTMLDivElement, CardFooterProps>(function CardFooter(
  { className, ...props },
  ref,
) {
  return (
    <div
      ref={ref}
      className={cn(
        'flex items-center gap-2 border-t border-slate-200 px-5 py-3 dark:border-slate-800',
        className,
      )}
      {...props}
    />
  );
});

export { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter };
