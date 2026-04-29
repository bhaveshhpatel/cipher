"use client";
import { useState, useCallback, useRef, useEffect } from "react";
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

export function useFlowEvents(token: string | null) {
  const [events,  setEvents]  = useState<FlowEventRaw[]>([]);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);

  // Keep a ref to the latest filters so the interval closure always uses fresh values
  const filtersRef = useRef<FlowEventsFilters>({});

  const fetch = useCallback(async (filters: FlowEventsFilters = {}) => {
    if (!token) return;
    filtersRef.current = filters;
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
  }, [token]);

  // 10-second auto-refresh
  useEffect(() => {
    if (!token) return;
    fetch(filtersRef.current);
    const iv = setInterval(() => fetch(filtersRef.current), 10_000);
    return () => clearInterval(iv);
  }, [token, fetch]);

  return { events, loading, error, fetch };
}
