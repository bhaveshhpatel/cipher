/**
 * useMarketStatus — SWR hook for current market session.
 *
 * Polls /api/proxy/market/status every 60s.
 * Also computes a derived `isOpen` boolean for gate-keeping.
 */
import useSWR from "swr";
import type { MarketStatus } from "@/types";

const REFRESH_MS = 60_000;

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

interface MarketStatusResponse {
  status:      MarketStatus;
  next_change: string | null; // ISO timestamp
  session:     string;
}

export interface UseMarketStatusReturn {
  status:       MarketStatus | null;
  nextChange:   string | null;
  session:      string | null;
  isOpen:       boolean;
  isLoading:    boolean;
  error:        Error | null;
  refresh:      () => void;
}

export function useMarketStatus(): UseMarketStatusReturn {
  const { data, error, isLoading, mutate } =
    useSWR<MarketStatusResponse>("/api/proxy/market/status", fetcher, {
      refreshInterval:       REFRESH_MS,
      revalidateOnFocus:     false,
      revalidateOnReconnect: true,
    });

  return {
    status:     data?.status     ?? null,
    nextChange: data?.next_change ?? null,
    session:    data?.session     ?? null,
    isOpen:     data?.status === "open",
    isLoading,
    error:      error ?? null,
    refresh:    () => mutate(),
  };
}
