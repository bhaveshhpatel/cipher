"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/hooks/useAuth";
import { useAdminDemo } from "@/hooks/useAdminDemo";

export default function AdminPage() {
  const router = useRouter();
  const { token, email, isAdmin, isAuthenticated, ready } = useAuth();
  const { status, isRunning, loading, error, toggle } = useAdminDemo(token);

  useEffect(() => {
    if (!ready) return;
    if (!isAuthenticated) { router.replace("/");         return; }
    if (!isAdmin)         { router.replace("/dashboard"); return; }
  }, [ready, isAuthenticated, isAdmin, router]);

  if (!ready || !isAdmin) return null;

  return (
    <div className="min-h-screen p-8" style={{ background: "var(--bg)", color: "var(--text)" }}>

      {/* Header */}
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight font-mono">⚙️ Admin Panel</h1>
          <p className="text-sm mt-1 font-mono" style={{ color: "var(--muted)" }}>{email}</p>
        </div>
        <button
          onClick={() => router.push("/dashboard")}
          className="text-sm transition-colors font-mono"
          style={{ color: "var(--muted)" }}
        >
          ← Back to Dashboard
        </button>
      </div>

      {/* Demo Engine Card */}
      <div className="rounded-xl p-6 max-w-xl mb-6"
           style={{ border: "1px solid var(--border)", background: "var(--surface)" }}>

        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-lg font-semibold font-mono">Demo Engine</h2>
            <p className="text-xs mt-1 font-mono" style={{ color: "var(--muted)" }}>
              Emits realistic Tradier timesale events through the full 6-layer pipeline
            </p>
          </div>
          <span
            className="text-xs font-mono px-3 py-1 rounded-full"
            style={{
              border:     isRunning ? "1px solid rgba(74,222,128,0.4)"  : "1px solid var(--border)",
              background: isRunning ? "rgba(74,222,128,0.1)"            : "var(--surface-2)",
              color:      isRunning ? "rgb(74,222,128)"                 : "var(--muted)",
            }}
          >
            {status === null ? "LOADING…" : isRunning ? "● RUNNING" : "○ STOPPED"}
          </span>
        </div>

        {/* Toggle buttons */}
        <div className="flex items-center gap-4 mb-6">
          <button
            disabled={loading || isRunning || status === null}
            onClick={() => toggle(true)}
            className="px-5 py-2 rounded-lg text-sm font-mono font-semibold transition-colors"
            style={{
              background: loading || isRunning || status === null ? "var(--border)" : "#16a34a",
              color:      loading || isRunning || status === null ? "var(--muted)"  : "#fff",
              cursor:     loading || isRunning || status === null ? "not-allowed"   : "pointer",
            }}
          >
            {loading && !isRunning ? "Starting…" : "▶ Start Demo"}
          </button>

          <button
            disabled={loading || !isRunning || status === null}
            onClick={() => toggle(false)}
            className="px-5 py-2 rounded-lg text-sm font-mono font-semibold transition-colors"
            style={{
              background: loading || !isRunning || status === null ? "var(--border)" : "#b91c1c",
              color:      loading || !isRunning || status === null ? "var(--muted)"  : "#fff",
              cursor:     loading || !isRunning || status === null ? "not-allowed"   : "pointer",
            }}
          >
            {loading && isRunning ? "Stopping…" : "■ Stop Demo"}
          </button>
        </div>

        {/* Stats */}
        {status?.demo && (
          <div className="grid grid-cols-2 gap-3">
            <Stat label="Ticks Emitted"     value={status.demo.ticks_emitted} />
            <Stat label="Signals Generated" value={status.demo.signals_generated} />
            <Stat label="Last Ticker"       value={status.demo.last_ticker ?? "—"} />
            <Stat label="Started At"        value={
              status.demo.started_at
                ? new Date(status.demo.started_at + "Z").toLocaleTimeString()
                : "—"
            } />
          </div>
        )}

        {error && (
          <p className="mt-4 text-xs font-mono p-3 rounded"
             style={{ color: "var(--red)", background: "rgba(220,53,69,0.1)", border: "1px solid rgba(220,53,69,0.2)" }}>
            {error}
          </p>
        )}
      </div>

      {/* How it works */}
      <div className="rounded-xl p-6 max-w-xl"
           style={{ border: "1px solid var(--border)", background: "var(--surface)" }}>
        <h3 className="text-sm font-semibold font-mono mb-3" style={{ color: "var(--muted)" }}>How It Works</h3>
        <ul className="text-xs space-y-2 font-mono" style={{ color: "var(--muted)" }}>
          <li>① Generates OCC symbol strings (e.g. TSLA  260620C00245000)</li>
          <li>② Emits timesale envelopes — exact Tradier format ("last" field for fill)</li>
          <li>③ Multi-exchange: same trade on 2-4 exchanges (N/C/M/Q) within 200ms</li>
          <li>④ Layer 3 parser → Layer 4 dedup → accumulator → composite signal</li>
          <li>⑤ Writes to flow_events + flow_episodes + signal_history in Supabase</li>
          <li>⑥ Broadcasts to WebSocket clients — appears live in dashboard</li>
        </ul>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg p-3"
         style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
      <p className="text-xs font-mono mb-1" style={{ color: "var(--muted)" }}>{label}</p>
      <p className="text-sm font-semibold font-mono" style={{ color: "var(--text)" }}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
    </div>
  );
}
