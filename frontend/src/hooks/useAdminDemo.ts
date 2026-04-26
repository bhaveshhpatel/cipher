"use client";
import { useState, useEffect, useCallback, useRef } from "react";

// Always use relative URLs so requests go through the Next.js proxy.
// Direct use of NEXT_PUBLIC_API_URL bypasses the proxy and causes CORS
// preflight failures in production.

export interface DemoStats {
  running:           boolean;
  ticks_emitted:     number;
  signals_generated: number;
  last_ticker:       string | null;
  started_at:        string | null;
}

// Actual shape returned by GET /api/admin/demo/status
export interface DemoStatus {
  demo:  DemoStats;
  admin: string;
  role:  string;
}

export function useAdminDemo(token: string | null) {
  const [status,  setStatus]  = useState<DemoStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchStatus = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch("/api/admin/demo/status", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        const text = await res.text();
        setError(`Status fetch failed: ${text}`);
        return;
      }
      const data: DemoStatus = await res.json();
      setStatus(data);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [token]);

  const toggle = useCallback(async (on: boolean) => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const endpoint = on ? "on" : "off";
      const res = await fetch(`/api/admin/demo/${endpoint}`, {
        method:  "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`Demo ${endpoint} failed: ${text}`);
      }
      // Refresh status immediately after toggle
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [token, fetchStatus]);

  // Poll status every 3s while mounted
  useEffect(() => {
    if (!token) return;
    fetchStatus();
    pollingRef.current = setInterval(fetchStatus, 3000);
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [fetchStatus, token]);

  // Derive running state directly from fetched status
  const isRunning = status?.demo?.running ?? false;

  return { status, isRunning, loading, error, toggle, refresh: fetchStatus };
}
