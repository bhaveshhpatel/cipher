"use client";
import { A, AdminCard, CardHeader } from "./_shared";

const STEPS = [
  { label: "Symbols",   desc: "CBOE universe filtered by Tradier liquidity screen → tiered watchlist" },
  { label: "Stream",    desc: "Tradier WebSocket delivers live option ticks for all T1/T2/T3 symbols" },
  { label: "Classify",  desc: "Each tick parsed, deduped, and written to flow_events + accumulators" },
  { label: "Signals",   desc: "Composite signal engine reads accumulators → emits smart_signals" },
  { label: "Dashboard", desc: "Frontend polls /api/flow and /api/signals for live UI updates" },
];

export function HowItWorksCard() {
  return (
    <AdminCard>
      <CardHeader title="Pipeline Overview" subtitle="End-to-end data flow" />
      <div className="flex flex-col gap-3">
        {STEPS.map((s, i) => (
          <div key={i} className="flex items-start gap-3">
            <span
              className="text-xs font-mono px-2 py-0.5 rounded shrink-0 mt-0.5"
              style={{ background: A.cyanDim, color: A.cyan, border: `1px solid ${A.cyanBorder}` }}
            >
              {String(i + 1).padStart(2, "0")}
            </span>
            <div>
              <span className="text-xs font-semibold font-mono" style={{ color: A.text }}>
                {s.label}
              </span>
              <span className="text-xs font-mono ml-2" style={{ color: A.muted }}>
                {s.desc}
              </span>
            </div>
          </div>
        ))}
      </div>
    </AdminCard>
  );
}
