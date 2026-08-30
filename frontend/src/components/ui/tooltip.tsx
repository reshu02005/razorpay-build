'use client';

/**
 * Tooltip - a small hover/focus hint, written by hand (no Radix, no Floating UI).
 *
 * It is used for the things this console must explain without spending screen
 * space: what guardrail R7 actually checks, what "propensity" means, why an
 * approve button is disabled. Those explanations matter to a first-time
 * operator, so the tooltip has to be reachable by keyboard as well as mouse.
 *
 * What is deliberate here:
 *
 * - It opens on focus, not only on hover. A hint that exists only for a mouse
 *   is a hint half the users never see.
 * - Escape dismisses it while the trigger keeps focus, which is what a user
 *   does when the bubble covers the thing they were reading.
 * - `aria-describedby` is cloned onto the trigger element rather than sitting
 *   on a wrapper, so a screen reader reads the button's own label first and the
 *   hint second - the wrapper's attributes would not be announced at all.
 * - The bubble is `pointer-events-none`. If it could take the pointer, moving
 *   the mouse from the trigger onto the bubble would fire mouseleave on the
 *   trigger and the tooltip would flicker.
 *
 * No positioning library, by design: `side` picks one of four absolute
 * placements relative to the trigger. That is enough for a hint next to a badge
 * or an icon button, and it costs nothing at runtime. It does not flip itself
 * near a viewport edge - pass `side="left"` for triggers at the right margin.
 */

import * as React from 'react';

import { cn } from '@/lib/utils';

export type TooltipSide = 'top' | 'right' | 'bottom' | 'left';

const SIDE_CLASSES: Record<TooltipSide, string> = {
  top: 'bottom-full left-1/2 -translate-x-1/2 mb-2',
  bottom: 'top-full left-1/2 -translate-x-1/2 mt-2',
  left: 'right-full top-1/2 -translate-y-1/2 mr-2',
  right: 'left-full top-1/2 -translate-y-1/2 ml-2',
};

export interface TooltipProps extends Omit<React.HTMLAttributes<HTMLSpanElement>, 'content'> {
  /** The hint. Keep it to one short sentence; this is not a place for prose. */
  content: React.ReactNode;
  side?: TooltipSide;
  /**
   * Hover delay in milliseconds. A short delay stops a row of icon buttons
   * firing a trail of tooltips as the pointer crosses them. Focus opens
   * immediately - a keyboard user asked for it explicitly.
   */
  delayMs?: number;
  /** Renders the trigger untouched with no hint attached. */
  disabled?: boolean;
  /**
   * The trigger. A single focusable element (a button) is strongly preferred:
   * a hint attached to plain text cannot be reached by keyboard at all.
   */
  children: React.ReactNode;
}

const Tooltip = React.forwardRef<HTMLSpanElement, TooltipProps>(function Tooltip(
  { className, content, side = 'top', delayMs = 150, disabled = false, children, ...props },
  ref,
) {
  const [open, setOpen] = React.useState(false);
  const tooltipId = React.useId();
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = React.useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // A pending timer that fires after unmount would call setState on a gone
  // component; clearing it on unmount is the whole reason the ref exists.
  React.useEffect(() => clearTimer, [clearTimer]);

  const openAfterDelay = () => {
    clearTimer();
    if (delayMs <= 0) {
      setOpen(true);
      return;
    }
    timerRef.current = setTimeout(() => setOpen(true), delayMs);
  };

  const closeNow = () => {
    clearTimer();
    setOpen(false);
  };

  // `aria-describedby` points at the bubble only while it is rendered; a
  // dangling reference to a removed node is worse than no reference at all.
  const describedBy = open ? tooltipId : undefined;
  const trigger = React.isValidElement<{ 'aria-describedby'?: string }>(children)
    ? React.cloneElement(children, { 'aria-describedby': describedBy })
    : children;

  if (disabled) {
    return <>{children}</>;
  }

  return (
    <span
      ref={ref}
      className={cn('relative inline-flex', className)}
      onMouseEnter={openAfterDelay}
      onMouseLeave={closeNow}
      // Focus/blur bubble through the wrapper (React normalises focusin), so
      // the hint appears when the trigger inside is tabbed to.
      onFocus={() => setOpen(true)}
      onBlur={closeNow}
      onKeyDown={(event) => {
        if (event.key === 'Escape') closeNow();
      }}
      {...props}
    >
      {trigger}
      {open ? (
        <span
          role="tooltip"
          id={tooltipId}
          className={cn(
            'pointer-events-none absolute z-50 max-w-xs rounded-md px-2 py-1',
            'text-xs font-normal leading-snug shadow-sm',
            'bg-slate-900 text-slate-50 dark:bg-slate-800 dark:text-slate-100',
            // The bubble is allowed to wrap: truncating an explanation of a
            // guardrail into one line defeats the point of having it.
            'w-max whitespace-normal',
            SIDE_CLASSES[side],
          )}
        >
          {content}
        </span>
      ) : null}
    </span>
  );
});

export { Tooltip };
