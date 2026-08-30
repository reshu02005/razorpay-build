'use client';

/**
 * Tabs - a hand-written implementation of the WAI-ARIA tabs pattern.
 *
 * Radix is not a dependency of this project, so rather than reach for a
 * `<div onClick>` pretending to be a tab, the pattern is implemented directly.
 * What that means concretely, and why each piece is here:
 *
 * - The active value lives in React context, so <TabsTrigger> and <TabsContent>
 *   can be placed anywhere inside <Tabs> without prop-drilling. Both controlled
 *   (`value` + `onValueChange`) and uncontrolled (`defaultValue`) use is
 *   supported, because the recovery screen wants the active tab in the URL and
 *   the smaller panels do not.
 *
 * - role="tablist" / role="tab" / role="tabpanel" with `aria-selected`,
 *   `aria-controls` and `aria-labelledby` wired both ways. Without the id pair a
 *   screen reader announces the tab but cannot tell the user what it controls.
 *
 * - Roving tabindex: exactly one trigger is in the page tab order at a time
 *   (`tabIndex=0` on the selected tab, `-1` on the rest). This is the part
 *   people usually skip. Without it, a five-tab strip costs five Tab presses to
 *   walk past; with it, Tab reaches the strip and the arrow keys move inside it.
 *
 * - Arrow keys move focus *and* activate (automatic activation). These panels
 *   render already-fetched data, so there is no cost to switching on focus, and
 *   automatic activation is what a sighted keyboard user expects.
 */

import * as React from 'react';

import { cn } from '@/lib/utils';

interface TabsContextValue {
  value: string;
  select: (value: string) => void;
  /** Shared id root so trigger and panel ids can be derived from the value. */
  baseId: string;
}

const TabsContext = React.createContext<TabsContextValue | null>(null);

function useTabsContext(component: string): TabsContextValue {
  const context = React.useContext(TabsContext);
  if (context === null) {
    // Failing loudly beats rendering a tab that silently does nothing.
    throw new Error(`<${component}> must be rendered inside <Tabs>.`);
  }
  return context;
}

/** Ids are derived, not stored, so a trigger and its panel always agree. */
function triggerId(baseId: string, value: string): string {
  return `${baseId}-trigger-${value}`;
}

function panelId(baseId: string, value: string): string {
  return `${baseId}-panel-${value}`;
}

export type TabsOrientation = 'horizontal' | 'vertical';

export interface TabsProps extends Omit<React.HTMLAttributes<HTMLDivElement>, 'onChange'> {
  /** Controlled active value. Pass together with `onValueChange`. */
  value?: string;
  /** Uncontrolled initial value. Supply one, or no tab is selected on mount. */
  defaultValue?: string;
  onValueChange?: (value: string) => void;
}

const Tabs = React.forwardRef<HTMLDivElement, TabsProps>(function Tabs(
  { className, value, defaultValue, onValueChange, children, ...props },
  ref,
) {
  const baseId = React.useId();
  const [uncontrolledValue, setUncontrolledValue] = React.useState<string>(defaultValue ?? '');

  const isControlled = value !== undefined;
  const activeValue = isControlled ? value : uncontrolledValue;

  const select = React.useCallback(
    (next: string) => {
      // The internal state is updated only when uncontrolled; a controlled
      // parent owns the value and must not be second-guessed here.
      if (!isControlled) setUncontrolledValue(next);
      onValueChange?.(next);
    },
    [isControlled, onValueChange],
  );

  const contextValue = React.useMemo<TabsContextValue>(
    () => ({ value: activeValue, select, baseId }),
    [activeValue, select, baseId],
  );

  return (
    <TabsContext.Provider value={contextValue}>
      <div ref={ref} className={cn('w-full', className)} {...props}>
        {children}
      </div>
    </TabsContext.Provider>
  );
});

export interface TabsListProps extends React.HTMLAttributes<HTMLDivElement> {
  orientation?: TabsOrientation;
}

const TabsList = React.forwardRef<HTMLDivElement, TabsListProps>(function TabsList(
  { className, orientation = 'horizontal', onKeyDown, ...props },
  ref,
) {
  const listRef = React.useRef<HTMLDivElement | null>(null);

  // Merge the forwarded ref with the local one: the local ref is needed to find
  // the sibling triggers for arrow navigation, and the caller may still want
  // the node for scrolling or measurement.
  const setRefs = React.useCallback(
    (node: HTMLDivElement | null) => {
      listRef.current = node;
      if (typeof ref === 'function') ref(node);
      else if (ref) ref.current = node;
    },
    [ref],
  );

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    onKeyDown?.(event);
    if (event.defaultPrevented) return;

    const nextKey = orientation === 'horizontal' ? 'ArrowRight' : 'ArrowDown';
    const previousKey = orientation === 'horizontal' ? 'ArrowLeft' : 'ArrowUp';
    // Annotated as string[] so the membership test compares against
    // KeyboardEvent.key (a plain string) rather than a literal union.
    const handledKeys: string[] = [nextKey, previousKey, 'Home', 'End'];
    if (!handledKeys.includes(event.key)) return;

    const list = listRef.current;
    if (list === null) return;

    // Disabled triggers are excluded so arrow keys never park focus on a tab
    // the user cannot activate.
    const tabs = Array.from(
      list.querySelectorAll<HTMLButtonElement>('[role="tab"]:not([disabled])'),
    );
    if (tabs.length === 0) return;

    const currentIndex = tabs.findIndex((tab) => tab === document.activeElement);
    let nextIndex: number;

    if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = tabs.length - 1;
    else if (event.key === nextKey) nextIndex = currentIndex < 0 ? 0 : (currentIndex + 1) % tabs.length;
    else nextIndex = currentIndex < 0 ? tabs.length - 1 : (currentIndex - 1 + tabs.length) % tabs.length;

    const nextTab: HTMLButtonElement | undefined = tabs[nextIndex];
    if (!nextTab) return;

    event.preventDefault();
    nextTab.focus();
    // Automatic activation: moving focus selects. `.click()` reuses the
    // trigger's own handler so there is one code path for mouse and keyboard.
    nextTab.click();
  };

  return (
    <div
      ref={setRefs}
      role="tablist"
      aria-orientation={orientation}
      onKeyDown={handleKeyDown}
      className={cn(
        orientation === 'horizontal'
          ? 'flex items-center gap-4 border-b border-slate-200 dark:border-slate-800'
          : 'flex flex-col items-stretch gap-1 border-r border-slate-200 dark:border-slate-800',
        className,
      )}
      {...props}
    />
  );
});

export interface TabsTriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Must match the `value` of exactly one <TabsContent>. */
  value: string;
}

const TabsTrigger = React.forwardRef<HTMLButtonElement, TabsTriggerProps>(function TabsTrigger(
  { className, value, onClick, disabled, children, ...props },
  ref,
) {
  const { value: activeValue, select, baseId } = useTabsContext('TabsTrigger');
  const isActive = activeValue === value;

  return (
    <button
      ref={ref}
      type="button"
      role="tab"
      id={triggerId(baseId, value)}
      aria-selected={isActive}
      aria-controls={panelId(baseId, value)}
      // Roving tabindex - see the file header for why this matters.
      tabIndex={isActive ? 0 : -1}
      disabled={disabled}
      onClick={(event) => {
        onClick?.(event);
        if (event.defaultPrevented) return;
        select(value);
      }}
      className={cn(
        '-mb-px inline-flex items-center gap-2 border-b-2 px-1 pb-2.5 pt-2 text-sm font-medium transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:focus-visible:ring-offset-slate-950',
        'disabled:pointer-events-none disabled:opacity-40',
        isActive
          ? 'border-slate-900 text-slate-900 dark:border-slate-100 dark:text-slate-50'
          : 'border-transparent text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100',
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
});

export interface TabsContentProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Must match the `value` of exactly one <TabsTrigger>. */
  value: string;
  /**
   * Keep the panel mounted while hidden. Use it when the panel holds state the
   * user would lose on a tab switch (a half-typed rejection reason); leave it
   * off for read-only panels so the DOM stays small.
   */
  forceMount?: boolean;
}

const TabsContent = React.forwardRef<HTMLDivElement, TabsContentProps>(function TabsContent(
  { className, value, forceMount = false, children, ...props },
  ref,
) {
  const { value: activeValue, baseId } = useTabsContext('TabsContent');
  const isActive = activeValue === value;

  if (!isActive && !forceMount) return null;

  return (
    <div
      ref={ref}
      role="tabpanel"
      id={panelId(baseId, value)}
      aria-labelledby={triggerId(baseId, value)}
      // A force-mounted inactive panel must be `hidden`, not just visually
      // clipped, or its contents stay in the tab order and the screen reader
      // reads a panel the user cannot see.
      hidden={!isActive}
      // The panel itself is focusable so that after activating a tab, the next
      // Tab press lands inside the content rather than skipping past it.
      tabIndex={0}
      className={cn(
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:focus-visible:ring-offset-slate-950',
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
});

export { Tabs, TabsList, TabsTrigger, TabsContent };
