"use client";
import { useEffect, useState } from "react";

interface SmartSignal {
  ticker:           string;
  direction:        string;
  alert_level:      string;
  conviction_score: number;
  total_premium:    number;
  trade_count:      number;
  is_accelerating:  boolean;
  timestamp:        string;
}

const fmt$ = (n: number) =>
  n >= 1_000_000 ? `$${(n/1_000_000).toFixed(2)}M` : `$${(n/1_000).toFixed(1)}K`;

const dirColor = (d: string) =>
  d === "BUY" ? "var(--green)" : d === "SELL" ? "var(--red)" : "var(--muted)";

const alertColor = (a: string) =>
  a === "CONVICTION" ? "var(--amber)" : a === "STRONG_SIGNAL" ? "var(--teal)" : "var(--blue)";

function SkeletonRows() {
  return (
    <>
      {Array.from({ length: 8 }).map((_, i) => (
        <tr key={i} className="border-b" style={{ borderColor: "var(--border)" }}>
          {Array.from({ length: 7 }).map((__, j) => (
            <td key={j} className="px-3 py-3">
              <div className="skeleton h-4 rounded" style={{ width: j === 0 ? 52 : 72 }} />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

type SortKey = keyof SmartSignal;

export function SmartSignals() {
  const [signals, setSignals] = useState<SmartSignal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState<string | null>(null);
  const [sort,    setSort]    = useState<SortKey>("conviction_score");
  const [sortDir, setSortDir] = useState<"asc"|"desc">("desc");

  useEffect(() => {
    const token = typeof window !== "undefined"
      ? localStorage.getItem("cipher_token")
      : null;
    if (!token) {
      setLoading(false);
      return;
    }
    setLoading(true);
    fetch("/api/signals/smart", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<SmartSignal[]>;
      })
      .then((d: SmartSignal[]) => { setSignals(d); setLoading(false); })
      .catch((e: Error) => { setError(e.message); setLoading(false); });
  }, []);

  const sorted = [...signals].sort((a, b) => {
    const av = a[sort], bv = b[sort];
    const cmp = av < bv ? -1 : av > bv ? 1 : 0;
    return sortDir === "asc" ? cmp : -cmp;
  });

  const toggleSort = (col: SortKey) => {
    if (sort === col) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSort(col); setSortDir("desc"); }
  };

  const Th = ({ col, label, right }: { col: SortKey; label: string; right?: boolean }) => (
    <th
      onClick={() => toggleSort(col)}
      className="px-3 py-3 cursor-pointer select-none whitespace-nowrap"
      style={{
        textAlign:     right ? "right" : "left",
        color:         sort === col ? "var(--amber)" : "var(--faint)",
        fontSize:      "0.7rem",
        fontWeight:    700,
        letterSpacing: "0.07em",
        textTransform: "uppercase",
        background:    "var(--surface-2)",
        borderBottom:  "1px solid var(--border)",
      }}
    >
      {label} {sort === col ? (sortDir === "desc" ? "↓" : "↑") : ""}
    </th>
  );

  // Summary stats
  const totalPremium = signals.reduce((s, x) => s + x.total_premium, 0);
  const bullCount    = signals.filter(x => x.direction === "BUY").length;
  const convCount    = signals.filter(x => x.alert_level === "CONVICTION").length;
  const accelCount   = signals.filter(x => x.is_accelerating).length;

  return (
    <div className="flex flex-col gap-4">

      {/* Summary */}
      {signals.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {[
            { label: "Total Premium",   value: fmt$(totalPremium), accent: "var(--amber)" },
            { label: "Bullish",         value: bullCount,          accent: "var(--green)" },
            { label: "Conviction",      value: convCount,          accent: "var(--amber)" },
            { label: "Accelerating",    value: accelCount,         accent: "var(--teal)"  },
          ].map(({ label, value, accent }) => (
            <div key={label} className="card px-4 py-3 flex flex-col gap-0.5">
              <span className="text-2xs font-bold uppercase tracking-widest" style={{ color: "var(--faint)" }}>{label}</span>
              <span className="text-2xl font-bold font-mono tabular" style={{ color: accent }}>{value}</span>
            </div>
          ))}
        </div>
      )}

      <div className="card overflow-hidden">
        <div className="px-4 py-3 border-b flex items-center justify-between" style={{ borderColor: "var(--border)" }}>
          <h2 className="text-sm font-bold uppercase tracking-widest" style={{ color: "var(--faint)" }}>
            Smart Signal Leaderboard
          </h2>
          <span className="text-xs font-mono" style={{ color: "var(--faint)" }}>
            {signals.length} signals
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[600px]">
            <thead>
              <tr>
                <Th col="ticker"           label="Ticker" />
                <Th col="direction"        label="Direction" />
                <Th col="alert_level"      label="Alert" />
                <Th col="conviction_score" label="Conviction" right />
                <Th col="total_premium"    label="Premium"    right />
                <Th col="trade_count"      label="Trades"     right />
                <Th col="timestamp"        label="Time" />
              </tr>
            </thead>
            <tbody>
              {loading ? <SkeletonRows /> :
               error   ? <tr><td colSpan={7} className="px-4 py-10 text-center text-sm" style={{ color: "var(--red)" }}>⚠ {error}</td></tr> :
               sorted.length === 0 ? (
                <tr><td colSpan={7}>
                  <div className="flex flex-col items-center justify-center py-20 gap-3">
                    <span className="text-3xl" style={{ color: "var(--faint)" }}>◉</span>
                    <p className="text-sm" style={{ color: "var(--muted)" }}>No smart signals detected yet</p>
                  </div>
                </td></tr>
               ) : (
                sorted.map((s, i) => (
                  <tr key={i} className="border-b transition-colors hover:bg-[var(--surface-2)] animate-fade-up"
                      style={{ borderColor: "var(--border)" }}>
                    <td className="px-3 py-3 font-mono font-bold text-sm" style={{ color: "var(--amber)" }}>
                      {s.ticker}
                      {s.is_accelerating && <span className="ml-1 text-xs" style={{ color: "var(--teal)" }}>⚡</span>}
                    </td>
                    <td className="px-3 py-3">
                      <span className="px-2 py-0.5 rounded text-xs font-black uppercase"
                            style={{ color: dirColor(s.direction), background: `${dirColor(s.direction)}18`, border: `1px solid ${dirColor(s.direction)}30` }}>
                        {s.direction}
                      </span>
                    </td>
                    <td className="px-3 py-3">

                      <span className="text-xs font-bold uppercase tracking-wider" style={{ color: alertColor(s.alert_level) }}>
                        {s.alert_level.replace("_", " ")}
                      </span>
                    </td>
                    <td className="px-3 py-3 text-right">
                      <span className="text-sm font-mono font-bold tabular"
                            style={{ color: s.conviction_score >= 0.7 ? "var(--green)" : s.conviction_score >= 0.4 ? "var(--amber)" : "var(--red)" }}>
                        {(s.conviction_score * 100).toFixed(0)}
                      </span>
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-sm tabular" style={{ color: "var(--amber)" }}>
                      {fmt$(s.total_premium)}
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-sm tabular" style={{ color: "var(--muted)" }}>
                      {s.trade_count}
                    </td>
                    <td className="px-3 py-3 font-mono text-xs" style={{ color: "var(--faint)" }}>
                      {new Date(s.timestamp).toLocaleTimeString()}
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
