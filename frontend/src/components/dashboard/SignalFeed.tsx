"use client";
import type { WsSignal } from "@/hooks/useSignalStream";

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

function EmptyState({ connected }: { connected: boolean }) {
  return (
    <div className="card flex flex-col items-center justify-center py-20 gap-4">
      <span className="text-4xl" style={{ color: "var(--faint)" }}>◉</span>
      <p className="text-base font-semibold" style={{ color: "var(--muted)" }}>
        {connected ? "Waiting for live signals…" : "Connecting to signal stream…"}
      </p>
      <p className="text-sm" style={{ color: "var(--faint)" }}>
        Signals appear here as whale flow is detected in real-time.
      </p>
    </div>
  );
}

interface Props { signals: WsSignal[]; connected: boolean; }

export function SignalFeed({ signals, connected }: Props) {
  if (signals.length === 0) return <EmptyState connected={connected} />;

  return (
    <div className="flex flex-col gap-2">
      {signals.map((s, i) => (
        <div
          key={i}
          className="card px-4 py-3 flex items-center gap-4 flex-wrap animate-fade-up"
          style={{ background: alertBg(s.alert_level) }}
        >
          {/* Direction badge */}
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

          {/* Contract info */}
          {s.contract_type && (
            <span className={s.contract_type === "CALL" ? "badge badge-green" : "badge badge-red"}>
              {s.contract_type}
            </span>
          )}
          {s.strike && (
            <span className="text-sm font-mono tabular" style={{ color: "var(--text)" }}>
              ${s.strike.toFixed(0)}
            </span>
          )}
          {s.expiry && (
            <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>{s.expiry}</span>
          )}

          {/* Premium */}
          {s.total_premium && (
            <span className="font-mono font-semibold text-sm tabular" style={{ color: "var(--amber)" }}>
              {s.total_premium >= 1_000_000
                ? `$${(s.total_premium/1_000_000).toFixed(2)}M`
                : `$${(s.total_premium/1_000).toFixed(1)}K`}
            </span>
          )}

          {/* Alert level */}
          <span
            className="ml-auto text-2xs font-bold uppercase tracking-widest"
            style={{ color: alertColor(s.alert_level) }}
          >
            {s.alert_level.replace("_", " ")}
          </span>

          {/* Accelerating flag */}
          {s.is_accelerating && (
            <span className="badge badge-amber">⚡ Accel</span>
          )}

          {/* Trade count */}
          {s.trade_count && (
            <span className="text-xs font-mono" style={{ color: "var(--faint)" }}>
              {s.trade_count} trades
            </span>
          )}

          {/* Time */}
          <span className="text-xs font-mono" style={{ color: "var(--faint)" }}>
            {new Date(s.timestamp).toLocaleTimeString()}
          </span>
        </div>
      ))}
    </div>
  );
}
