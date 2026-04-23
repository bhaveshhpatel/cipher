"use client";
import type { CompositeSignal } from "@/lib/api";

interface Props {
  signal:  CompositeSignal | null;
  loading: boolean;
  ticker:  string;
}

function ScoreBar({ label, score, color }: { label: string; score: number; color: string }) {
  const pct = Math.min(Math.max(score * 100, 0), 100);
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--muted)" }}>
          {label}
        </span>
        <span className="text-sm font-mono font-bold tabular" style={{ color }}>
          {pct.toFixed(0)}
        </span>
      </div>
      <div className="h-2 rounded-full overflow-hidden" style={{ background: "var(--border)" }}>
        <div className="h-full rounded-full transition-all duration-700" style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}

const recStyle = (r: string) => {
  if (r === "BUY")       return { color: "var(--green)",  bg: "rgba(26,158,90,0.08)",  border: "rgba(26,158,90,0.2)"  };
  if (r === "SELL")      return { color: "var(--red)",    bg: "rgba(220,53,69,0.08)",  border: "rgba(220,53,69,0.2)"  };
  if (r === "STRONG_BUY")return { color: "var(--teal)",   bg: "rgba(10,155,140,0.08)", border: "rgba(10,155,140,0.2)" };
  return                          { color: "var(--muted)", bg: "var(--surface-2)",      border: "var(--border)"        };
};

function EmptyState({ ticker }: { ticker: string }) {
  return (
    <div className="card flex flex-col items-center justify-center py-20 gap-4">
      <span className="text-4xl" style={{ color: "var(--faint)" }}>◈</span>
      <p className="text-base font-semibold" style={{ color: "var(--muted)" }}>
        No composite signal for {ticker}
      </p>
      <p className="text-sm" style={{ color: "var(--faint)" }}>
        Click "Analyze {ticker}" to run multi-factor scoring.
      </p>
    </div>
  );
}

export function CompositeCard({ signal, loading, ticker }: Props) {
  if (loading) {
    return (
      <div className="card p-6 flex flex-col gap-4">
        {[120, 80, 240, 160].map((w, i) => (
          <div key={i} className="skeleton h-5 rounded" style={{ width: w }} />
        ))}
      </div>
    );
  }

  if (!signal) return <EmptyState ticker={ticker} />;

  const rs = recStyle(signal.recommendation);
  const compositeColor = signal.composite_score >= 0.7 ? "var(--green)"
    : signal.composite_score >= 0.4 ? "var(--amber)" : "var(--red)";

  return (
    <div className="grid gap-4 sm:grid-cols-2">

      {/* Left: Score gauges */}
      <div className="card p-5 flex flex-col gap-4">
        <h3 className="text-sm font-bold uppercase tracking-widest" style={{ color: "var(--faint)" }}>
          Factor Scores · {signal.ticker}
        </h3>
        <ScoreBar label="Composite"    score={signal.composite_score} color={compositeColor} />
        <ScoreBar label="Flow Score"   score={signal.flow_score}      color="var(--amber)" />
        <ScoreBar label="Backtest"     score={signal.backtest_score}  color="var(--teal)" />

        {/* Composite dial */}
        <div className="flex items-center justify-center pt-2">
          <div className="flex flex-col items-center gap-1">
            <span
              className="text-5xl font-black font-mono tabular"
              style={{ color: compositeColor }}
            >
              {(signal.composite_score * 100).toFixed(0)}
            </span>
            <span className="text-xs uppercase tracking-widest" style={{ color: "var(--faint)" }}>
              composite score
            </span>
          </div>
        </div>
      </div>

      {/* Right: Recommendation + reasoning */}
      <div className="card p-5 flex flex-col gap-4">
        <h3 className="text-sm font-bold uppercase tracking-widest" style={{ color: "var(--faint)" }}>
          Recommendation
        </h3>

        {/* Verdict pill */}
        <div
          className="rounded-xl px-5 py-4 flex items-center justify-center"
          style={{ background: rs.bg, border: `1px solid ${rs.border}` }}
        >
          <span className="text-4xl font-black font-mono tracking-tight" style={{ color: rs.color }}>
            {signal.recommendation}
          </span>
        </div>

        {/* Reasoning */}
        <div className="flex flex-col gap-1">
          <span className="text-xs font-bold uppercase tracking-widest" style={{ color: "var(--faint)" }}>
            Reasoning
          </span>
          <p className="text-sm leading-relaxed" style={{ color: "var(--text)" }}>
            {signal.reasoning}
          </p>
        </div>
      </div>
    </div>
  );
}
