/**
 * AppShell - the persistent chrome around every route.
 *
 * A sticky header carrying the wordmark, the primary navigation and the theme
 * control, then a single centred content column. That is the whole shell: no
 * sidebar, no breadcrumb rail, no secondary toolbar. This console has six
 * screens, and a navigation structure heavier than the thing it navigates is
 * exactly the enterprise-dashboard feel the product is trying not to have.
 *
 * The header is sticky rather than static because the two longest screens (the
 * audit ledger and the failed-payments table) scroll well past a viewport, and
 * an operator who has scrolled into a table still needs a one-click way back to
 * the dashboard.
 *
 * No `'use client'`: this component holds no state. `Nav` and `ThemeToggle`
 * declare the client boundary themselves, which keeps the shell itself out of
 * the browser bundle.
 */

import type { ReactNode } from "react";
import { IndianRupee } from "lucide-react";
import Link from "next/link";

import { Nav } from "@/components/layout/nav";
import { ThemeToggle } from "@/components/layout/theme-toggle";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      {/* `bg-background/80 + backdrop-blur` rather than a solid fill: content
          scrolling under the header stays faintly visible, which keeps the page
          reading as one surface instead of two stacked panels. */}
      <header className="sticky top-0 z-40 border-b border-border bg-background/80 backdrop-blur">
        <div className="mx-auto flex h-14 w-full max-w-7xl items-center gap-4 px-4 sm:px-6 lg:px-8">
          <Link
            href="/"
            className="flex shrink-0 items-center gap-2 rounded-md text-sm font-semibold tracking-tight"
          >
            <span
              aria-hidden="true"
              className="flex h-7 w-7 items-center justify-center rounded-md bg-primary text-primary-foreground"
            >
              <IndianRupee className="h-4 w-4" />
            </span>
            <span className="font-display">RecoverAI</span>
          </Link>

          <Nav className="min-w-0 flex-1" />

          <ThemeToggle />
        </div>
      </header>

      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-8 sm:px-6 lg:px-8">{children}</main>

      {/* The footer states the operating envelope rather than a copyright line.
          Anyone who lands on this console should be able to read, without
          clicking anything, that nothing here moves money on its own. */}
      <footer className="border-t border-border">
        <div className="mx-auto w-full max-w-7xl px-4 py-5 text-xs text-muted-foreground sm:px-6 lg:px-8">
          Every money-moving action requires an explicit human approval. The agent proposes; the
          guardrails constrain; you decide.
        </div>
      </footer>
    </div>
  );
}
