/**
 * Route-level loading UI for the dashboard.
 *
 * This covers a different window from the skeleton inside `page.tsx`. That one
 * appears once the page component is running and is waiting on the API; this
 * one appears before that, while Next is still fetching the route's JavaScript
 * chunk - a real, visible gap on a cold load or a slow disk, and the moment
 * that decides whether the app feels instant or blank.
 *
 * It is a server component and deliberately holds no data: rendering it must
 * never be able to fail, or the loading state would need a loading state.
 *
 * The shape mirrors the dashboard - a header, six tiles, a gauge, two panels
 * and a table - so the page does not reflow as each stage takes over. A spinner
 * would be less work and would make the whole load feel like one long pause.
 */

import { Skeleton } from "@/components/ui/skeleton";

export default function DashboardLoading() {
  return (
    <div className="space-y-6" aria-busy="true">
      <span className="sr-only">Loading the recovery console</span>

      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2">
          <Skeleton className="h-6 w-52" />
          <Skeleton className="h-4 w-80" />
        </div>
        <div className="flex gap-2">
          <Skeleton className="h-9 w-28" />
          <Skeleton className="h-9 w-56" />
        </div>
      </div>

      <Skeleton className="h-[4.5rem] w-full" />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {Array.from({ length: 6 }, (_unused, index) => (
          <Skeleton key={index} className="h-[6.5rem] w-full" />
        ))}
      </div>

      <Skeleton className="h-[9rem] w-full" />

      <div className="grid gap-4 lg:grid-cols-2">
        <Skeleton className="h-80 w-full" />
        <Skeleton className="h-80 w-full" />
      </div>

      <Skeleton className="h-96 w-full" />
    </div>
  );
}
