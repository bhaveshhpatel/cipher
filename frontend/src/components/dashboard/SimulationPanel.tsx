"use client";
import type { SimulationResult } from "@/lib/api";

interface Props {
  result:   SimulationResult | null;
  loading:  boolean;
  error:    string | null;
  progress: number;
}

const verdictStyle = (d: string) => {
  if (d === "BUY")  return { color: "var(--green)",  bg: "rgba(26,158,90,0.1)",  border: "rgba(26,158,90,0.25)"  };
  if (d === "SELL") return { color: "var(--red)",    bg: "rgba(220,53,69,0.1)",  border: "rgba(220,53,69,0.25)"  };
  return              { color: "var(--muted)",        bg: "var(--surface-2)",     border: "var(--border)"         };
};

function LoadingState({ progress }: { progress: number }) {
  return (
    <div className="card flex flex-col items-center justify-center py-20 gap-6">
      <span className="text-5xl" style={{ color: "var(--amber)" }}>⬡</span>
      <div className="flex flex-col items-center gap-2 w-64">
        <p className="text-sm font-semibold" style={{ color: "var(--muted)" }}>
          Running AI swarm simulation…
        </p>
        <div className="w-full h-2 rounded-full overflow-hidden" style={{ background: "var(--border)" }}>
          <div
            className="h-full rounded-full transition-all duration-300"
            style={{ width: `${progress}%`, background: "var(--amber)" }}
          />
        </div>
        <span className="text-xs font-mono tabular" style={{ color: "var(--faint)" }}>{progress}%</span>
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="card flex flex-col items-center justify-center py-20 gap-4">
      <span className="text-4xl" style={{ color: "var(--faint)" }}>⬡</span>
      <p className="text-base font-semibold" style={{ color: "var(--muted)" }}>No simulation results yet</p>
      <p className="text-sm" style={{ color: "var(--faint)" }}>Scan flow events for a ticker, then click "Run AI Simulation".</p>
    </div>
  );
}

export function SimulationPanel({ result, loading, error, progress }: Props) {
  if (loading) return <LoadingState progress={progress} />;
  if (error) return (
    <div className="card px-5 py-10 text-center text-sm" style={{ color: "var(--red)" }}>⚠ {error}</div>
  );
  if (!result) return <EmptyState />;

  const total = result.bull_votes + result.bear_votes + result.hold_votes;
  const vStyle = verdictStyle(result.direction);

  return (
    <div className="flex flex-col gap-4">

      {/* Verdict card */}
      <div className="card p-6 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div className="flex flex-col gap-1">
          <span className="text-xs font-bold uppercase tracking-widest" style={{ color: "var(--faint)" }}>
            AI Swarm Verdict · {result.ticker}
          </span>
          <div
            className="text-5xl font-black font-mono tracking-tight"
            style={{ color: vStyle.color }}
          >
            {result.direction}
          </div>
          <p className="text-sm mt-1 max-w-prose" style={{ color: "var(--muted)" }}>{result.summary}</p>
        </div>

        {/* Confidence ring */}
        <div className="flex flex-col items-center gap-1 shrink-0">
          <ConfidenceRing confidence={result.confidence} />
          <span className="text-xs font-mono" style={{ color: "var(--faint)" }}>Confidence</span>
        </div>
      </div>

      {/* Vote bars */}
      <div className="card p-5 flex flex-col gap-3">
        <h3 className="text-sm font-bold uppercase tracking-widest" style={{ color: "var(--faint)" }}>
          Agent Votes
        </h3>
        {[
          { label: "BUY",  count: result.bull_votes, color: "var(--green)" },
          { label: "SELL", count: result.bear_votes, color: "var(--red)" },
          { label: "HOLD", count: result.hold_votes, color: "var(--muted)" },
        ].map(({ label, count, color }) => (
          <div key={label} className="flex items-center gap-3">
            <span className="w-10 text-xs font-bold font-mono" style={{ color }}>{label}</span>
            <div className="flex-1 h-2 rounded-full overflow-hidden" style={{ background: "var(--border)" }}>
              <div
                className="h-full rounded-full transition-all"
                style={{ width: `${total > 0 ? (count/total)*100 : 0}%`, background: color }}
              />
            </div>
            <span className="w-6 text-xs font-mono tabular text-right" style={{ color: "var(--muted)" }}>{count}</span>
          </div>
        ))}
      </div>

      {/* Agent reasoning */}
      {result.agents && result.agents.length > 0 && (
        <div className="card p-5 flex flex-col gap-3">
          <h3 className="text-sm font-bold uppercase tracking-widest" style={{ color: "var(--faint)" }}>
            Agent Reasoning
          </h3>
          <div className="grid gap-3 sm:grid-cols-2">
            {result.agents.map((a, i) => {
              const s = verdictStyle(a.direction);
              return (
                <div key={i}
                  className="rounded-lg p-3 flex flex-col gap-1.5"
                  style={{ background: s.bg, border: `1px solid ${s.border}` }}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-bold uppercase tracking-wider" style={{ color: "var(--muted)" }}>
                      {a.role}
                    </span>
                    <span className="text-xs font-black font-mono" style={{ color: s.color }}>
                      {a.direction}
                    </span>
                  </div>
                  <p className="text-xs leading-relaxed" style={{ color: "var(--text)" }}>{a.reasoning}</p>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function ConfidenceRing({ confidence }: { confidence: number }) {
  const pct  = Math.min(Math.max(confidence, 0), 1);
  const deg  = pct * 360;
  const color = pct >= 0.7 ? "var(--green)" : pct >= 0.4 ? "var(--amber)" : "var(--red)";
  return (
    <div
      className="w-20 h-20 rounded-full flex items-center justify-center relative"
      style={{
        background: `conic-gradient(${color} ${deg}deg, var(--border) ${deg}deg)`,
      }}
    >
      <div
        className="w-14 h-14 rounded-full flex items-center justify-center flex-col"
        style={{ background: "var(--surface)" }}
      >
        <span className="text-lg font-black font-mono tabular" style={{ color }}>
          {(pct * 100).toFixed(0)}
        </span>
        <span className="text-2xs" style={{ color: "var(--faint)" }}>%</span>
      </div>
    </div>
  );
}
