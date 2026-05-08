/**
 * useGateConfig.ts — Fetches the live gate configuration from the backend.
 * ADMIN-UI-001 | Chunk 2
 *
 * GET /api/admin/gate-config → GateConfigResponse
 *
 * Polling: re-fetches every 30 seconds so the UI stays in sync if another
 * admin changes a gate value. The caller can also trigger a manual refresh
 * via the returned `refresh()` function (e.g. after a successful PATCH).
 */
"use client";
import { useState, useEffect, useCallback, useRef } from "react";
import type { GateConfigResponse } from "@/types/gates";

const POLL_INTERVAL_MS = 30_000;

export interface UseGateConfigReturn {
  data:     GateConfigResponse | null;
  loading:  boolean;
  error:    string | null;
  refresh:  () => void;
}

export function useGateConfig(token: string | null): UseGateConfigReturn {
  const [data,    setData]    = useState<GateConfigResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error,   setError]   = useState<string | null>(null);
  const timerRef              = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchConfig = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/admin/gate-config", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 401) {
        setError("Unauthorized — admin role required.");
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError((body as { detail?: string }).detail ?? `HTTP ${res.status}`);
        return;
      }
      const json: GateConfigResponse = await res.json();
      setData(json);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Network error");
    } finally {
      setLoading(false);
    }
  }, [token]);

  // Initial fetch + poll
  useEffect(() => {
    fetchConfig();

    timerRef.current = setInterval(fetchConfig, POLL_INTERVAL_MS);
    return () => {
      if (timerRef.current !== null) clearInterval(timerRef.current);
    };
  }, [fetchConfig]);

  return { data, loading, error, refresh: fetchConfig };
}
