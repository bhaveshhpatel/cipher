"use client";
import { useState, useEffect, useCallback, useRef } from "react";
import { useSignalHistory, HistoryFilters } from "@/hooks/useSignalHistory";
import type { SignalHistoryItem } from "@/lib/api";

const AUTO_REFRESH_MS = 60_000;

const REC_COLOR: Record<string, string> = {
  BUY:  "var(--green)",
  SELL: "var(--red)",
  HOLD: "var(--amber)",
};

const TIER_LABELS: Record<string, string> = {
  WHALE:         "🐳 Whale",
  INSTITUTIONAL: "🏦 Institutional",
  LARGE:         "📊 Large",
  RETAIL:        "👤 Retail",
};

function ScoreBar({ value, color }: { value: number; color: string }) {
  const pct = Math.round(value * 100);
  return (
    <div className="flex items-center gap-2 min-w-0">
      <div
        className="h-1.5 rounded-full shrink-0"
        style={{ width: `${pct}%`, maxWidth: 72, minWidth: 4, background: color, opacity: 0.85 }}
      />
      <span className="text-xs font-mono tabular" style={{ color }}>{pct}%</span>
    </div>
  );
}

function HistoryRow({ item }: { item: SignalHistoryItem }) {
  const recColor = REC_COLOR[item.recommendation] ?? "var(--muted)";
  const ts = item.created_at
    ? new Date(item.created_at).toLocaleString(undefined, {
        month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit",
      })
    : "—";

  return (
    <tr
      style={{ borderBottom: "1px solid var(--border)", transition: "background 0.1s" }}
      className="hover:bg-[var(--surface-2)]"
    >
      <td className="px-3 py-2.5">
        <span className="font-mono font-bold text-sm" style={{ color: "var(--amber)" }}>{item.ticker}</span>
      </td>
      <td className="px-3 py-2.5">
        <span
          className="inline-block px-2 py-0.5 rounded text-xs font-bold font-mono"
          style={{
            color:      recColor,
            background: `color-mix(in srgb, ${recColor} 15%, transparent)`,
            border:     `1px solid color-mix(in srgb, ${recColor} 30%, transparent)`,
          }}
        >
          {item.recommendation}
        </span>
      </td>
      <td className="px-3 py-2.5"><ScoreBar value={item.composite_score} color={recColor} /></td>
      <td className="px-3 py-2.5 hidden md:table-cell"><ScoreBar value={item.flow_score} color="var(--blue)" /></td>
      <td className="px-3 py-2.5 hidden lg:table-cell"><ScoreBar value={item.backtest_score} color="var(--purple)" /></td>
      <td className="px-3 py-2.5 hidden sm:table-cell">
        <span className="text-xs" style={{ color: "var(--muted)" }}>
          {item.influence_tier ? (TIER_LABELS[item.influence_tier] ?? item.influence_tier) : "—"}
        </span>
      </td>
      <td className="px-3 py-2.5 hidden lg:table-cell">
        <span className="text-xs font-mono tabular" style={{ color: "var(--text)" }}>
          {item.total_premium != null
            ? `$${(item.total_premium / 1_000).toLocaleString(undefined, { maximumFractionDigits: 0 })}k`
            : "—"}
        </span>
      </td>
      <td className="px-3 py-2.5 hidden xl:table-cell text-center">
        {item.is_accelerating
          ? <span style={{ color: "var(--green)", fontSize: 14 }}>⚡</span>
          : <span style={{ color: "var(--faint)" }}>—</span>}
      </td>
      <td className="px-3 py-2.5 text-right">
        <span className="text-xs font-mono" style={{ color: "var(--faint)" }}>{ts}</span>
      </td>
    </tr>
  );
}

export interface SignalHistoryProps { token: string | null; }

export function SignalHistory({ token }: SignalHistoryProps) {
  const { items, total, loading, error, page, pageSize, fetch, setPage } = useSignalHistory(token);

  const [filters,      setFilters]      = useState<HistoryFilters>({});
  const [tickerInput,  setTickerInput]  = useState("");
  const [countdown,    setCountdown]    = useState(AUTO_REFRESH_MS / 1000);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const currentFilters = useRef<HistoryFilters>({});
  const currentPage    = useRef(1);

  const doFetch = useCallback((f: HistoryFilters, p: number) => {
    currentFilters.current = f;
    currentPage.current    = p;
    fetch(f, p);
    setCountdown(AUTO_REFRESH_MS / 1000);
  }, [fetch]);

  // Load ALL on mount
  useEffect(() => { doFetch({}, 1); }, [doFetch]);

  // Auto-refresh every 60s
  useEffect(() => {
    timerRef.current = setInterval(() => {
      setCountdown(c => {
        if (c <= 1) {
          doFetch(currentFilters.current, currentPage.current);
          return AUTO_REFRESH_MS / 1000;
        }
        return c - 1;
      });
    }, 1000);
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [doFetch]);

  const applyFilters = useCallback(() => {
    const f: HistoryFilters = { ...filters };
    if (tickerInput.trim()) f.ticker = tickerInput.trim();
    else delete f.ticker;
    doFetch(f, 1);
  }, [filters, tickerInput, doFetch]);

  const clearFilters = () => {
    setTickerInput("");
    setFilters({});
    doFetch({}, 1);
  };

  const handlePageChange = (p: number) => {
    setPage(p);
    const f: HistoryFilters = { ...filters };
    if (tickerInput.trim()) f.ticker = tickerInput.trim();
    doFetch(f, p);
  };

  const totalPages = Math.ceil(total / pageSize);

  return (
    <div className="flex flex-col gap-4">

      {/* ── Filter bar ── */}
      <div
        className="flex flex-wrap items-end gap-3 p-4 rounded-xl"
        style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
      >
        {/* Ticker */}
        <div className="flex flex-col gap-1">
          <label className="text-xs font-mono" style={{ color: "var(--muted)" }}>Filter by Ticker</label>
          <input
            value={tickerInput}
            onChange={e => setTickerInput(e.target.value.toUpperCase())}
            placeholder="All tickers"
            maxLength={6}
            className="w-28 px-2 py-1.5 rounded-lg text-sm font-mono uppercase outline-none"
            style={{ background: "var(--surface-2)", border: "1px solid var(--border)", color: "var(--text)" }}
            onKeyDown={e => e.key === "Enter" && applyFilters()}
          />
        </div>

        {/* Direction */}
        <div className="flex flex-col gap-1">
          <label className="text-xs font-mono" style={{ color: "var(--muted)" }}>Direction</label>
          <select
            value={filters.direction ?? ""}
            onChange={e => setFilters(f => ({ ...f, direction: e.target.value || undefined }))}
            className="px-2 py-1.5 rounded-lg text-sm outline-none"
            style={{ background: "var(--surface-2)", border: "1px solid var(--border)", color: "var(--text)" }}
          >
            <option value="">All</option>
            <option value="bullish">Bullish (BUY)</option>
            <option value="bearish">Bearish (SELL)</option>
            <option value="neutral">Neutral (HOLD)</option>
          </select>
        </div>

        {/* Tier */}
        <div className="flex flex-col gap-1">
          <label className="text-xs font-mono" style={{ color: "var(--muted)" }}>Tier</label>
          <select
            value={filters.tier ?? ""}
            onChange={e => setFilters(f => ({ ...f, tier: e.target.value || undefined }))}
            className="px-2 py-1.5 rounded-lg text-sm outline-none"
            style={{ background: "var(--surface-2)", border: "1px solid var(--border)", color: "var(--text)" }}
          >
            <option value="">All tiers</option>
            <option value="whale">Whale</option>
            <option value="institutional">Institutional</option>
            <option value="large">Large</option>
            <option value="retail">Retail</option>
          </select>
        </div>

        {/* Min conviction */}
        <div className="flex flex-col gap-1">
          <label className="text-xs font-mono" style={{ color: "var(--muted)" }}>Min Score</label>
          <select
            value={filters.min_conviction ?? 0}
            onChange={e => setFilters(f => ({ ...f, min_conviction: Number(e.target.value) || undefined }))}
            className="px-2 py-1.5 rounded-lg text-sm outline-none"
            style={{ background: "var(--surface-2)", border: "1px solid var(--border)", color: "var(--text)" }}
          >
            <option value={0}>Any</option>
            <option value={0.5}>50%+</option>
            <option value={0.65}>65%+</option>
            <option value={0.75}>75%+</option>
            <option value={0.85}>85%+</option>
          </select>
        </div>

        <div className="flex items-center gap-2 mt-auto">
          <button onClick={applyFilters} disabled={loading} className="btn btn-primary text-sm px-4 py-1.5">
            {loading ? "Loading…" : "Apply"}
          </button>
          <button onClick={clearFilters} disabled={loading} className="btn btn-ghost text-sm px-3 py-1.5">
            Clear
          </button>
        </div>

        <div className="ml-auto flex items-center gap-3">
          {total > 0 && (
            <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
              {total.toLocaleString()} signal{total !== 1 ? "s" : ""}
            </span>
          )}
          <span className="text-xs font-mono" style={{ color: "var(--faint)" }}>
            refresh in {countdown}s
          </span>
        </div>
      </div>

      {/* ── Error ── */}
      {error && (
        <div
          className="px-4 py-3 rounded-lg text-sm"
          style={{ background: "rgba(239,68,68,0.1)", color: "var(--red)", border: "1px solid rgba(239,68,68,0.25)" }}
        >
          {error}
        </div>
      )}

      {/* ── Table ── */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: "1px solid var(--border)", background: "var(--surface)" }}
      >
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border)", background: "var(--surface-2)" }}>
                {[
                  ["Ticker",    "px-3 py-3"],
                  ["Signal",    "px-3 py-3"],
                  ["Composite", "px-3 py-3"],
                  ["Flow",      "px-3 py-3 hidden md:table-cell"],
                  ["Backtest",  "px-3 py-3 hidden lg:table-cell"],
                  ["Tier",      "px-3 py-3 hidden sm:table-cell"],
                  ["Premium",   "px-3 py-3 hidden lg:table-cell"],
                  ["⚡",         "px-3 py-3 hidden xl:table-cell text-center"],
                  ["Time",      "px-3 py-3 text-right"],
                ].map(([label, cls]) => (
                  <th
                    key={label}
                    className={`${cls} text-xs font-semibold font-mono uppercase tracking-wider`}
                    style={{ color: "var(--muted)" }}
                  >
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && items.length === 0 && (
                <tr>
                  <td colSpan={9} className="px-4 py-12 text-center">
                    <div className="flex flex-col items-center gap-3">
                      <svg className="animate-spin" width="24" height="24" viewBox="0 0 24 24" fill="none"
                           stroke="var(--amber)" strokeWidth="2.5">
                        <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
                      </svg>
                      <span className="text-sm" style={{ color: "var(--muted)" }}>Loading signal history…</span>
                    </div>
                  </td>
                </tr>
              )}
              {!loading && items.length === 0 && (
                <tr>
                  <td colSpan={9} className="px-4 py-14 text-center">
                    <div className="flex flex-col items-center gap-2">
                      <span className="text-3xl">📭</span>
                      <p className="text-sm font-medium" style={{ color: "var(--text)" }}>No signals yet</p>
                      <p className="text-xs" style={{ color: "var(--muted)" }}>
                        Composite signals will appear here once the stream processes qualifying flow episodes.
                      </p>
                    </div>
                  </td>
                </tr>
              )}
              {items.map(item => <HistoryRow key={item.id} item={item} />)}
            </tbody>
          </table>
        </div>

        {/* ── Pagination ── */}
        {totalPages > 1 && (
          <div
            className="flex items-center justify-between px-4 py-3"
            style={{ borderTop: "1px solid var(--border)" }}
          >
            <button
              onClick={() => handlePageChange(page - 1)}
              disabled={page <= 1 || loading}
              className="btn btn-ghost text-xs px-3 py-1.5"
            >
              ← Prev
            </button>
            <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
              Page {page} / {totalPages}
            </span>
            <button
              onClick={() => handlePageChange(page + 1)}
              disabled={page >= totalPages || loading}
              className="btn btn-ghost text-xs px-3 py-1.5"
            >
              Next →
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
