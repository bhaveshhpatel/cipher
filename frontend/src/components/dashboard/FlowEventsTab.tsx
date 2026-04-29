"use client";
import { useState } from "react";
import type { FlowEventRaw } from "@/lib/api";
import type { FlowEventsFilters } from "@/hooks/useFlowEvents";

interface Props {
  events:           FlowEventRaw[];
  loading:          boolean;
  error:            string | null;
  onFiltersChange:  (f: FlowEventsFilters) => void;
}

// ── helpers ───────────────────────────────────────────────────────────────────

const fmt$ = (n: number) =>
  n >= 1_000_000 ? `$${(n / 1_000_000).toFixed(2)}M`
  : n >= 1_000   ? `$${(n / 1_000).toFixed(1)}K`
  : `$${n.toFixed(0)}`;

const fmtTime = (ts: string) => {
  try { return new Date(ts).toLocaleTimeString(); } catch { return ts; }
};

const sentimentBadge = (s: string) => {
  if (s === "BULLISH") return "badge badge-green";
  if (s === "BEARISH") return "badge badge-red";
  return "badge badge-muted";
};

const tierBadge = (t: string) => {
  if (t === "T1") return "badge badge-amber";
  if (t === "T2") return "badge badge-teal";
  if (t === "T3") return "badge badge-blue";
  return "badge badge-muted";
};

// ── skeleton ──────────────────────────────────────────────────────────────────

function SkeletonRows() {
  return (
    <>
      {Array.from({ length: 6 }).map((_, i) => (
        <tr key={i} className="border-b" style={{ borderColor: "var(--border)" }}>
          {Array.from({ length: 10 }).map((__, j) => (
            <td key={j} className="px-3 py-3">
              <div className="skeleton h-4 rounded" style={{ width: j === 0 ? 48 : 72 }} />
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
      <span className="text-4xl">⟁</span>
      <p className="text-base font-semibold" style={{ color: "var(--muted)" }}>
        No flow events match these filters
      </p>
      <p className="text-sm">Adjust filters or wait for the next auto-refresh (10s).</p>
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export function FlowEventsTab({ events, loading, error, onFiltersChange }: Props) {
  const [sentiment,    setSentiment]    = useState("ALL");
  const [contractType, setContractType] = useState("ALL");
  const [tier,         setTier]         = useState("ALL");
  const [aggressive,   setAggressive]   = useState(false);
  const [goldenSweep,  setGoldenSweep]  = useState(false);

  const applyFilters = (
    s  = sentiment,
    ct = contractType,
    t  = tier,
    ag = aggressive,
    gs = goldenSweep,
  ) => {
    const f: FlowEventsFilters = {};
    if (s  !== "ALL") f.sentiment     = s;
    if (ct !== "ALL") f.contract_type = ct;
    if (t  !== "ALL") f.tier          = t;
    if (ag)           f.aggressive    = true;
    if (gs)           f.golden_sweep  = true;
    onFiltersChange(f);
  };

  const handleSentiment = (v: string) => { setSentiment(v);    applyFilters(v); };
  const handleCT        = (v: string) => { setContractType(v); applyFilters(sentiment, v); };
  const handleTier      = (v: string) => { setTier(v);         applyFilters(sentiment, contractType, v); };
  const handleAgg       = ()          => {
    const next = !aggressive;
    setAggressive(next);
    applyFilters(sentiment, contractType, tier, next);
  };
  const handleGS        = ()          => {
    const next = !goldenSweep;
    setGoldenSweep(next);
    applyFilters(sentiment, contractType, tier, aggressive, next);
  };

  // KPI stats
  const totalPremium  = events.reduce((s, e) => s + e.premium, 0);
  const uniqueTickers = new Set(events.map(e => e.ticker)).size;

  return (
    <div className="flex flex-col gap-4">

      {/* KPI bar */}
      {events.length > 0 && (
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: "Total Premium",  value: fmt$(totalPremium), accent: "var(--amber)" },
            { label: "Trade Count",    value: events.length,       accent: "var(--text)"  },
            { label: "Unique Tickers", value: uniqueTickers,       accent: "var(--teal)"  },
          ].map(({ label, value, accent }) => (
            <div key={label} className="card px-4 py-3 flex flex-col gap-0.5">
              <span className="text-2xs font-bold uppercase tracking-widest" style={{ color: "var(--faint)" }}>{label}</span>
              <span className="text-2xl font-bold font-mono tabular" style={{ color: accent }}>{value}</span>
            </div>
          ))}
        </div>
      )}

      {/* Filter bar */}
      <div className="flex items-center gap-2 flex-wrap">
        {["ALL", "BULLISH", "BEARISH", "NEUTRAL"].map(s => (
          <button
            key={s}
            onClick={() => handleSentiment(s)}
            className="px-3 py-1.5 rounded-md text-xs font-semibold transition-all"
            style={{
              background: sentiment === s ? "var(--amber)" : "var(--surface-2)",
              color:      sentiment === s ? "#1a0f00"       : "var(--muted)",
              border:     `1px solid ${sentiment === s ? "var(--amber)" : "var(--border)"}`,
            }}
          >
            {s === "ALL" ? "All Sentiment" : s}
          </button>
        ))}

        <div className="w-px h-5 mx-1" style={{ background: "var(--border)" }} />

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

        {["ALL", "T1", "T2", "T3"].map(t => (
          <button
            key={t}
            onClick={() => handleTier(t)}
            className="px-3 py-1.5 rounded-md text-xs font-semibold transition-all"
            style={{
              background: tier === t ? "var(--amber)" : "var(--surface-2)",
              color:      tier === t ? "#1a0f00"       : "var(--muted)",
              border:     `1px solid ${tier === t ? "var(--amber)" : "var(--border)"}`,
            }}
          >
            {t === "ALL" ? "All Tiers" : t}
          </button>
        ))}

        <div className="w-px h-5 mx-1" style={{ background: "var(--border)" }} />

        <button
          onClick={handleAgg}
          className="px-3 py-1.5 rounded-md text-xs font-semibold transition-all"
          style={{
            background: aggressive ? "var(--orange)" : "var(--surface-2)",
            color:      aggressive ? "#fff"           : "var(--muted)",
            border:     `1px solid ${aggressive ? "var(--orange)" : "var(--border)"}`,
          }}
        >
          ⚡ Aggressive
        </button>
        <button
          onClick={handleGS}
          className="px-3 py-1.5 rounded-md text-xs font-semibold transition-all"
          style={{
            background: goldenSweep ? "var(--gold)" : "var(--surface-2)",
            color:      goldenSweep ? "#1a0f00"      : "var(--muted)",
            border:     `1px solid ${goldenSweep ? "var(--gold)" : "var(--border)"}`,
          }}
        >
          ★ Golden Sweep
        </button>
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px]">
            <thead>
              <tr>
                {[
                  "Time", "Ticker", "Contract", "Type", "Sentiment",
                  "Premium", "Size", "Bid / Ask / Fill", "Tier", "Flags",
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
                  <td colSpan={10} className="px-4 py-10 text-center text-sm" style={{ color: "var(--red)" }}>
                    ⚠ {error}
                  </td>
                </tr>
              ) : events.length === 0 ? (
                <tr><td colSpan={10}><EmptyState /></td></tr>
              ) : (
                events.map((e) => (
                  <tr
                    key={e.id}
                    className="border-b transition-colors hover:bg-[var(--surface-2)] animate-fade-up"
                    style={{ borderColor: "var(--border)" }}
                  >
                    <td className="px-3 py-3 font-mono text-xs" style={{ color: "var(--faint)" }}>
                      {fmtTime(e.timestamp)}
                    </td>
                    <td className="px-3 py-3 font-mono font-bold text-sm" style={{ color: "var(--amber)" }}>
                      {e.ticker}
                    </td>
                    <td className="px-3 py-3 font-mono text-xs" style={{ color: "var(--muted)" }}>
                      ${e.strike.toFixed(0)} {e.expiry}
                    </td>
                    <td className="px-3 py-3">
                      <span className={e.contract_type === "CALL" ? "badge badge-green" : "badge badge-red"}>
                        {e.contract_type}
                      </span>
                    </td>
                    <td className="px-3 py-3">
                      <span className={sentimentBadge(e.sentiment)}>{e.sentiment}</span>
                    </td>
                    <td className="px-3 py-3 font-mono font-semibold text-sm tabular text-right" style={{ color: "var(--amber)" }}>
                      {fmt$(e.premium)}
                    </td>
                    <td className="px-3 py-3 font-mono text-xs tabular text-right" style={{ color: "var(--text)" }}>
                      {e.size.toLocaleString()}
                    </td>
                    <td className="px-3 py-3 font-mono text-xs tabular" style={{ color: "var(--muted)" }}>
                      {e.bid.toFixed(2)} / {e.ask.toFixed(2)} / <span style={{ color: "var(--text)" }}>{e.fill_price.toFixed(2)}</span>
                    </td>
                    <td className="px-3 py-3">
                      <span className={tierBadge(e.tier)}>{e.tier}</span>
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex items-center gap-1.5">
                        {e.is_aggressive && (
                          <span
                            className="text-xs px-1.5 py-0.5 rounded font-bold"
                            style={{ background: "var(--orange)", color: "#fff" }}
                            title="Aggressive fill"
                          >⚡</span>
                        )}
                        {e.is_golden_sweep && (
                          <span
                            className="text-xs px-1.5 py-0.5 rounded font-bold"
                            style={{ background: "var(--gold)", color: "#1a0f00" }}
                            title="Golden Sweep"
                          >★</span>
                        )}
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
