"use client";
import { SignalHistory } from "@/components/dashboard/SignalHistory";

interface Props { token: string | null; }

export function SignalHistoryPage({ token }: Props) {
  return (
    <div className="flex flex-col gap-4" data-testid="signal-history-page">
      <div>
        <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Signal History</h1>
        <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
          Persisted composite signals · flow × 0.55 + backtest × 0.35 + volume-premium × 0.10
        </p>
      </div>
      <SignalHistory token={token} />
    </div>
  );
}
