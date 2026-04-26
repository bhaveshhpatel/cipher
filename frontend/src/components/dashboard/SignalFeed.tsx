"use client";
import { useEffect, useState, useRef } from "react";
import { api } from "@/lib/api";
import type { WsSignal } from "@/hooks/useSignalStream";

const POLL_MS = 20_000; // fallback DB poll when WS has no data

const alertColor = (level: string) => {
  if (level === "CONVICTION")    return "var(--amber)";
  if (level === "STRONG_SIGNAL") return "var(--teal)";
  if (level === "ALERT")         return "var(--blue)";
  return "var(--muted)";
};
const alertBg = (level: string) => {
  if (level === "CONVICTION")    return "rgba(232,160,32,0.07)";
  if (level === "STRONG_SIGNAL") return "rgba(10,155,140,0.07)";
  if (level === "ALERT")         return "rgba(26,110,245,0.07)";
  return "transparent";
};
const directionColor = (d: string) => {
  if (d === "BUY")  return "var(--green)";
  if (d === "SELL") return "var(--red)";
  return "var(--muted)";
};
const fmt$ = (n: number) =>
  n >= 1_000_000 ? `$${(n / 1_000_000).toFixed(2)}M` : `$${(n / 1_000).toFixed(1)}K`;

function EmptyState({ connected, polling }: { connected: boolean; polling: boolean }) {
  return (
    <div className="card flex flex-col items-center justify-center py-20 gap-4">
      <span className="text-4xl" style={{ color: "var(--faint)" }}>◉</span>
      <p className="text-base font-semibold" style={{ color: "var(--muted)" }}>
        {connected ? "Waiting for live signals…" : polling ? "Polling DB for signals…" : "Connecting to stream…"}
      </p>
      <p className="text-sm" style={{ color: "var(--faint)" }}>
        Signals appear here as whale flow is detected in real-time.
      </p>
    </div>
  );
}

// Shape of a DB-backed signal_history row adapted to look like WsSignal
function dbRowToWsSignal(row: {
  ticker: string;
  direction: string | null;
  recommendation: string;
  composite_score: number;
  influence_tier: string | null;
  total_premium: number | null;
  trade_count: number | null;
  is_accelerating: boolean;
  created_at: string;
  contract_type: string | null;
}): WsSignal {
  const dir = row.direction?.toUpperCase() ?? row.recommendation;
  return {
    ticker:           row.ticker,
    direction:        dir === "BUY" || dir === "SELL" ? dir : "HOLD",
    alert_level:      row.composite_score >= 0.75 ? "CONVICTION" : row.composite_score >= 0.55 ? "STRONG_SIGNAL" : "ALERT",
    conviction_score: row.composite_score,
    total_premium:    row.total_premium ?? 0,
    trade_count:      row.trade_count ?? 0,
    is_accelerating:  row.is_accelerating,
    timestamp:        row.created_at,
    contract_type:    row.contract_type ?? undefined,
    strike:           undefined,
    expiry:           undefined,
  };
}

interface Props { signals: WsSignal[]; connected: boolean; token: string | null; }

export function SignalFeed({ signals, connected, token }: Props) {
  // DB-backed signals polled as fallback
  const [dbSignals, setDbSignals] = useState<WsSignal[]>([]);
  const [polling,   setPolling]   = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const pollDb = async () => {
    if (!token) return;
    setPolling(true);
    try {
      const res = await api.getSignalHistory(token, { limit: 50, offset: 0 });
      setDbSignals(res.signals.map(dbRowToWsSignal));
    } catch {
      // silently ignore — WS signals take priority anyway
    } finally {
      setPolling(false);
    }
  };

  // Poll DB on mount + every 20s
  useEffect(() => {
    if (!token) return;
    pollDb();
    pollRef.current = setInterval(pollDb, POLL_MS);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [token]); // eslint-disable-line react-hooks/exhaustive-deps

  // Prefer live WS signals; fall back to DB signals
  const displayed: WsSignal[] = signals.length > 0 ? signals : dbSignals;

  if (displayed.length === 0) return <EmptyState connected={connected} polling={polling} />;

  return (
    <div className="flex flex-col gap-2">
      {signals.length === 0 && dbSignals.length > 0 && (
        <div
          className="px-4 py-2 rounded-lg text-xs font-mono"
          style={{ background: "rgba(232,160,32,0.07)", color: "var(--muted)", border: "1px solid var(--border)" }}
        >
          ⚡ Showing persisted signals from DB — live WebSocket signals will appear here when detected.
          {polling && " Refreshing…"}
        </div>
      )}
      {displayed.map((s, i) => (
        <div
          key={i}
          className="card px-4 py-3 flex items-center gap-4 flex-wrap animate-fade-up"
          style={{ background: alertBg(s.alert_level) }}
        >
          {/* Direction */}
          <span
            className="w-14 text-center py-1 rounded-md text-xs font-black uppercase tracking-wider"
            style={{
              background: `${directionColor(s.direction)}18`,
              color:       directionColor(s.direction),
              border:      `1px solid ${directionColor(s.direction)}30`,
            }}
          >
            {s.direction}
          </span>

          {/* Ticker */}
          <span className="font-mono font-bold text-base tabular" style={{ color: "var(--amber)", minWidth: 52 }}>
            {s.ticker}
          </span>

          {s.contract_type && (
            <span className={s.contract_type === "CALL" ? "badge badge-green" : "badge badge-red"}>
              {s.contract_type}
            </span>
          )}
          {s.strike !== undefined && (
            <span className="text-sm font-mono tabular" style={{ color: "var(--text)" }}>
              ${(s.strike as number).toFixed(0)}
            </span>
          )}
          {s.expiry && (
            <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>{s.expiry}</span>
          )}

          {(s.total_premium ?? 0) > 0 && (
            <span className="font-mono font-semibold text-sm tabular" style={{ color: "var(--amber)" }}>
              {fmt$(s.total_premium ?? 0)}
            </span>
          )}

          <span
            className="ml-auto text-2xs font-bold uppercase tracking-widest"
            style={{ color: alertColor(s.alert_level) }}
          >
            {s.alert_level.replace("_", " ")}
          </span>

          {s.is_accelerating && <span className="badge badge-amber">⚡ Accel</span>}

          {(s.trade_count ?? 0) > 0 && (
            <span className="text-xs font-mono" style={{ color: "var(--faint)" }}>
              {s.trade_count} trades
            </span>
          )}

          <span className="text-xs font-mono" style={{ color: "var(--faint)" }}>
            {new Date(s.timestamp).toLocaleTimeString()}
          </span>
        </div>
      ))}
    </div>
  );
}
