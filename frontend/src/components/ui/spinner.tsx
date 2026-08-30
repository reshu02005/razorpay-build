/**
 * Spinner - the single "work in progress" indicator for the console.
 *
 * Deliberately dependency-free: it is a hand-drawn SVG rather than an icon from
 * lucide-react so that the arc geometry, stroke weight and the two-tone
 * (track + head) look stay identical at every size. It is also the primitive
 * <Button loading> renders, so it must have no hooks and no client-only code -
 * that keeps it renderable from a server component too.
 *
 * Accessibility: by default the spinner is decorative (`aria-hidden`). A bare
 * spinner announcing "loading" from inside a button that already carries
 * `aria-busy` would make a screen reader say the same thing twice. Pass `label`
 * only when the spinner is the *only* thing on screen telling the user to wait.
 */

import * as React from 'react';

import { cn } from '@/lib/utils';

export type SpinnerSize = 'xs' | 'sm' | 'md' | 'lg';

const SPINNER_SIZES: Record<SpinnerSize, string> = {
  xs: 'h-3 w-3',
  sm: 'h-4 w-4',
  md: 'h-5 w-5',
  lg: 'h-8 w-8',
};

export interface SpinnerProps extends React.SVGAttributes<SVGSVGElement> {
  /** Visual size. Matches the x-height of the text it usually sits next to. */
  size?: SpinnerSize;
  /**
   * Accessible name. When omitted the spinner is treated as decorative, which
   * is the right default whenever a surrounding control already announces the
   * busy state.
   */
  label?: string;
}

const Spinner = React.forwardRef<SVGSVGElement, SpinnerProps>(function Spinner(
  { size = 'sm', label, className, ...props },
  ref,
) {
  const decorative = label === undefined;

  return (
    <svg
      ref={ref}
      viewBox="0 0 24 24"
      fill="none"
      // `currentColor` everywhere: the spinner inherits whatever colour the
      // button or text block it lives in already resolved to, so it never needs
      // a variant of its own.
      className={cn('animate-spin text-current', SPINNER_SIZES[size], className)}
      role={decorative ? undefined : 'status'}
      aria-hidden={decorative ? true : undefined}
      aria-label={label}
      {...props}
    >
      {/* Track: the full circle at low opacity. */}
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.5" className="opacity-20" />
      {/* Head: a quarter arc at full opacity - this is what reads as motion. */}
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        className="opacity-90"
      />
    </svg>
  );
});

export { Spinner };
