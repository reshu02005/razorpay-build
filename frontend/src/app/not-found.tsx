/**
 * 404.
 *
 * The console's deep routes are all keyed by an id - `/payments/{id}`,
 * `/recovery/{id}`, `/checkout/{id}` - so the realistic way to land here is a
 * stale link or an id that was typed by hand, not a broken navigation. That
 * makes the useful response a list of the places that always exist, rather than
 * a large "404" and a back button that returns to wherever the bad link was.
 *
 * A server component: it has no state and reads nothing, so none of it needs to
 * ship to the browser.
 */

import Link from "next/link";
import { Compass } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * The button's own class recipe, spelled out rather than borrowed.
 *
 * `buttonVariants` lives in a `'use client'` module, and a server component may
 * render a client component but may not *call* a function exported from one -
 * the function does not exist on the server at all. Rather than turn this whole
 * page into a client component to style one link, the outline variant is
 * written here. It is three utility groups and it keeps the 404 free of any
 * client JavaScript.
 */
const OUTLINE_LINK = cn(
  "inline-flex h-9 items-center justify-center gap-2 rounded-lg border border-border px-4",
  "bg-background text-sm font-medium text-foreground transition-colors hover:bg-muted",
);

const DESTINATIONS: readonly { href: string; label: string; description: string }[] = [
  {
    href: "/",
    label: "Dashboard",
    description: "Failed payments, the recovery queue and the day's budget.",
  },
  {
    href: "/audit",
    label: "Audit ledger",
    description: "Every recorded action, with live hash-chain verification.",
  },
  {
    href: "/policy",
    label: "Guardrails",
    description: "The active limits and the thirteen rules, read-only.",
  },
];

export default function NotFound() {
  return (
    <Card className="mx-auto max-w-xl">
      <CardHeader>
        <div className="flex items-center gap-3">
          <span
            aria-hidden="true"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground"
          >
            <Compass className="h-5 w-5" />
          </span>
          <div>
            <CardTitle className="text-base">Nothing at this address</CardTitle>
            <CardDescription>
              The page does not exist, or the payment or case id in the URL is not one this
              database holds.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
          {DESTINATIONS.map((destination) => (
            <li key={destination.href}>
              <Link
                href={destination.href}
                className="block px-4 py-3 transition-colors hover:bg-muted"
              >
                <span className="text-sm font-medium">{destination.label}</span>
                <span className="mt-0.5 block text-xs text-muted-foreground">
                  {destination.description}
                </span>
              </Link>
            </li>
          ))}
        </ul>

        <Link href="/" className={OUTLINE_LINK}>
          Back to the dashboard
        </Link>
      </CardContent>
    </Card>
  );
}
