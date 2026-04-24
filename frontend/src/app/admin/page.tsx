"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAdminDemo } from "@/hooks/useAdminDemo";

const ADMIN_EMAIL = "bhaveshhpatel@yahoo.com";

export default function AdminPage() {
  const router = useRouter();
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const { status, loading, error, toggle } = useAdminDemo(token);

  useEffect(() => {
    const t = localStorage.getItem("cipher_token");
    const e = localStorage.getItem("cipher_email");
    if (!t || e !== ADMIN_EMAIL) {
      router.replace("/dashboard");
      return;
    }
    setToken(t);
    setEmail(e);
  }, [router]);

  const isRunning = status?.demo?.running ?? false;

  return (
    <div className="min-h-screen bg-[var(--surface-0)] text-[var(--text-primary)] p-8">
      {/* Header */}
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight font-mono">⚙️ Admin Panel</h1>
          <p className="text-[var(--text-muted)] text-sm mt-1">{email}</p>
        </div>
        <button
          onClick={() => router.push("/dashboard")}
          className="text-sm text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors"
        >
          ← Back to Dashboard
        </button>
      </div>

      {/* Demo Engine Card */}
      <div className="rounded-xl border border-[var(--border)] bg-[var(--surface-1)] p-6 max-w-xl">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-lg font-semibold font-mono">Demo Engine</h2>
            <p className="text-xs text-[var(--text-muted)] mt-1">
              Emits realistic Tradier timesale events through the full 6-layer pipeline
            </p>
          </div>
          {/* Status badge */}
          <span
            className={`text-xs font-mono px-3 py-1 rounded-full border ${
              isRunning
                ? "border-green-500/40 bg-green-500/10 text-green-400"
                : "border-[var(--border)] bg-[var(--surface-0)] text-[var(--text-muted)]"
            }`}
          >
            {isRunning ? "● RUNNING" : "○ STOPPED"}
          </span>
        </div>

        {/* Toggle */}
        <div className="flex items-center gap-4 mb-6">
          <button
            disabled={loading || isRunning}
            onClick={() => toggle(true)}
            className="px-5 py-2 rounded-lg text-sm font-mono font-semibold 
                       bg-green-600 hover:bg-green-500 disabled:opacity-40 
                       disabled:cursor-not-allowed transition-colors"
          >
            {loading && !isRunning ? "Starting…" : "▶ Start Demo"}
          </button>
          <button
            disabled={loading || !isRunning}
            onClick={() => toggle(false)}
            className="px-5 py-2 rounded-lg text-sm font-mono font-semibold 
                       bg-red-700 hover:bg-red-600 disabled:opacity-40 
                       disabled:cursor-not-allowed transition-colors"
          >
            {loading && isRunning ? "Stopping…" : "■ Stop Demo"}
          </button>
        </div>

        {/* Stats */}
        {status?.demo && (
          <div className="grid grid-cols-2 gap-3">
            <Stat label="Ticks Emitted"    value={status.demo.ticks_emitted} />
            <Stat label="Signals Generated" value={status.demo.signals_generated} />
            <Stat label="Last Ticker"      value={status.demo.last_ticker ?? "—"} />
            <Stat label="Stream Mode"      value={String(status.stream?.mode ?? "—")} />
          </div>
        )}

        {/* Error */}
        {error && (
          <p className="mt-4 text-xs text-red-400 font-mono bg-red-500/10 
                        border border-red-500/20 rounded p-3">
            {error}
          </p>
        )}
      </div>

      {/* What demo mode does */}
      <div className="mt-6 max-w-xl rounded-xl border border-[var(--border)] 
                      bg-[var(--surface-1)] p-6">
        <h3 className="text-sm font-semibold font-mono mb-3 text-[var(--text-muted)]">How It Works</h3>
        <ul className="text-xs text-[var(--text-muted)] space-y-2 font-mono">
          <li>① Generates OCC symbol strings (e.g. TSLA  260620C00245000)</li>
          <li>② Emits timesale envelopes — exact Tradier format ("last" field for fill)</li>
          <li>③ Multi-exchange: same trade on 2-4 exchanges (N/C/M/Q) within 200ms</li>
          <li>④ Goes through Layer 3 parser → Layer 4 dedup → accumulator → composite</li>
          <li>⑤ Writes to flow_events + flow_episodes + signal_history in Supabase</li>
          <li>⑥ Broadcasts to WebSocket clients via Supabase Realtime</li>
        </ul>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg bg-[var(--surface-0)] border border-[var(--border)] p-3">
      <p className="text-xs text-[var(--text-muted)] font-mono mb-1">{label}</p>
      <p className="text-sm font-semibold font-mono text-[var(--text-primary)]">
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
    </div>
  );
}
