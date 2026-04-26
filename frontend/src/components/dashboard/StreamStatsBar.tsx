"use client";
import type { StreamStats } from "@/lib/api";

interface Props { stats: StreamStats; }

function StatItem({ label, value, accent }: { label: string; value: string | number; accent?: string }) {
  return (
    <div className="flex flex-col items-center px-4 py-2 gap-0.5">
      <span
        className="text-lg font-bold font-mono tabular leading-tight"
        style={{ color: accent ?? "var(--text)" }}
      >
        {typeof value === "number" ? value.toLocaleString() : value}
      </span>
      <span className="text-2xs font-semibold uppercase tracking-widest" style={{ color: "var(--faint)" }}>
        {label}
      </span>
    </div>
  );
}

export function StreamStatsBar({ stats }: Props) {
  const classifyRate = stats.ticks > 0
    ? ((stats.classified / stats.ticks) * 100).toFixed(1) + "%"
    : "—";

  return (
    <div className="flex items-center justify-center flex-wrap divide-x divide-[var(--border)]">
      <StatItem label="Active Symbols" value={stats.active_symbols} accent="var(--amber)" />
      <StatItem label="Ticks"          value={stats.ticks} />
      <StatItem label="Classified"     value={stats.classified} />
      <StatItem label="Classify Rate"  value={classifyRate} accent="var(--teal)" />
      <StatItem label="Signals"        value={stats.signals} accent="var(--blue)" />
      {stats.errors > 0 && (
        <StatItem label="Errors" value={stats.errors} accent="var(--red)" />
      )}
    </div>
  );
}
