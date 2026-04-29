"use client";
import { useState, useCallback, useEffect } from "react";
import { api, FlowEventRaw } from "@/lib/api";

export interface FlowEventsFilters {
  ticker?:        string;
  sentiment?:     string;
  contract_type?: string;
  tier?:          string;
  aggressive?:    boolean;
  golden_sweep?:  boolean;
  limit?:         number;
  offset?:        number;
}

export function useFlowEvents(
  token:   string | null,
  filters: FlowEventsFilters = {},
) {
  const [events,  setEvents]  = useState<FlowEventRaw[]>([]);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);

  const fetch = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const d = await api.getFlowEvents(token, { limit: 50, ...filters });
      setEvents(d.events ?? []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load flow events");
      setEvents([]);
    } finally {
      setLoading(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, JSON.stringify(filters)]);

  // 10-second auto-refresh; re-triggers when token or filters change
  useEffect(() => {
    if (!token) return;
    fetch();
    const iv = setInterval(fetch, 10_000);
    return () => clearInterval(iv);
  }, [fetch, token]);

  return { events, loading, error, fetch };
}
