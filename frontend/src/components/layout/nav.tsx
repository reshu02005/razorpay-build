'use client';

/**
 * Primary navigation.
 *
 * Only three entries, and that is deliberate. The console has six routes, but
 * three of them (`/payments/[id]`, `/recovery/[id]`, `/checkout/[id]`) are
 * detail screens for one record - there is no meaningful "all payments" or "all
 * cases" index behind them. A nav item pointing at `/recovery` would 404, and a
 * nav that can send you nowhere is worse than a short one. Those screens are
 * reached from the dashboard's tables, which is also how an operator actually
 * arrives at them: via the thing that needs attention, not via a menu.
 *
 * Client component because active-state highlighting reads the current route.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutDashboard, ScrollText, ShieldCheck, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

const NAV_ITEMS: readonly NavItem[] = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/audit", label: "Audit ledger", icon: ScrollText },
  { href: "/policy", label: "Guardrails", icon: ShieldCheck },
];

/**
 * The dashboard is only active on an exact match; every other section is active
 * for its whole subtree. Without the exact check, "/" would light up on every
 * single route, since every path starts with a slash.
 */
function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function Nav({ className }: { className?: string }) {
  const pathname = usePathname();

  return (
    <nav aria-label="Primary" className={cn("flex items-center", className)}>
      {/* Horizontal scroll rather than a hamburger: three items always fit on a
          phone once they collapse to icons, and a menu that has to be opened is
          one more tap between an operator and the queue. */}
      <ul className="flex items-center gap-1 overflow-x-auto">
        {NAV_ITEMS.map((item) => {
          const active = isActive(pathname, item.href);
          const Icon = item.icon;

          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex items-center gap-2 whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                  active
                    ? "bg-muted text-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                {/* The label is hidden, not removed, on the narrowest screens so
                    the accessible name of the link never disappears. */}
                <span className="hidden sm:inline">{item.label}</span>
                <span className="sr-only sm:hidden">{item.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
