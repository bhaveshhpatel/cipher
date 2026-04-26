"use client";
import { useState, useCallback } from "react";
import { api, FlowEvent } from "@/lib/api";

export function useFlow(token: string | null) {
  const [events,  setEvents]  = useState<FlowEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);

  // ticker = "" → fetch all tickers (no filter)
  const fetch = useCallback(async (ticker: string, limit = 100, offset = 0) => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const d = await api.getFlow(ticker, token, limit, offset);
      setEvents(d.events ?? []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load flow");
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, [token]);

  return { events, loading, error, fetch };
}
