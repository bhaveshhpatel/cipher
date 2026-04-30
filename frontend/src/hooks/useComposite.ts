/**
 * useComposite — SWR hook for composite signal data.
 *
 * Fetches /api/flow/composite with optional symbol filter.
 * Polls on configurable interval (default 30s during market hours).
 */
import useSWR from "swr";
import type { CompositeSignal } from "@/types";

const DEFAULT_REFRESH_MS = 30_000;

async function fetcher<T>(url: string): Promise<T> {
  const token = typeof window !== "undefined"
    ? localStorage.getItem("cipher_token")
    : null;

  const res = await fetch(url, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });

  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`) as Error & { status: number };
    err.status = res.status;
    throw err;
  }

  return res.json() as Promise<T>;
}

export interface UseCompositeOptions {
  symbol?:          string;
  refreshInterval?: number;
  paused?:          boolean;
}

export interface UseCompositeReturn {
  signal:       CompositeSignal | null;
  isLoading:    boolean;
  isValidating: boolean;
  error:        Error | null;
  refresh:      () => void;
}

export function useComposite({
  symbol,
  refreshInterval = DEFAULT_REFRESH_MS,
  paused          = false,
}: UseCompositeOptions = {}): UseCompositeReturn {
  const params = new URLSearchParams();
  if (symbol) params.set("symbol", symbol.toUpperCase());

  const key = paused ? null : `/api/proxy/flow/composite?${params}`;

  const { data, error, isLoading, isValidating, mutate } =
    useSWR<CompositeSignal>(key, fetcher, {
      refreshInterval,
      revalidateOnFocus:     false,
      revalidateOnReconnect: true,
      keepPreviousData:      true,
    });

  return {
    signal:       data ?? null,
    isLoading,
    isValidating,
    error:        error ?? null,
    refresh:      () => mutate(),
  };
}
