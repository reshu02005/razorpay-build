'use client';

/**
 * Route error boundary.
 *
 * This screen exists to end a debugging session, not to apologise. By far the
 * most likely reason anyone sees it is the one that has nothing to do with the
 * frontend at all: the Next.js dev server is running and the FastAPI backend is
 * not, so every request fails at the network layer. A generic "Something went
 * wrong. Try again." would leave a reviewer clicking a button that cannot
 * possibly work, and the honest difference between that and this page is
 * whether they spend ten minutes on it or ten seconds.
 *
 * So the page names three things it can actually be specific about:
 *
 *   - the exact URL the browser tried to reach, resolved the same way the API
 *     client resolves it, including the fallback used when `.env.local` was
 *     never copied;
 *   - the command that starts the missing process, on both Windows and
 *     macOS/Linux;
 *   - the raw error message, verbatim, in monospace.
 *
 * `reset()` re-renders the route subtree, which remounts the dashboard and
 * re-runs its fetch - so it is a genuine retry once the backend is up, not a
 * decorative button.
 */

import { RefreshCw, ServerCrash, Terminal } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { API_BASE_URL } from "@/lib/api";

export default function DashboardError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <Card className="mx-auto max-w-2xl">
      <CardHeader>
        <div className="flex items-center gap-3">
          <span
            aria-hidden="true"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-danger-subtle text-danger-strong"
          >
            <ServerCrash className="h-5 w-5" />
          </span>
          <div>
            <CardTitle className="text-base">This screen could not load its data</CardTitle>
            <CardDescription>
              Almost always because the backend is not running, or is running somewhere other than
              where this build expects it.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-5">
        <section className="space-y-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            What the browser tried to reach
          </h2>
          <code className="block break-all rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs">
            {API_BASE_URL}/api/…
          </code>
          <p className="text-xs text-muted-foreground">
            Set by <code className="font-mono">NEXT_PUBLIC_API_BASE_URL</code> in{" "}
            <code className="font-mono">frontend/.env.local</code>. That value is inlined at build
            time, so changing it means restarting the dev server, not just reloading the page.
          </p>
        </section>

        <section className="space-y-2">
          <h2 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            <Terminal className="h-3.5 w-3.5" aria-hidden="true" />
            Start the backend
          </h2>
          <pre className="overflow-x-auto rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs leading-relaxed">
            {[
              "# macOS / Linux, from the project root",
              "python dev.py backend",
              "",
              "# Windows",
              "dev.bat backend",
              "",
              "# or run the backend and this frontend together",
              "python dev.py start",
            ].join("\n")}
          </pre>
          <p className="text-xs text-muted-foreground">
            The API answers on <code className="font-mono">http://127.0.0.1:8000/api/health</code>{" "}
            once it is up. If it is running but this page still fails, check that the frontend
            origin is listed in <code className="font-mono">CORS_ORIGINS</code> in{" "}
            <code className="font-mono">backend/.env</code>.
          </p>
        </section>

        <section className="space-y-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Reported error
          </h2>
          {/* Verbatim, untruncated. A paraphrased error message is a message that
              can no longer be searched for. */}
          <p className="break-words rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs">
            {error.message || "No message was attached to the error."}
          </p>
          {error.digest ? (
            <p className="text-xs text-muted-foreground">
              Digest <code className="font-mono">{error.digest}</code> - use this to find the
              matching entry in the server log.
            </p>
          ) : null}
        </section>

        <Button onClick={reset} leadingIcon={<RefreshCw className="h-4 w-4" />}>
          Try again
        </Button>
      </CardContent>
    </Card>
  );
}
