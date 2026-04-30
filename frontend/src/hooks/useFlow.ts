/**
 * useFlow — SWR hook for paginated flow events.
 *
 * Fetches /api/flow/events with optional symbol + page filters.
 * Exposes helpers: loadMore, refresh, isEmpty.
 */
import useSWR from "swr";
import useSWRInfinite from "swr/infinite";
import type { FlowEvent, PaginatedResponse } from "@/types";

const FLOW_PAGE_SIZE = 50;

// ── Fetcher ────────────────────────────────────────────
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

// ── Types ─────────────────────────────────────────────
export interface UseFlowOptions {
  symbol?:      string;
  pageSize?:    number;
  /** Poll interval in ms. 0 = no polling. */
  refreshInterval?: number;
  paused?:      boolean;
}

export interface UseFlowReturn {
  events:      FlowEvent[];
  isLoading:   boolean;
  isValidating: boolean;
  error:       Error | null;
  isEmpty:     boolean;
  hasMore:     boolean;
  loadMore:    () => void;
  refresh:     () => void;
  page:        number;
}

// ── Hook ──────────────────────────────────────────────
export function useFlow({
  symbol,
  pageSize        = FLOW_PAGE_SIZE,
  refreshInterval = 0,
  paused          = false,
}: UseFlowOptions = {}): UseFlowReturn {
  const getKey = (pageIndex: number, prev: PaginatedResponse<FlowEvent> | null) => {
    if (paused) return null;
    if (prev && prev.events.length < pageSize) return null; // no more pages
    const params = new URLSearchParams({ page: String(pageIndex + 1), limit: String(pageSize) });
    if (symbol) params.set("symbol", symbol.toUpperCase());
    return `/api/proxy/flow/events?${params}`;
  };

  const { data, error, isLoading, isValidating, setSize, size, mutate } =
    useSWRInfinite<PaginatedResponse<FlowEvent>>(getKey, fetcher, {
      refreshInterval,
      revalidateOnFocus:  false,
      revalidateOnReconnect: true,
      keepPreviousData:   true,
    });

  const events  = data ? data.flatMap(p => p.events) : [];
  const lastPage = data?.[data.length - 1];
  const hasMore  = lastPage ? lastPage.events.length >= pageSize : false;

  return {
    events,
    isLoading,
    isValidating,
    error:    error ?? null,
    isEmpty:  !isLoading && events.length === 0,
    hasMore,
    loadMore: () => setSize(size + 1),
    refresh:  () => mutate(),
    page:     size,
  };
}
