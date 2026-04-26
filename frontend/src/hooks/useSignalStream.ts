"use client";
import { useState, useEffect, useRef, useCallback } from "react";

export interface WsSignal {
  ticker: string; direction: string; contract_type?: string; strike?: number;
  expiry?: string; total_premium?: number; trade_count?: number; alert_level: string;
  is_accelerating?: boolean; seed_episode?: string; timestamp: string;
  conviction_score?: number;
}

/**
 * Derives the WebSocket URL from env vars or current page origin.
 *
 * Priority:
 *   1. NEXT_PUBLIC_WS_URL env var (set in Vercel to wss://...railway.app)
 *   2. Derive from window.location (works for local dev)
 *
 * Returns null if a valid URL cannot be determined — the hook will
 * skip connecting rather than crash the page.
 */
function getWsUrl(token: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    const explicit = process.env.NEXT_PUBLIC_WS_URL;
    if (explicit && explicit.trim() !== "") {
      const base = explicit.replace(/\/+$/, "");
      return `${base}/ws/signals?token=${token}`;
    }
    const proto  = window.location.protocol === "https:" ? "wss" : "ws";
    const host   = window.location.host;
    const wsHost = (host.includes("localhost") || host.includes("127.0.0.1"))
      ? host.replace(/:.*/, ":8000")
      : host;
    return `${proto}://${wsHost}/ws/signals?token=${token}`;
  } catch {
    return null;
  }
}

export function useSignalStream(token: string | null) {
  const [signals,   setSignals]   = useState<WsSignal[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef        = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    if (!token) return;

    const wsUrl = getWsUrl(token);
    if (!wsUrl) {
      console.warn("[useSignalStream] Could not determine WebSocket URL — NEXT_PUBLIC_WS_URL may not be set.");
      return;
    }

    let active = true;

    const connect = () => {
      if (!active) return;
      try {
        const ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        ws.onopen  = () => { if (active) setConnected(true); };
        ws.onclose = () => {
          setConnected(false);
          if (active) reconnectRef.current = setTimeout(connect, 3000);
        };
        ws.onerror = () => setConnected(false);
        ws.onmessage = (e) => {
          try {
            const m = JSON.parse(e.data as string);

            // Phase 4: respond to server keepalive pings
            if (m.type === "ping") {
              if (ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: "pong" }));
              }
              return;
            }

            const payload = m.type === "signal" && m.data ? m.data : m;
            if (payload?.ticker && payload?.alert_level) {
              setSignals(p => [payload, ...p].slice(0, 200));
            }
          } catch { /* ignore malformed frames */ }
        };
      } catch (err) {
        console.error("[useSignalStream] WebSocket construction failed:", err);
      }
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
