/**
 * useFlow — SWR hook for paginated flow events.
 *
 * Fetches /api/proxy/flow/events with optional symbol + page filters.
 * Returns FlowEventRaw[] (raw per-trade rows with id, tier, fill_price, etc.)
 * Exposes helpers: loadMore, refresh, isEmpty, hasMore.
 *
 * Endpoint note: this hook fetches /api/proxy/flow/events which the proxy
 * layer forwards to the backend's /api/flow/events (FlowEventsResponse shape).
 * This is intentionally distinct from the legacy /api/flow/scan endpoint
 * used by api.getFlow() — that endpoint returns aggregated FlowEvent[] rows
 * and is retained only for the simulation tab's imperative fetch pattern.
 */
import useSWRInfinite from "swr/infinite";
import type { FlowEventsResponse, FlowEventRaw } from "@/types";

const FLOW_PAGE_SIZE = 50;

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

export interface UseFlowOptions {
  symbol?:          string;
  pageSize?:        number;
  /** Poll interval in ms. 0 = no polling. */
  refreshInterval?: number;
  paused?:          boolean;
}

export interface UseFlowReturn {
  events:       FlowEventRaw[];
  isLoading:    boolean;
  isValidating: boolean;
  error:        Error | null;
  isEmpty:      boolean;
  hasMore:      boolean;
  loadMore:     () => void;
  refresh:      () => void;
  page:         number;
}

export function useFlow({
  symbol,
  pageSize        = FLOW_PAGE_SIZE,
  refreshInterval = 0,
  paused          = false,
}: UseFlowOptions = {}): UseFlowReturn {
  const getKey = (pageIndex: number, prev: FlowEventsResponse | null) => {
    if (paused) return null;
    if (prev && prev.events.length < pageSize) return null;
    const params = new URLSearchParams({ limit: String(pageSize), offset: String(pageIndex * pageSize) });
    if (symbol) params.set("ticker", symbol.toUpperCase());
    return `/api/proxy/flow/events?${params}`;
  };

  const { data, error, isLoading, isValidating, setSize, size, mutate } =
    useSWRInfinite<FlowEventsResponse>(getKey, fetcher, {
      refreshInterval,
      revalidateOnFocus:     false,
      revalidateOnReconnect: true,
      keepPreviousData:      true,
      // Prevents SWR from re-fetching page 1 when loadMore increments size.
      // Without this, the page-1 re-fetch consumes the page-2 mock in tests
      // and causes stale-data issues in production infinite-scroll feeds.
      revalidateFirstPage:   false,
    });

  const events   = data ? data.flatMap(p => p.events) : [];
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
