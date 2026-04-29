"use client";
import { useState, useCallback, useRef, useEffect } from "react";
import { api, FlowEpisode } from "@/lib/api";

export interface FlowEpisodesFilters {
  ticker?:        string;
  direction?:     string;
  contract_type?: string;
  alert_level?:   string;
  limit?:         number;
  offset?:        number;
}

export function useFlowEpisodes(token: string | null) {
  const [episodes, setEpisodes] = useState<FlowEpisode[]>([]);
  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState<string | null>(null);

  const filtersRef = useRef<FlowEpisodesFilters>({});

  const fetch = useCallback(async (filters: FlowEpisodesFilters = {}) => {
    if (!token) return;
    filtersRef.current = filters;
    setLoading(true);
    setError(null);
    try {
      const d = await api.getFlowEpisodes(token, { limit: 50, ...filters });
      setEpisodes(d.episodes ?? []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load episodes");
      setEpisodes([]);
    } finally {
      setLoading(false);
    }
  }, [token]);

  // 30-second auto-refresh
  useEffect(() => {
    if (!token) return;
    fetch(filtersRef.current);
    const iv = setInterval(() => fetch(filtersRef.current), 30_000);
    return () => clearInterval(iv);
  }, [token, fetch]);

  return { episodes, loading, error, fetch };
}
