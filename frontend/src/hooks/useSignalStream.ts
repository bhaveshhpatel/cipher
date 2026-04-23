"use client";
import { useState, useEffect, useRef, useCallback } from "react";

export interface WsSignal {
  ticker: string; direction: string; contract_type?: string; strike?: number;
  expiry?: string; total_premium?: number; trade_count?: number; alert_level: string;
  is_accelerating?: boolean; seed_episode?: string; timestamp: string;
}

/**
 * Derives the WebSocket URL from the current page origin so it always
 * points at the right host regardless of environment:
 *
 *   localhost:3000 (dev)  → ws://localhost:8000/ws/signals
 *   cipher.vercel.app     → wss://cipher-production-xxxx.up.railway.app/ws/signals
 *
 * NEXT_PUBLIC_WS_URL must be set in Vercel env vars to the Railway wss:// URL.
 * Falls back gracefully to localhost for local dev.
 */
function getWsBase(): string {
  // Server-side render guard
  if (typeof window === "undefined") return "ws://localhost:8000";

  const explicit = process.env.NEXT_PUBLIC_WS_URL;
  if (explicit) return explicit.replace(/\/+$/, "");

  // Derive from current page: http→ws, https→wss, same host
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const host  = window.location.host;
  // In local dev the Next.js dev server is on 3000 but backend is on 8000
  const wsHost = host.includes("localhost") || host.includes("127.0.0.1")
    ? host.replace(/:.*/, ":8000")
    : host;
  return `${proto}://${wsHost}`;
}

export function useSignalStream(token: string | null) {
  const [signals,   setSignals]   = useState<WsSignal[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef        = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    if (!token) return;
    let active = true;
    const base  = getWsBase();
    const wsUrl = `${base}/ws/signals?token=${token}`;

    const connect = () => {
      if (!active) return;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen  = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (active) reconnectRef.current = setTimeout(connect, 3000);
      };
      ws.onerror = () => setConnected(false);
      ws.onmessage = (e) => {
        try {
          const m = JSON.parse(e.data as string);
          if (m.type === "ping") return;
          const payload = m.type === "signal" && m.data ? m.data : m;
          if (payload?.ticker && payload?.alert_level) {
            setSignals(p => [payload, ...p].slice(0, 200));
          }
        } catch { /* ignore malformed frames */ }
      };
    };

    connect();
    return () => {
      active = false;
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [token]);

  const clear = useCallback(() => setSignals([]), []);
  return { signals, connected, clear };
}
