"use client";
import { useState, useCallback, useEffect } from "react";
import { api, FlowEpisode } from "@/lib/api";

export interface FlowEpisodesFilters {
  ticker?:        string;
  direction?:     string;
  contract_type?: string;
  alert_level?:   string;
  limit?:         number;
  offset?:        number;
}

export function useFlowEpisodes(
  token:   string | null,
  filters: FlowEpisodesFilters = {},
) {
  const [episodes, setEpisodes] = useState<FlowEpisode[]>([]);
  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState<string | null>(null);

  const fetch = useCallback(async () => {
    if (!token) return;
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
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, JSON.stringify(filters)]);

  // 30-second auto-refresh; re-triggers when token or filters change
  useEffect(() => {
    if (!token) return;
    fetch();
    const iv = setInterval(fetch, 30_000);
    return () => clearInterval(iv);
  }, [fetch, token]);

  return { episodes, loading, error, fetch };
}
