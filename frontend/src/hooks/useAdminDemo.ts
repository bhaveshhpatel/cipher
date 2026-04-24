"use client";
import { useState, useEffect, useCallback } from "react";

export interface DemoStats {
  running:           boolean;
  ticks_emitted:     number;
  signals_generated: number;
  last_ticker:       string | null;
  started_at:        string | null;
}

export interface DemoStatus {
  demo:   DemoStats;
  stream: Record<string, unknown>;
  admin:  string;
}

export function useAdminDemo(token: string | null) {
  const [status,  setStatus]  = useState<DemoStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);

  const API = process.env.NEXT_PUBLIC_API_URL ?? "";

  const fetchStatus = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch(`${API}/api/admin/demo/status`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(await res.text());
      setStatus(await res.json());
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [token, API]);

  const toggle = useCallback(async (on: boolean) => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const endpoint = on ? "on" : "off";
      const res = await fetch(`${API}/api/admin/demo/${endpoint}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(await res.text());
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [token, API, fetchStatus]);

  // Poll status every 3s while mounted
  useEffect(() => {
    fetchStatus();
    const iv = setInterval(fetchStatus, 3000);
    return () => clearInterval(iv);
  }, [fetchStatus]);

  return { status, loading, error, toggle, refresh: fetchStatus };
}
