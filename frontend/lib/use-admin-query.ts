"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError } from "./admin-api";

export type QueryState<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "success"; data: T };

/**
 * Checkpoint 07 §27: every list/detail page needs the same
 * loading/error/success handling. Centralized here instead of
 * duplicated per page. A 401 (session expired) redirects to /login --
 * the backend's own 401 is what's authoritative (§4); this is just the
 * UI reacting to it.
 */
export function useAdminQuery<T>(
  fetcher: () => Promise<T>,
  deps: React.DependencyList
): QueryState<T> & { refetch: () => void } {
  const [state, setState] = useState<QueryState<T>>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const router = useRouter();

  const load = useCallback(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetcher()
      .then((data) => {
        if (!cancelled) setState({ status: "success", data });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          router.push("/login");
          return;
        }
        const message = err instanceof ApiError ? err.message : "Something went wrong.";
        setState({ status: "error", message });
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => load(), [load, tick]);

  return { ...state, refetch: () => setTick((t) => t + 1) };
}
