"use client";

/**
 * The console's data-fetching hook.
 *
 * No data-fetching library is installed, and for six screens over a local
 * FastAPI process none is warranted: there is no cross-screen cache to
 * invalidate, no optimistic mutation, no pagination cursor. What is actually
 * needed is a request tied to a component's lifetime, a `loading` flag, a
 * readable error, and a way to re-run after an approval. That is this file.
 *
 * Every fetch still goes through `@/lib/api` -- this hook only decides *when* a
 * request runs and what happens to the result.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { errorMessage } from "@/lib/utils";

export interface UseApiResult<T> {
  /**
   * The last successful response, or `null` before the first one lands.
   *
   * Deliberately preserved across refreshes and across failures: an operator
   * reading a case should not have the screen blank out because a poll blipped.
   * Render the initial skeleton on `data === null && loading`, and show `error`
   * as a banner above whatever `data` is still there.
   */
  data: T | null;
  /** Human-readable failure from the last attempt, cleared by the next success. */
  error: string | null;
  /** True while a request is in flight, including refreshes and polls. */
  loading: boolean;
  /** Re-runs the fetcher. Stable identity -- safe in a dependency array. */
  refresh: () => void;
}

/**
 * Runs `fetcher` on mount and whenever `deps` change.
 *
 * @param fetcher Usually an inline arrow such as `() => api.getCase(caseId)`.
 *   Its identity is intentionally *not* a dependency: an inline arrow is a new
 *   function on every render, so depending on it would re-fetch forever. The
 *   latest one is kept in a ref and `deps` is what decides when to re-run.
 * @param deps The values the request is derived from -- an id, a filter. Must
 *   keep a stable length between renders, as with any React dependency array.
 */
export function useApi<T>(fetcher: () => Promise<T>, deps: unknown[]): UseApiResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  // Bumping this re-runs the effect without changing `deps`, which is what
  // `refresh()` does after an approve/reject.
  const [reloadToken, setReloadToken] = useState(0);

  const fetcherRef = useRef(fetcher);
  // Declared before the fetching effect so it commits first: React runs effects
  // in declaration order, so by the time the request fires the ref already holds
  // the fetcher from the current render.
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  useEffect(() => {
    /*
     * The cancellation flag is what makes this hook safe in two situations that
     * are easy to get wrong:
     *
     *  - The component unmounts (or `deps` change) while a request is still in
     *    flight. Without the flag, the late resolution calls `setState` on a
     *    component that is gone, and a superseded response can overwrite the
     *    newer one it lost the race to.
     *  - React StrictMode, which is enabled in `next.config.mjs`, mounts every
     *    component twice in development. That makes the bug above reproducible
     *    on every single page load rather than occasionally, which is exactly
     *    why StrictMode is left on.
     */
    let cancelled = false;

    setLoading(true);

    fetcherRef
      .current()
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // `data` is left alone on purpose: a transient failure during a refresh
        // should surface as a banner, not wipe the screen being read.
        setError(errorMessage(err));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // `deps` is spread so the caller's values participate directly; `reloadToken`
    // is what `refresh()` moves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadToken, ...deps]);

  const refresh = useCallback(() => {
    setReloadToken((token) => token + 1);
  }, []);

  return { data, error, loading, refresh };
}

/**
 * `useApi` plus a repeating refresh on a timer.
 *
 * Used by the customer checkout screen, where the state that matters changes
 * *off-screen*: the customer pays in the Razorpay window (or the simulated
 * gateway resolves) and the case moves to `recovered` on the server with nothing
 * happening in this tab to trigger a re-render. Polling is the honest way to see
 * that with no websocket in the stack.
 *
 * Pass `intervalMs <= 0` to stop polling without changing hook order -- that is
 * how a screen stands down once its case reaches a terminal state, instead of
 * hammering the API forever.
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  deps: unknown[],
  intervalMs: number,
): UseApiResult<T> {
  const result = useApi(fetcher, deps);
  const { refresh } = result;

  useEffect(() => {
    if (intervalMs <= 0) return;

    const timer = setInterval(refresh, intervalMs);
    // Cleared on unmount and whenever the interval changes. A surviving timer
    // would keep calling `refresh` on an unmounted component -- the same class
    // of bug the cancellation flag above guards against, one level up.
    return () => clearInterval(timer);
  }, [refresh, intervalMs]);

  return result;
}
