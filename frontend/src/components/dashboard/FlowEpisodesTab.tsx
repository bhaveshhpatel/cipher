"use client";
import { useState } from "react";
import type { FlowEpisode } from "@/lib/api";
import type { FlowEpisodesFilters } from "@/hooks/useFlowEpisodes";

interface Props {
  episodes: FlowEpisode[];
  loading:  boolean;
  error:    string | null;
  onFilter: (f: FlowEpisodesFilters) => void;
}

// ── helpers ───────────────────────────────────────────────────────────────────

const fmt$ = (n: number) =>
  n >= 1_000_000 ? `$${(n / 1_000_000).toFixed(2)}M`
  : n >= 1_000   ? `$${(n / 1_000).toFixed(1)}K`
  : `$${n.toFixed(0)}`;

/** Format a signed delta: -$20.0K or +$50.0K */
const fmtDelta = (delta: number) => {
  const abs = Math.abs(delta);
  const formatted = fmt$(abs);
  return delta >= 0 ? `+${formatted}` : `-${formatted}`;
};

const fmtDuration = (secs: number) => {
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}m ${s}s`;
};

const fmtTime = (ts: string) => {
  try { return new Date(ts).toLocaleTimeString(); } catch { return ts; }
};

/** Alert level → badge style */
const alertBadgeStyle = (level: string): React.CSSProperties => {
  switch (level) {
    case "STRONG": return { background: "var(--orange)",  color: "#fff" };
    case "ALERT":  return { background: "var(--gold)",    color: "#1a0f00" };
    case "HOLD":   return { background: "var(--blue)",    color: "#fff" };
    default:       return { background: "var(--surface-2)", color: "var(--muted)", border: "1px solid var(--border)" }; // WATCH
  }
};

/** Direction → badge class */
const dirBadge = (d: string) =>
  d === "BULLISH" ? "badge badge-green" : d === "BEARISH" ? "badge badge-red" : "badge badge-muted";

// ── skeleton ──────────────────────────────────────────────────────────────────

function SkeletonRows() {
  return (
    <>
      {Array.from({ length: 5 }).map((_, i) => (
        <tr key={i} className="border-b" style={{ borderColor: "var(--border)" }}>
          {Array.from({ length: 8 }).map((__, j) => (
            <td key={j} className="px-3 py-3">
              <div className="skeleton h-4 rounded" style={{ width: j === 0 ? 56 : 72 }} />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

// ── empty state ───────────────────────────────────────────────────────────────

function EmptyState() {
  return (
    <div
      className="flex flex-col items-center justify-center py-20 gap-4"
      style={{ color: "var(--faint)" }}
    >
      <span className="text-4xl">◎</span>
      <p className="text-base font-semibold" style={{ color: "var(--muted)" }}>
        No active episodes match these filters
      </p>
      <p className="text-sm">Episodes accumulate when 3+ trades hit the same ticker in a session.</p>
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export function FlowEpisodesTab({ episodes, loading, error, onFilter }: Props) {
  const [direction,    setDirection]    = useState("ALL");
  const [contractType, setContractType] = useState("ALL");
  const [alertLevel,   setAlertLevel]   = useState("ALL");
  const [accelerating, setAccelerating] = useState(false);

  const applyFilters = (
    d  = direction,
    ct = contractType,
    al = alertLevel,
    ac = accelerating,
  ) => {
    const f: FlowEpisodesFilters = {};
    if (d  !== "ALL") f.direction     = d;
    if (ct !== "ALL") f.contract_type = ct;
    if (al !== "ALL") f.alert_level   = al;
    // "accelerating" is a client-side filter — sort by delta premium desc
    // We pass the base filters to the hook; acceleration sort is done locally
    void ac; // used only for local sort (see sorted below)
    onFilter(f);
  };

  const handleDir  = (v: string) => { setDirection(v);    applyFilters(v); };
  const handleCT   = (v: string) => { setContractType(v); applyFilters(direction, v); };
  const handleAL   = (v: string) => { setAlertLevel(v);   applyFilters(direction, contractType, v); };
  const handleAcc  = ()          => {
    const next = !accelerating;
    setAccelerating(next);
    applyFilters(direction, contractType, alertLevel, next);
  };

  // Client-side: sort by (total_premium - last_signaled_premium) desc when accelerating toggle is on
  const sorted = accelerating
    ? [...episodes].sort((a, b) =>
        (b.total_premium - b.last_signaled_premium) - (a.total_premium - a.last_signaled_premium)
      )
    : episodes;

  return (
    <div className="flex flex-col gap-4">

      {/* Filter bar */}
      <div className="flex items-center gap-2 flex-wrap">
        {/* Direction */}
        {["ALL", "BULLISH", "BEARISH"].map(d => (
          <button
            key={d}
            onClick={() => handleDir(d)}
            className="px-3 py-1.5 rounded-md text-xs font-semibold transition-all"
            style={{
              background: direction === d ? (d === "BULLISH" ? "var(--green)" : d === "BEARISH" ? "var(--red)" : "var(--amber)") : "var(--surface-2)",
              color:      direction === d ? "#fff" : "var(--muted)",
              border:     `1px solid ${direction === d ? "transparent" : "var(--border)"}`,
            }}
          >
            {d === "ALL" ? "All Directions" : d}
          </button>
        ))}

        <div className="w-px h-5 mx-1" style={{ background: "var(--border)" }} />

        {/* Contract Type */}
        {["ALL", "CALL", "PUT"].map(ct => (
          <button
            key={ct}
            onClick={() => handleCT(ct)}
            className="px-3 py-1.5 rounded-md text-xs font-semibold transition-all"
            style={{
              background: contractType === ct ? (ct === "CALL" ? "var(--green)" : ct === "PUT" ? "var(--red)" : "var(--amber)") : "var(--surface-2)",
              color:      contractType === ct ? "#fff" : "var(--muted)",
              border:     `1px solid ${contractType === ct ? "transparent" : "var(--border)"}`,
            }}
          >
            {ct === "ALL" ? "All Types" : ct}
          </button>
        ))}

        <div className="w-px h-5 mx-1" style={{ background: "var(--border)" }} />

        {/* Alert Level */}
        {["ALL", "STRONG", "ALERT", "HOLD", "WATCH"].map(al => (
          <button
            key={al}
            onClick={() => handleAL(al)}
            className="px-3 py-1.5 rounded-md text-xs font-semibold transition-all"
            style={{
              ...(alertLevel === al ? alertBadgeStyle(al) : {
                background: "var(--surface-2)",
                color:      "var(--muted)",
                border:     "1px solid var(--border)",
              }),
            }}
          >
            {al === "ALL" ? "All Levels" : al}
          </button>
        ))}

        <div className="w-px h-5 mx-1" style={{ background: "var(--border)" }} />

        {/* Accelerating toggle */}
        <button
          onClick={handleAcc}
          className="px-3 py-1.5 rounded-md text-xs font-semibold transition-all"
          style={{
            background: accelerating ? "var(--amber)" : "var(--surface-2)",
            color:      accelerating ? "#1a0f00"       : "var(--muted)",
            border:     `1px solid ${accelerating ? "var(--amber)" : "var(--border)"}`,
          }}
        >
          ↑ Accelerating
        </button>
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px]">
            <thead>
              <tr>
                {[
                  "Ticker", "Direction", "Contract Type", "Alert Level",
                  "Trades", "Total Premium", "ΔPremium", "Duration", "Started",
                ].map(h => (
                  <th
                    key={h}
                    className="px-3 py-3 text-left whitespace-nowrap"
                    style={{
                      color:         "var(--faint)",
                      fontSize:      "0.7rem",
                      fontWeight:    700,
                      letterSpacing: "0.07em",
                      textTransform: "uppercase",
                      background:    "var(--surface-2)",
                      borderBottom:  "1px solid var(--border)",
                    }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <SkeletonRows />
              ) : error ? (
                <tr>
                  <td colSpan={9} className="px-4 py-10 text-center text-sm" style={{ color: "var(--red)" }}>
                    ⚠ {error}
                  </td>
                </tr>
              ) : sorted.length === 0 ? (
                <tr><td colSpan={9}><EmptyState /></td></tr>
              ) : (
                sorted.map((ep) => {
                  const delta = ep.total_premium - ep.last_signaled_premium;
                  return (
                    <tr
                      key={ep.id}
                      className="border-b transition-colors hover:bg-[var(--surface-2)] animate-fade-up"
                      style={{ borderColor: "var(--border)" }}
                    >
                      <td className="px-3 py-3 font-mono font-bold text-sm" style={{ color: "var(--amber)" }}>
                        {ep.ticker}
                      </td>
                      <td className="px-3 py-3">
                        <span className={dirBadge(ep.direction)}>{ep.direction}</span>
                      </td>
                      <td className="px-3 py-3">
                        <span className={ep.contract_type === "CALL" ? "badge badge-green" : "badge badge-red"}>
                          {ep.contract_type}
                        </span>
                      </td>
                      <td className="px-3 py-3">
                        <span
                          className="px-2 py-0.5 rounded text-xs font-bold"
                          style={alertBadgeStyle(ep.alert_level)}
                        >
                          {ep.alert_level}
                        </span>
                      </td>
                      <td className="px-3 py-3 font-mono tabular text-sm" style={{ color: "var(--text)" }}>
                        {ep.trade_count.toLocaleString()}
                      </td>
                      <td className="px-3 py-3 font-mono font-semibold text-sm tabular" style={{ color: "var(--amber)" }}>
                        {fmt$(ep.total_premium)}
                      </td>
                      <td className="px-3 py-3 font-mono text-sm tabular" style={{ color: delta >= 0 ? "var(--green)" : "var(--red)" }}>
                        {fmtDelta(delta)}
                      </td>
                      <td className="px-3 py-3 font-mono text-xs" style={{ color: "var(--muted)" }}>
                        {fmtDuration(ep.duration_seconds)}
                      </td>
                      <td className="px-3 py-3 font-mono text-xs" style={{ color: "var(--faint)" }}>
                        {fmtTime(ep.started_at)}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
