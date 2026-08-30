/**
 * Layout for the merchant console.
 *
 * `(console)` is a Next.js route group: the parentheses mean it groups files
 * without adding a URL segment, so `(console)/page.tsx` still serves `/` and
 * `(console)/audit/page.tsx` still serves `/audit`.
 *
 * It exists so the application chrome - the navigation, the theme toggle, the
 * "reduced credentials" banner - wraps the merchant's screens and nothing else.
 * The customer-facing `/checkout/[caseId]` page deliberately sits outside this
 * group.
 *
 * That is a product decision, not a cosmetic one. The person on the checkout
 * page is the *customer* completing a payment, not the merchant operating the
 * system. Showing them a nav bar linking to the merchant's dashboard, audit
 * ledger and guardrail configuration would be confusing at best, and at worst it
 * invites a customer to click into an operator's console from a link that
 * arrived in a payment email. A payment page should show the payment and nothing
 * else.
 *
 * The alternative was to keep one root layout and have the shell hide itself on
 * `/checkout` via `usePathname()`. It was rejected because it makes the shell
 * responsible for knowing about a route it has nothing to do with, and every
 * future customer-facing page would have to remember to add itself to that list.
 * A route group makes the boundary structural: a page is inside the console or
 * it is not.
 */

import type { ReactNode } from "react";

import { AppShell } from "@/components/layout/app-shell";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
