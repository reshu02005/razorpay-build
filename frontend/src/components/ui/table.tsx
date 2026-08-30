/**
 * Table - the dense list primitive (failed payments, recovery queue, ledger).
 *
 * Design notes worth stating, because they are the difference between a
 * scannable operator console and an enterprise grid:
 *
 * - The <table> is always wrapped in an `overflow-x-auto` container. A payments
 *   row carries an id, an amount, a method, a failure category and a timestamp;
 *   on a laptop that overflows, and horizontal scroll *inside the card* is far
 *   better than the whole page shifting sideways.
 *
 * - Row separation is a single hairline, not zebra striping. Striping fights
 *   with the semantic row colours we actually need (a blocked case, a recovered
 *   case) and doubles the number of background tones on screen.
 *
 * - Numeric columns should be right-aligned and set in the mono stack by the
 *   caller (`className="text-right font-mono tabular-nums"`); amounts that do
 *   not line up on the decimal point cannot be compared at a glance.
 *
 * No hooks here, so no `'use client'`. A client screen that attaches `onClick`
 * to a row can still import these directly.
 */

import * as React from 'react';

import { cn } from '@/lib/utils';

export interface TableProps extends React.TableHTMLAttributes<HTMLTableElement> {
  /** Classes for the scroll container that wraps the table element. */
  containerClassName?: string;
}

const Table = React.forwardRef<HTMLTableElement, TableProps>(function Table(
  { className, containerClassName, ...props },
  ref,
) {
  return (
    <div className={cn('w-full overflow-x-auto', containerClassName)}>
      <table
        ref={ref}
        className={cn('w-full caption-bottom border-collapse text-sm', className)}
        {...props}
      />
    </div>
  );
});

export interface TableSectionProps extends React.HTMLAttributes<HTMLTableSectionElement> {}

const TableHeader = React.forwardRef<HTMLTableSectionElement, TableSectionProps>(
  function TableHeader({ className, ...props }, ref) {
    return (
      <thead
        ref={ref}
        className={cn('border-b border-slate-200 dark:border-slate-800', className)}
        {...props}
      />
    );
  },
);

const TableBody = React.forwardRef<HTMLTableSectionElement, TableSectionProps>(
  function TableBody({ className, ...props }, ref) {
    return (
      <tbody
        ref={ref}
        className={cn('divide-y divide-slate-100 dark:divide-slate-800/70', className)}
        {...props}
      />
    );
  },
);

const TableFooter = React.forwardRef<HTMLTableSectionElement, TableSectionProps>(
  function TableFooter({ className, ...props }, ref) {
    return (
      <tfoot
        ref={ref}
        className={cn(
          'border-t border-slate-200 bg-slate-50 font-medium dark:border-slate-800 dark:bg-slate-900/50',
          className,
        )}
        {...props}
      />
    );
  },
);

export interface TableRowProps extends React.HTMLAttributes<HTMLTableRowElement> {
  /**
   * Adds hover feedback and a pointer cursor. Set it only when the whole row
   * really is clickable - a hover highlight on a row that does nothing is a
   * promise the UI does not keep.
   */
  interactive?: boolean;
  /** Renders the row as the currently selected one. */
  selected?: boolean;
}

const TableRow = React.forwardRef<HTMLTableRowElement, TableRowProps>(function TableRow(
  { className, interactive = false, selected = false, ...props },
  ref,
) {
  return (
    <tr
      ref={ref}
      // `aria-selected` is only valid once the row is part of a selectable
      // collection, so it is emitted only when selection is actually in play.
      aria-selected={selected ? true : undefined}
      className={cn(
        'transition-colors',
        interactive && 'cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-900/60',
        selected && 'bg-slate-50 dark:bg-slate-900/60',
        className,
      )}
      {...props}
    />
  );
});

export interface TableHeadProps extends React.ThHTMLAttributes<HTMLTableCellElement> {}

const TableHead = React.forwardRef<HTMLTableCellElement, TableHeadProps>(function TableHead(
  { className, scope = 'col', ...props },
  ref,
) {
  return (
    <th
      ref={ref}
      // An explicit `scope` is what lets a screen reader announce "Amount,
      // ₹1,240" instead of just the value when moving cell to cell.
      scope={scope}
      className={cn(
        'px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400',
        className,
      )}
      {...props}
    />
  );
});

export interface TableCellProps extends React.TdHTMLAttributes<HTMLTableCellElement> {}

const TableCell = React.forwardRef<HTMLTableCellElement, TableCellProps>(function TableCell(
  { className, ...props },
  ref,
) {
  return <td ref={ref} className={cn('px-4 py-3 align-middle', className)} {...props} />;
});

export interface TableCaptionProps extends React.HTMLAttributes<HTMLTableCaptionElement> {}

const TableCaption = React.forwardRef<HTMLTableCaptionElement, TableCaptionProps>(
  function TableCaption({ className, ...props }, ref) {
    return (
      <caption
        ref={ref}
        className={cn('mt-3 text-xs text-slate-500 dark:text-slate-400', className)}
        {...props}
      />
    );
  },
);

export { Table, TableHeader, TableBody, TableFooter, TableRow, TableHead, TableCell, TableCaption };
