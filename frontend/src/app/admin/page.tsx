"use client";
import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/hooks/useAuth";
import { useAdminDemo } from "@/hooks/useAdminDemo";

interface ConfigRow {
  key:         string;
  value:       string;
  value_type:  string;
  description: string;
  updated_at:  string;
  updated_by:  string | null;
}

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
      <div className="rounded-xl p-6 max-w-xl mb-6"
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

      {/* Ingestion Config Card */}
      <IngestionConfigCard token={token} />

    </div>
  );
}

function IngestionConfigCard({ token }: { token: string | null }) {
  const [rows,     setRows]     = useState<ConfigRow[]>([]);
  const [edits,    setEdits]    = useState<Record<string, string>>({});
  const [saving,   setSaving]   = useState<Record<string, boolean>>({});
  const [saved,    setSaved]    = useState<Record<string, boolean>>({});
  const [errors,   setErrors]   = useState<Record<string, string>>({});
  const [loading,  setLoading]  = useState(true);
  const [fetchErr, setFetchErr] = useState<string | null>(null);

  const fetchConfig = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setFetchErr(null);
    try {
      const res = await fetch("/api/admin/ingestion/config", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setRows(data.config ?? []);
      const initial: Record<string, string> = {};
      for (const row of (data.config ?? [])) initial[row.key] = row.value;
      setEdits(initial);
    } catch (e: unknown) {
      setFetchErr(e instanceof Error ? e.message : "Failed to load config");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { fetchConfig(); }, [fetchConfig]);

  const handleSave = async (key: string) => {
    if (!token) return;
    setSaving(s => ({ ...s, [key]: true }));
    setErrors(e => ({ ...e, [key]: "" }));
    setSaved(s => ({ ...s, [key]: false }));
    try {
      const res = await fetch("/api/admin/ingestion/config", {
        method:  "PATCH",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body:    JSON.stringify({ key, value: edits[key] }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`);
      }
      setSaved(s => ({ ...s, [key]: true }));
      await fetchConfig();
      setTimeout(() => setSaved(s => ({ ...s, [key]: false })), 2500);
    } catch (e: unknown) {
      setErrors(er => ({ ...er, [key]: e instanceof Error ? e.message : "Save failed" }));
    } finally {
      setSaving(s => ({ ...s, [key]: false }));
    }
  };

  const isDirty = (key: string, currentValue: string) =>
    edits[key] !== undefined && edits[key] !== currentValue;

  return (
    <div className="rounded-xl p-6 max-w-xl"
         style={{ border: "1px solid var(--border)", background: "var(--surface)" }}>

      <div className="flex items-center justify-between mb-1">
        <div>
          <h2 className="text-lg font-semibold font-mono">Ingestion Config</h2>
          <p className="text-xs mt-1 font-mono" style={{ color: "var(--muted)" }}>
            Layer 1 OCC registry + universe pipeline knobs. Changes take effect on next registry
            refresh — no restart needed.
          </p>
        </div>
        <button
          onClick={fetchConfig}
          className="text-xs font-mono ml-4 transition-colors"
          style={{ color: "var(--muted)" }}
        >
          ↻ Refresh
        </button>
      </div>

      <div className="mt-5">
        {loading && (
          <p className="text-xs font-mono" style={{ color: "var(--muted)" }}>Loading…</p>
        )}
        {fetchErr && (
          <p className="text-xs font-mono p-3 rounded"
             style={{ color: "var(--red)", background: "rgba(220,53,69,0.1)", border: "1px solid rgba(220,53,69,0.2)" }}>
            {fetchErr}
          </p>
        )}

        {!loading && rows.length > 0 && (
          <div className="space-y-3">
            {rows.map(row => (
              <div key={row.key}
                   className="rounded-lg p-4"
                   style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>

                <div className="flex items-start justify-between gap-2 mb-2">
                  <div>
                    <p className="text-xs font-mono font-semibold" style={{ color: "var(--text)" }}>
                      {row.key}
                    </p>
                    {row.description && (
                      <p className="text-xs font-mono mt-0.5" style={{ color: "var(--muted)" }}>
                        {row.description}
                      </p>
                    )}
                  </div>
                  <span className="text-xs font-mono px-2 py-0.5 rounded shrink-0"
                        style={{ background: "var(--surface)", color: "var(--muted)", border: "1px solid var(--border)" }}>
                    {row.value_type}
                  </span>
                </div>

                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={edits[row.key] ?? row.value}
                    onChange={e => setEdits(prev => ({ ...prev, [row.key]: e.target.value }))}
                    onKeyDown={e => { if (e.key === "Enter") handleSave(row.key); }}
                    className="flex-1 px-3 py-1.5 rounded text-sm font-mono"
                    style={{
                      background: "var(--bg)",
                      border: `1px solid ${
                        errors[row.key]              ? "rgba(220,53,69,0.6)" :
                        isDirty(row.key, row.value)  ? "rgba(250,204,21,0.5)" :
                        "var(--border)"
                      }`,
                      color:   "var(--text)",
                      outline: "none",
                    }}
                  />
                  <button
                    onClick={() => handleSave(row.key)}
                    disabled={saving[row.key] || !isDirty(row.key, row.value)}
                    className="px-4 py-1.5 rounded text-xs font-mono font-semibold transition-colors"
                    style={{
                      minWidth:   "62px",
                      background:
                        saved[row.key]               ? "rgba(74,222,128,0.15)" :
                        saving[row.key]              ? "var(--border)" :
                        !isDirty(row.key, row.value) ? "var(--border)" :
                        "rgba(99,102,241,0.8)",
                      color:
                        saved[row.key]               ? "rgb(74,222,128)" :
                        !isDirty(row.key, row.value) ? "var(--muted)" :
                        "#fff",
                      cursor:  saving[row.key] || !isDirty(row.key, row.value) ? "not-allowed" : "pointer",
                      border:  saved[row.key] ? "1px solid rgba(74,222,128,0.3)" : "1px solid transparent",
                    }}
                  >
                    {saved[row.key] ? "✓ Saved" : saving[row.key] ? "Saving…" : "Save"}
                  </button>
                </div>

                {errors[row.key] && (
                  <p className="text-xs font-mono mt-1.5" style={{ color: "var(--red)" }}>
                    {errors[row.key]}
                  </p>
                )}

                <p className="text-xs font-mono mt-2" style={{ color: "var(--muted)", opacity: 0.55 }}>
                  Updated {new Date(row.updated_at).toLocaleString()}
                  {row.updated_by ? ` by ${row.updated_by}` : ""}
                </p>
              </div>
            ))}
          </div>
        )}
      </div>

      <p className="text-xs font-mono mt-5" style={{ color: "var(--muted)", opacity: 0.45 }}>
        ⚡ Changes propagate to the OCC registry within REGISTRY_REFRESH_MINS minutes (next scheduled rebuild).
      </p>
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
