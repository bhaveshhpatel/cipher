"use client";
import { useState, useCallback } from "react";
import { api, SignalHistoryItem } from "@/lib/api";

export interface HistoryFilters {
  ticker?:         string;
  direction?:      string;   // bullish | bearish | neutral | ""
  tier?:           string;   // whale | institutional | large | retail | ""
  min_conviction?: number;
}

export interface UseSignalHistoryReturn {
  items:    SignalHistoryItem[];
  total:    number;
  loading:  boolean;
  error:    string | null;
  page:     number;
  pageSize: number;
  fetch:    (filters?: HistoryFilters, page?: number) => Promise<void>;
  setPage:  (p: number) => void;
}

const PAGE_SIZE = 50;

export function useSignalHistory(token: string | null): UseSignalHistoryReturn {
  const [items,   setItems]   = useState<SignalHistoryItem[]>([]);
  const [total,   setTotal]   = useState(0);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);
  const [page,    setPageState] = useState(1);

  const fetch = useCallback(async (filters: HistoryFilters = {}, p = 1) => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const params: Parameters<typeof api.getSignalHistory>[1] = {
        limit:  PAGE_SIZE,
        offset: (p - 1) * PAGE_SIZE,
      };
      if (filters.ticker)                               params.ticker         = filters.ticker.toUpperCase();
      if (filters.direction && filters.direction !== "") params.direction      = filters.direction;
      if (filters.tier      && filters.tier      !== "") params.tier           = filters.tier;
      if (filters.min_conviction !== undefined)          params.min_conviction = filters.min_conviction;

      const res = await api.getSignalHistory(token, params);
      setItems(res.signals);
      setTotal(res.total);
      setPageState(p);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load signal history");
    } finally {
      setLoading(false);
    }
  }, [token]);

  const setPage = useCallback((p: number) => setPageState(p), []);

  return { items, total, loading, error, page, pageSize: PAGE_SIZE, fetch, setPage };
}
