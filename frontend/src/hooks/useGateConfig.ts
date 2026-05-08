/**
 * useGateConfig.ts — Fetches the live gate configuration from the backend.
 * ADMIN-UI-001 | Chunk 2
 *
 * GET /api/admin/gate-config → GateConfigResponse
 *
 * Polling: re-fetches every 30 seconds so the UI stays in sync if another
 * admin changes a gate value. Poll is paused while the document is hidden
 * (tab switch / minimised window) to avoid hammering the backend.
 *
 * AbortController: every in-flight fetch is aborted on unmount or when the
 * token changes, preventing state updates on unmounted components.
 *
 * The caller can trigger a manual refresh via the returned `refresh()`
 * function (e.g. after a successful PATCH).
 */
"use client";
import { useState, useEffect, useCallback, useRef } from "react";
import type { GateConfigResponse } from "@/types/gates";

export const GATE_CONFIG_URL = "/api/admin/gate-config";

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

  const timerRef      = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef      = useRef<AbortController | null>(null);
  // Increment to trigger a manual refresh without recreating the callback
  const refreshTick   = useRef<number>(0);
  const [tick, setTick] = useState<number>(0);

  const fetchConfig = useCallback(async () => {
    if (!token) return;

    // Abort any previous in-flight request
    if (abortRef.current) abortRef.current.abort();
    const controller  = new AbortController();
    abortRef.current  = controller;

    // Set loading synchronously so there is no ghost-frame where
    // loading=false, data=null, error=null all coexist.
    setLoading(true);
    setError(null);

    try {
      const res = await fetch(GATE_CONFIG_URL, {
        headers: { Authorization: `Bearer ${token}` },
        signal:  controller.signal,
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
      // Ignore abort errors — they are intentional on unmount / token change
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(e instanceof Error ? e.message : "Network error");
    } finally {
      // Only clear loading if this controller was not aborted
      if (!controller.signal.aborted) {
        setLoading(false);
      }
    }
  }, [token]);

  // Visibility-aware polling
  useEffect(() => {
    if (!token) return;

    fetchConfig();

    function startPolling() {
      if (timerRef.current !== null) clearInterval(timerRef.current);
      timerRef.current = setInterval(() => {
        if (document.visibilityState === "visible") fetchConfig();
      }, POLL_INTERVAL_MS);
    }

    function handleVisibility() {
      if (document.visibilityState === "visible") fetchConfig();
    }

    startPolling();
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      if (timerRef.current !== null) clearInterval(timerRef.current);
      document.removeEventListener("visibilitychange", handleVisibility);
      // Abort any pending fetch on unmount
      if (abortRef.current) abortRef.current.abort();
    };
  }, [fetchConfig, token]);

  // Manual refresh: bump tick → triggers the effect above via `tick` dep.
  // We keep fetchConfig stable so bumping a counter is cleaner than calling
  // fetchConfig directly from the returned ref (avoids stale-closure risk).
  const refresh = useCallback(() => {
    refreshTick.current += 1;
    setTick(t => t + 1);
  }, []);

  // When tick changes (manual refresh), call fetchConfig immediately
  useEffect(() => {
    if (tick === 0) return; // skip initial mount — handled by the main effect
    fetchConfig();
  }, [tick, fetchConfig]);

  return { data, loading, error, refresh };
}
