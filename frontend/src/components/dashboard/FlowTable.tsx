"use client";
import { useState } from "react";
import type { FlowEvent } from "@/lib/api";

interface Props {
  events:  FlowEvent[];
  loading: boolean;
  error:   string | null;
  ticker:  string;
  onScan:  (t: string) => void;
}

const sentimentColor = (s: string) => {
  if (s === "BULLISH")  return "var(--green)";
  if (s === "BEARISH")  return "var(--red)";
  return "var(--muted)";
};
const sentimentBadge = (s: string) => {
  if (s === "BULLISH") return "badge badge-green";
  if (s === "BEARISH") return "badge badge-red";
  return "badge badge-muted";
};
const tierBadge = (t: string) => {
  if (t === "WHALE")       return "badge badge-amber";
  if (t === "INSTITUTIONAL") return "badge badge-teal";
  if (t === "LARGE")       return "badge badge-blue";
  return "badge badge-muted";
};
const fmt$ = (n: number) =>
  n >= 1_000_000 ? `$${(n/1_000_000).toFixed(2)}M`
  : n >= 1_000   ? `$${(n/1_000).toFixed(1)}K`
  : `$${n.toFixed(0)}`;

function EmptyState({ ticker }: { ticker: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-20 gap-4"
         style={{ color: "var(--faint)" }}>
      <span className="text-4xl">⟁</span>
      <p className="text-base font-semibold" style={{ color: "var(--muted)" }}>No flow events for {ticker}</p>
      <p className="text-sm">Try scanning a different ticker or check stream connectivity.</p>
    </div>
  );
}

function SkeletonRows() {
  return (
    <>
      {Array.from({ length: 6 }).map((_, i) => (
        <tr key={i} className="border-b" style={{ borderColor: "var(--border)" }}>
          {Array.from({ length: 9 }).map((__, j) => (
            <td key={j} className="px-3 py-3">
              <div className="skeleton h-4 rounded" style={{ width: j === 0 ? 48 : j === 8 ? 64 : 80 }} />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

export function FlowTable({ events, loading, error, ticker, onScan }: Props) {
  const [sort,    setSort]   = useState<keyof FlowEvent>("conviction_score");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [filterSentiment, setFilterSentiment] = useState<string>("ALL");

  const sorted = [...events]
    .filter(e => filterSentiment === "ALL" || e.sentiment === filterSentiment)
    .sort((a, b) => {
      const av = a[sort], bv = b[sort];
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sortDir === "asc" ? cmp : -cmp;
    });

  const totalPremium = events.reduce((s, e) => s + e.premium, 0);
  const bullCount = events.filter(e => e.sentiment === "BULLISH").length;
  const bearCount = events.filter(e => e.sentiment === "BEARISH").length;
  const whaleCount = events.filter(e => e.influence_tier === "WHALE").length;

  const toggleSort = (col: keyof FlowEvent) => {
    if (sort === col) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSort(col); setSortDir("desc"); }
  };

  const Th = ({ col, label, right }: { col: keyof FlowEvent; label: string; right?: boolean }) => (
    <th
      onClick={() => toggleSort(col)}
      className="px-3 py-3 text-left cursor-pointer select-none whitespace-nowrap"
      style={{
        textAlign:    right ? "right" : "left",
        color:        sort === col ? "var(--amber)" : "var(--faint)",
        fontSize:     "0.7rem",
        fontWeight:   700,
        letterSpacing:"0.07em",
        textTransform:"uppercase",
        background:   "var(--surface-2)",
        borderBottom: "1px solid var(--border)",
        userSelect:   "none",
      }}
    >
      {label} {sort === col ? (sortDir === "desc" ? "↓" : "↑") : ""}
    </th>
  );

  return (
    <div className="flex flex-col gap-4">

      {/* Summary row */}
      {events.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {[
            { label: "Total Premium",  value: fmt$(totalPremium), accent: "var(--amber)" },
            { label: "Bullish Events", value: bullCount,          accent: "var(--green)" },
            { label: "Bearish Events", value: bearCount,          accent: "var(--red)" },
            { label: "Whale Trades",   value: whaleCount,         accent: "var(--amber)" },
          ].map(({ label, value, accent }) => (
            <div key={label} className="card px-4 py-3 flex flex-col gap-0.5">
              <span className="text-2xs font-bold uppercase tracking-widest" style={{ color: "var(--faint)" }}>{label}</span>
              <span className="text-2xl font-bold font-mono tabular" style={{ color: accent }}>{value}</span>
            </div>
          ))}
        </div>
      )}

      {/* Filter + count bar */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          {["ALL", "BULLISH", "BEARISH", "NEUTRAL"].map(s => (
            <button
              key={s}
              onClick={() => setFilterSentiment(s)}
              className="px-3 py-1.5 rounded-md text-xs font-semibold transition-all"
              style={{
                background:  filterSentiment === s ? "var(--amber)" : "var(--surface-2)",
                color:       filterSentiment === s ? "#1a0f00"       : "var(--muted)",
                border:      `1px solid ${filterSentiment === s ? "var(--amber)" : "var(--border)"}`,
              }}
            >
              {s}
            </button>
          ))}
        </div>
        <span className="text-xs font-mono" style={{ color: "var(--faint)" }}>
          {sorted.length} of {events.length} events
        </span>
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[700px]">
            <thead>
              <tr>
                <Th col="ticker"          label="Ticker" />
                <Th col="contract_type"   label="Type" />
                <Th col="strike"          label="Strike"     right />
                <Th col="expiry"          label="Expiry" />
                <Th col="premium"         label="Premium"    right />
                <Th col="sentiment"       label="Sentiment" />
                <Th col="influence_tier"  label="Tier" />
                <Th col="conviction_score"label="Conviction" right />
                <Th col="timestamp"       label="Time" />
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <SkeletonRows />
              ) : error ? (
                <tr><td colSpan={9} className="px-4 py-10 text-center text-sm" style={{ color: "var(--red)" }}>⚠ {error}</td></tr>
              ) : sorted.length === 0 ? (
                <tr><td colSpan={9}><EmptyState ticker={ticker} /></td></tr>
              ) : (
                sorted.map((e, i) => (
                  <tr
                    key={i}
                    className="border-b transition-colors hover:bg-[var(--surface-2)] animate-fade-up"
                    style={{ borderColor: "var(--border)" }}
                  >
                    <td className="px-3 py-3 font-mono font-bold text-sm" style={{ color: "var(--amber)" }}>
                      {e.ticker}
                      {e.is_golden_sweep && <span className="ml-1 text-xs">★</span>}
                    </td>
                    <td className="px-3 py-3">
                      <span className={e.contract_type === "CALL" ? "badge badge-green" : "badge badge-red"}>
                        {e.contract_type}
                      </span>
                    </td>
                    <td className="px-3 py-3 font-mono text-sm tabular text-right" style={{ color: "var(--text)" }}>
                      ${e.strike.toFixed(0)}
                    </td>
                    <td className="px-3 py-3 font-mono text-xs" style={{ color: "var(--muted)" }}>
                      {e.expiry}
                    </td>
                    <td className="px-3 py-3 font-mono font-semibold text-sm tabular text-right"
                        style={{ color: "var(--amber)" }}>
                      {fmt$(e.premium)}
                    </td>
                    <td className="px-3 py-3">
                      <span className={sentimentBadge(e.sentiment)}>{e.sentiment}</span>
                    </td>
                    <td className="px-3 py-3">
                      <span className={tierBadge(e.influence_tier)}>{e.influence_tier}</span>
                    </td>
                    <td className="px-3 py-3 text-right">
                      <ConvictionBar score={e.conviction_score} />
                    </td>
                    <td className="px-3 py-3 font-mono text-xs" style={{ color: "var(--faint)" }}>
                      {new Date(e.timestamp).toLocaleTimeString()}
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

function ConvictionBar({ score }: { score: number }) {
  const pct = Math.min(Math.max(score * 100, 0), 100);
  const color = pct >= 70 ? "var(--green)" : pct >= 40 ? "var(--amber)" : "var(--red)";
  return (
    <div className="flex items-center gap-2 justify-end">
      <div className="w-16 h-1.5 rounded-full overflow-hidden" style={{ background: "var(--border)" }}>
        <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="text-xs font-mono tabular w-8 text-right" style={{ color }}>{pct.toFixed(0)}</span>
    </div>
  );
}
