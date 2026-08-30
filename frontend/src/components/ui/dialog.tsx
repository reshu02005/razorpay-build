'use client';

/**
 * Dialog - a modal, written by hand because Radix is not a dependency here.
 *
 * This is the component that stands between an operator and a money-moving
 * action ("Approve this recovery?", "Reject and close the case?"), so its
 * keyboard behaviour is not cosmetic. Each of the four behaviours below is
 * implemented deliberately; a modal that has none of them is unusable without a
 * mouse and, on a confirmation dialog, that means the keyboard user is locked
 * out of the decision entirely.
 *
 *   1. Escape closes.
 *      The listener is on `document`, not on the panel, so it fires even when
 *      focus has drifted somewhere unexpected. A modal you cannot dismiss from
 *      the keyboard is a trap.
 *
 *   2. Focus moves into the panel on open.
 *      The panel carries `tabIndex={-1}` and is focused programmatically, so
 *      the screen reader immediately reads the dialog's title instead of
 *      leaving the user parked on the trigger behind the overlay.
 *
 *   3. Focus is restored to the trigger on close.
 *      The element that was focused at open time is captured and re-focused on
 *      unmount. Without it, closing the dialog dumps focus at the top of the
 *      document and the user has to Tab all the way back to where they were.
 *
 *   4. Tab is trapped inside the panel.
 *      Tab from the last focusable element wraps to the first and Shift+Tab
 *      wraps backwards. Otherwise Tab walks out of the modal and into the page
 *      underneath, which is still visible through the overlay - the user ends
 *      up interacting with content that `aria-modal` has told their screen
 *      reader does not exist.
 *
 * Plus background scroll lock, so the page behind cannot be scrolled away while
 * the decision is open.
 *
 * No portal: `createPortal` needs a live `document`, which complicates server
 * rendering for no benefit here. A `fixed inset-0 z-50` overlay is visually
 * equivalent as long as no ancestor creates a new stacking context (a CSS
 * `transform`, `filter` or `contain` on a parent would clip it) - so mount the
 * dialog near the top of a route's tree, not inside a transformed card.
 */

import * as React from 'react';
import { X } from 'lucide-react';

import { cn } from '@/lib/utils';

/** Everything a browser will let the user Tab to, used for the focus trap. */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export type DialogSize = 'sm' | 'md' | 'lg';

const SIZE_CLASSES: Record<DialogSize, string> = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
};

export interface DialogProps extends Omit<React.HTMLAttributes<HTMLDivElement>, 'title'> {
  open: boolean;
  /** Called with `false` on Escape, overlay click or the close button. */
  onOpenChange: (open: boolean) => void;
  /**
   * Required. It becomes the dialog's accessible name via `aria-labelledby`,
   * and a modal without an accessible name is announced as just "dialog".
   * Making it a prop rather than a slot is what guarantees it is never omitted.
   */
  title: React.ReactNode;
  description?: React.ReactNode;
  size?: DialogSize;
  /**
   * Click-outside-to-close. Turn it off for a confirmation the user should have
   * to answer deliberately - an accidental click on the backdrop silently
   * cancelling an approval is a bad outcome either way round.
   */
  closeOnOverlayClick?: boolean;
  hideCloseButton?: boolean;
  /** Action row pinned to the bottom of the panel. Use <DialogFooter>. */
  footer?: React.ReactNode;
}

const Dialog = React.forwardRef<HTMLDivElement, DialogProps>(function Dialog(
  {
    className,
    open,
    onOpenChange,
    title,
    description,
    size = 'md',
    closeOnOverlayClick = true,
    hideCloseButton = false,
    footer,
    children,
    ...props
  },
  ref,
) {
  const panelRef = React.useRef<HTMLDivElement | null>(null);
  const baseId = React.useId();
  const titleId = `${baseId}-title`;
  const descriptionId = `${baseId}-description`;

  const setRefs = React.useCallback(
    (node: HTMLDivElement | null) => {
      panelRef.current = node;
      if (typeof ref === 'function') ref(node);
      else if (ref) ref.current = node;
    },
    [ref],
  );

  // (1) Escape closes, from anywhere.
  React.useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      // Stop the key from also reaching anything behind the modal - only the
      // topmost surface should react to a single Escape press.
      event.stopPropagation();
      onOpenChange(false);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open, onOpenChange]);

  // (2) + (3) Move focus in on open, put it back on close.
  React.useEffect(() => {
    if (!open) return;
    const previouslyFocused =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panelRef.current?.focus();
    return () => {
      previouslyFocused?.focus();
    };
  }, [open]);

  // Background scroll lock. The previous value is captured and restored rather
  // than hard-reset to '', so nesting or an app-level lock is not clobbered.
  React.useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  // All hooks run before this point: the early return must never change the
  // number of hooks React sees between renders.
  if (!open) return null;

  // (4) The focus trap.
  const handlePanelKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Tab') return;
    const panel = panelRef.current;
    if (panel === null) return;

    const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
      // `offsetParent === null` catches elements hidden with `display:none`,
      // which are in the DOM but not tabbable - focusing one would look like
      // focus vanishing.
      (element) => element.offsetParent !== null || element === panel,
    );

    if (focusable.length === 0) {
      // Nothing to tab to: keep focus on the panel rather than letting it
      // escape to the page behind.
      event.preventDefault();
      panel.focus();
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;

    const active = document.activeElement;

    if (event.shiftKey && (active === first || active === panel)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    } else if (!event.shiftKey && active === panel) {
      // Opening focuses the panel itself; the first Tab should enter the
      // content rather than jump past the whole dialog.
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* The overlay is inert to assistive technology - `aria-modal` on the
          panel is what hides the rest of the page - so it is aria-hidden and
          exists only to dim the background and catch outside clicks. */}
      <div
        aria-hidden="true"
        onClick={closeOnOverlayClick ? () => onOpenChange(false) : undefined}
        className="absolute inset-0 bg-slate-950/40"
      />

      <div
        ref={setRefs}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={handlePanelKeyDown}
        className={cn(
          'relative w-full rounded-lg border border-slate-200 bg-white shadow-sm outline-none',
          'dark:border-slate-800 dark:bg-slate-950',
          'max-h-[85vh] overflow-y-auto',
          SIZE_CLASSES[size],
          className,
        )}
        {...props}
      >
        <div className="flex items-start justify-between gap-4 px-5 pb-2 pt-5">
          <div className="min-w-0 space-y-1">
            <h2
              id={titleId}
              className="text-base font-semibold leading-tight text-slate-900 dark:text-slate-50"
            >
              {title}
            </h2>
            {description ? (
              <p
                id={descriptionId}
                className="text-xs leading-relaxed text-slate-500 dark:text-slate-400"
              >
                {description}
              </p>
            ) : null}
          </div>

          {hideCloseButton ? null : (
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              // An icon-only control needs a text label; "Close" is what the
              // screen reader announces in place of the glyph.
              aria-label="Close dialog"
              className="-mr-1 -mt-1 shrink-0 rounded-md p-1.5 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          )}
        </div>

        <div className="px-5 py-3 text-sm text-slate-700 dark:text-slate-300">{children}</div>

        {footer}
      </div>
    </div>
  );
});

export interface DialogFooterProps extends React.HTMLAttributes<HTMLDivElement> {}

/**
 * Action row. Right-aligned on wide viewports and full-width stacked on narrow
 * ones, with the confirming action last - the rightmost position is where the
 * eye finishes, and on a destructive dialog that ordering makes the cancel
 * button the one closer to the thumb.
 */
const DialogFooter = React.forwardRef<HTMLDivElement, DialogFooterProps>(function DialogFooter(
  { className, ...props },
  ref,
) {
  return (
    <div
      ref={ref}
      className={cn(
        'flex flex-col-reverse gap-2 border-t border-slate-200 px-5 py-3 sm:flex-row sm:justify-end dark:border-slate-800',
        className,
      )}
      {...props}
    />
  );
});

export { Dialog, DialogFooter };
