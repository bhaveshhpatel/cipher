"use client";
import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/hooks/useAuth";
import { useAdminDemo } from "@/hooks/useAdminDemo";

/* ─── Types ──────────────────────────────────────────────── */

interface ConfigRow {
  key:         string;
  value:       string;
  value_type:  string;
  description: string;
  updated_at:  string;
  updated_by:  string | null;
}

interface TierThresholdsRow {
  id:                 number;
  updated_at:         string;
  updated_by:         string | null;
  is_active:          boolean;
  t1_min_volume:      number;
  t1_min_last_price:  number;
  t1_min_oi:          number;
  t1_atm_pct:         number;
  t1_max_dte:         number;
  t2_min_volume:      number;
  t2_min_last_price:  number;
  t2_min_oi:          number;
  t2_atm_pct:         number;
  t2_max_dte:         number;
  t3_min_volume:      number;
  t3_min_last_price:  number;
  t3_min_oi:          number;
  t3_atm_pct:         number;
  t3_max_dte:         number;
}

interface CacheMeta {
  warm:        boolean;
  age_seconds: number | null;
  ttl_seconds: number;
}

interface StreamHealth {
  mode:              string;
  active_symbols:    number;
  ticks:             number;
  classified:        number;
  deduped:           number;
  signals:           number;
  errors:            number;
  reconnects:        number;
  last_tick_at:      string | null;
  last_reconnect_at: string | null;
  uptime_seconds:    number;
}

/* 4A-OI: tier-distribution response types */
interface TierDistributionSample {
  symbol:         string;
  open_interest:  number | null;
}

interface TierDistributionTier {
  count:   number;
  samples: TierDistributionSample[];
}

interface TierDistribution {
  snapshot_id: string;
  total:       number;
  tiers: {
    "1": TierDistributionTier;
    "2": TierDistributionTier;
    "3": TierDistributionTier;
  };
}

/* ─── Admin palette ──────────────────────────────────────── */
const A = {
  bg:          "#080c14",
  surface:     "#0e1422",
  surface2:    "#131927",
  border:      "#1e2d45",
  border2:     "#263754",
  text:        "#dce6f5",
  muted:       "#6b83a6",
  faint:       "#364d6b",
  cyan:        "#22d3ee",
  cyanDim:     "rgba(34,211,238,0.12)",
  cyanBorder:  "rgba(34,211,238,0.25)",
  indigo:      "#818cf8",
  indigoDim:   "rgba(129,140,248,0.12)",
  indigoBorder:"rgba(129,140,248,0.25)",
  amber:       "#fbbf24",
  amberDim:    "rgba(251,191,36,0.10)",
  amberBorder: "rgba(251,191,36,0.25)",
  green:       "rgb(74,222,128)",
  greenDim:    "rgba(74,222,128,0.10)",
  greenBorder: "rgba(74,222,128,0.25)",
  red:         "#f87171",
  redDim:      "rgba(248,113,113,0.10)",
  redBorder:   "rgba(248,113,113,0.25)",
};

/* ─── Shared sub-components ──────────────────────────────── */

function AdminCard({
  children,
  className = "",
  style = {},
}: {
  children: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <div
      className={`rounded-xl p-6 ${className}`}
      style={{
        background: A.surface,
        border: `1px solid ${A.border}`,
        ...style,
      }}
    >
      {children}
    </div>
  );
}

function CardHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between mb-5 gap-3">
      <div>
        <h2
          className="text-base font-semibold font-mono tracking-tight"
          style={{ color: A.text }}
        >
          {title}
        </h2>
        {subtitle && (
          <p className="text-xs mt-1 font-mono leading-relaxed" style={{ color: A.muted }}>
            {subtitle}
          </p>
        )}
      </div>
      {action}
    </div>
  );
}

function StatusPill({
  on,
  onLabel = "● RUNNING",
  offLabel = "○ STOPPED",
  loading = false,
}: {
  on: boolean;
  onLabel?: string;
  offLabel?: string;
  loading?: boolean;
}) {
  return (
    <span
      className="text-xs font-mono px-3 py-1 rounded-full shrink-0"
      style={{
        border:     on ? `1px solid ${A.greenBorder}` : `1px solid ${A.border}`,
        background: on ? A.greenDim : A.surface2,
        color:      on ? A.green    : A.muted,
      }}
    >
      {loading ? "LOADING…" : on ? onLabel : offLabel}
    </span>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div
      className="rounded-lg p-3"
      style={{ background: A.surface2, border: `1px solid ${A.border}` }}
    >
      <p className="text-xs font-mono mb-1" style={{ color: A.muted }}>{label}</p>
      <p className="text-sm font-semibold font-mono tabular" style={{ color: A.text }}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
    </div>
  );
}

function FieldInput({
  value,
  onChange,
  onEnter,
  dirty,
  error,
}: {
  value: string;
  onChange: (v: string) => void;
  onEnter: () => void;
  dirty: boolean;
  error: string;
}) {
  return (
    <input
      type="text"
      value={value}
      onChange={e => onChange(e.target.value)}
      onKeyDown={e => { if (e.key === "Enter") onEnter(); }}
      className="flex-1 px-3 py-1.5 rounded text-sm font-mono"
      style={{
        background: A.bg,
        border: `1px solid ${error ? A.redBorder : dirty ? A.amberBorder : A.border}`,
        color: A.text,
        outline: "none",
      }}
    />
  );
}

function SaveBtn({
  onClick,
  saving,
  saved,
  dirty,
}: {
  onClick: () => void;
  saving: boolean;
  saved: boolean;
  dirty: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={saving || !dirty}
      className="px-4 py-1.5 rounded text-xs font-mono font-semibold transition-colors"
      style={{
        minWidth: "64px",
        background: saved    ? A.greenDim
                  : saving   ? A.surface2
                  : !dirty   ? A.surface2
                  : A.indigoDim,
        color:  saved    ? A.green
              : !dirty   ? A.muted
              : A.indigo,
        border: saved  ? `1px solid ${A.greenBorder}`
              : !dirty ? `1px solid ${A.border}`
              : `1px solid ${A.indigoBorder}`,
        cursor: saving || !dirty ? "not-allowed" : "pointer",
      }}
    >
      {saved ? "✓ Saved" : saving ? "Saving…" : "Save"}
    </button>
  );
}

/* ─── Page ───────────────────────────────────────────────── */

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
    <div className="min-h-screen" style={{ background: A.bg, color: A.text }}>

      {/* ── Top bar ────────────────────────────────────────── */}
      <div
        className="sticky top-0 z-10 flex items-center justify-between px-8 py-4"
        style={{
          background: `${A.surface}cc`,
          borderBottom: `1px solid ${A.border}`,
          backdropFilter: "blur(12px)",
        }}
      >
        <div className="flex items-center gap-3">
          <span
            className="text-xs font-mono px-2 py-0.5 rounded"
            style={{ background: A.cyanDim, color: A.cyan, border: `1px solid ${A.cyanBorder}` }}
          >
            ADMIN
          </span>
          <h1 className="text-sm font-semibold font-mono tracking-wide" style={{ color: A.text }}>
            Cipher Control Panel
          </h1>
        </div>
        <div className="flex items-center gap-4">
          <span className="text-xs font-mono" style={{ color: A.muted }}>{email}</span>
          <button
            onClick={() => router.push("/dashboard")}
            className="text-xs font-mono transition-colors px-3 py-1.5 rounded"
            style={{
              color: A.muted,
              border: `1px solid ${A.border}`,
              background: A.surface2,
            }}
          >
            ← Dashboard
          </button>
        </div>
      </div>

      {/* ── Body ───────────────────────────────────────────── */}
      <div className="p-8">

        {/* Row 1: Demo Engine (left) + Stream Health (right) */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
          <DemoEngineCard
            status={status}
            isRunning={isRunning}
            loading={loading}
            error={error}
            toggle={toggle}
          />
          <StreamHealthCard token={token} />
        </div>

        {/* Row 2: Tier Thresholds (left) + Ingestion Config (right) */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
          <TierThresholdsCard token={token} />
          <IngestionConfigCard token={token} />
        </div>

        {/* Row 3: Pipeline Overview (full width) */}
        <HowItWorksCard />

        {/* Row 4: Tier Distribution — avg chain OI per symbol (4A-OI) */}
        <div className="mt-6">
          <TierDistributionCard token={token} />
        </div>
      </div>
    </div>
  );
}

/* ─── Stream Health card (B-008) ─────────────────────────── */

const MODE_COLOR: Record<string, { color: string; bg: string; border: string }> = {
  live:          { color: A.green,  bg: A.greenDim,  border: A.greenBorder },
  demo:          { color: A.cyan,   bg: A.cyanDim,   border: A.cyanBorder },
  starting:      { color: A.amber,  bg: A.amberDim,  border: A.amberBorder },
  reconnecting:  { color: A.amber,  bg: A.amberDim,  border: A.amberBorder },
  market_closed: { color: A.muted,  bg: A.surface2,  border: A.border },
  idle:          { color: A.muted,  bg: A.surface2,  border: A.border },
};

function fmtUptime(secs: number): string {
  if (secs < 60)   return `${Math.floor(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${Math.floor(secs % 60)}s`;
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return `${h}h ${m}m`;
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString();
}

function StreamHealthCard({ token }: { token: string | null }) {
  const [health,   setHealth]   = useState<StreamHealth | null>(null);
  const [loading,  setLoading]  = useState(true);
  const [fetchErr, setFetchErr] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const fetchHealth = useCallback(async () => {
    if (!token) return;
    setFetchErr(null);
    try {
      const res = await fetch("/api/health/stream", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setHealth(await res.json());
      setLastRefresh(new Date());
    } catch (e: unknown) {
      setFetchErr(e instanceof Error ? e.message : "Failed to fetch stream health");
    } finally {
      setLoading(false);
    }
  }, [token]);

  // Initial fetch + auto-refresh every 10s
  useEffect(() => {
    fetchHealth();
    const id = setInterval(fetchHealth, 10_000);
    return () => clearInterval(id);
  }, [fetchHealth]);

  const mode     = health?.mode ?? "unknown";
  const modeC    = MODE_COLOR[mode] ?? { color: A.muted, bg: A.surface2, border: A.border };
  const isLive   = mode === "live" || mode === "demo";

  return (
    <AdminCard>
      <div className="flex items-start justify-between mb-5">
        <div>
          <h2 className="text-base font-semibold font-mono tracking-tight" style={{ color: A.text }}>
            Stream Health
          </h2>
          <p className="text-xs mt-1 font-mono" style={{ color: A.muted }}>
            Live pipeline counters — auto-refreshes every 10s
          </p>
        </div>
        <div className="flex items-center gap-2">
          {health && (
            <span
              className="text-xs font-mono px-3 py-1 rounded-full"
              style={{
                background: modeC.bg,
                border:     `1px solid ${modeC.border}`,
                color:      modeC.color,
                textTransform: "uppercase",
              }}
            >
              ● {mode}
            </span>
          )}
          <button
            onClick={fetchHealth}
            className="text-xs font-mono px-2 py-1 rounded transition-colors"
            style={{ color: A.muted, border: `1px solid ${A.border}`, background: A.surface2 }}
          >
            ↻
          </button>
        </div>
      </div>

      {loading && (
        <p className="text-xs font-mono" style={{ color: A.muted }}>Loading…</p>
      )}
      {fetchErr && (
        <p
          className="text-xs font-mono p-3 rounded"
          style={{ color: A.red, background: A.redDim, border: `1px solid ${A.redBorder}` }}
        >
          {fetchErr}
        </p>
      )}

      {health && !loading && (
        <>
          {/* Counter grid */}
          <div className="grid grid-cols-3 gap-3 mb-4">
            <Stat label="Active Symbols" value={health.active_symbols} />
            <Stat label="Ticks"          value={health.ticks} />
            <Stat label="Classified"     value={health.classified} />
            <Stat label="Deduped"        value={health.deduped} />
            <Stat label="Signals"        value={health.signals} />
            <Stat label="Errors"         value={health.errors} />
          </div>
          <div className="grid grid-cols-2 gap-3 mb-4">
            <Stat label="Reconnects"   value={health.reconnects} />
            <Stat label="Uptime"       value={fmtUptime(health.uptime_seconds)} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Stat label="Last Tick"      value={fmtTime(health.last_tick_at)} />
            <Stat label="Last Reconnect" value={fmtTime(health.last_reconnect_at)} />
          </div>
          {lastRefresh && (
            <p className="text-xs font-mono mt-3" style={{ color: A.faint }}>
              Last refreshed: {lastRefresh.toLocaleTimeString()}
            </p>
          )}
        </>
      )}
    </AdminCard>
  );
}

/* ─── Demo Engine card ───────────────────────────────────── */

function DemoEngineCard({
  status,
  isRunning,
  loading,
  error,
  toggle,
}: {
  status:    ReturnType<typeof useAdminDemo>["status"];
  isRunning: boolean;
  loading:   boolean;
  error:     string | null;
  toggle:    (on: boolean) => void;
}) {
  return (
    <AdminCard>
      <CardHeader
        title="Demo Engine"
        subtitle="Simulated flow for UI testing when markets are closed"
        action={
          <StatusPill on={isRunning} loading={loading} />
        }
      />

      {error && (
        <p
          className="text-xs font-mono p-3 rounded mb-4"
          style={{ color: A.red, background: A.redDim, border: `1px solid ${A.redBorder}` }}
        >
          {error}
        </p>
      )}

      {status && (
        <div className="grid grid-cols-2 gap-3 mb-5">
          <Stat label="Ticks Emitted"     value={status.demo.ticks_emitted} />
          <Stat label="Signals Generated" value={status.demo.signals_generated} />
          <Stat label="Last Ticker"       value={status.demo.last_ticker ?? "—"} />
          <Stat
            label="Started At"
            value={status.demo.started_at
              ? new Date(status.demo.started_at).toLocaleTimeString()
              : "—"}
          />
        </div>
      )}

      <div className="flex gap-3">
        <button
          onClick={() => toggle(true)}
          disabled={isRunning || loading}
          className="flex-1 py-2 rounded text-xs font-mono font-semibold transition-colors"
          style={{
            background: isRunning ? A.surface2 : A.greenDim,
            color:      isRunning ? A.muted    : A.green,
            border:     isRunning ? `1px solid ${A.border}` : `1px solid ${A.greenBorder}`,
            cursor:     isRunning || loading ? "not-allowed" : "pointer",
          }}
        >
          ▶ Start
        </button>
        <button
          onClick={() => toggle(false)}
          disabled={!isRunning || loading}
          className="flex-1 py-2 rounded text-xs font-mono font-semibold transition-colors"
          style={{
            background: !isRunning ? A.surface2 : A.redDim,
            color:      !isRunning ? A.muted    : A.red,
            border:     !isRunning ? `1px solid ${A.border}` : `1px solid ${A.redBorder}`,
            cursor:     !isRunning || loading ? "not-allowed" : "pointer",
          }}
        >
          ■ Stop
        </button>
      </div>
    </AdminCard>
  );
}

/* ─── Tier Thresholds card ───────────────────────────────── */

function TierThresholdsCard({ token }: { token: string | null }) {
  const [row,     setRow]     = useState<TierThresholdsRow | null>(null);
  const [loading, setLoading] = useState(true);
  const [err,     setErr]     = useState<string | null>(null);
  const [drafts,  setDrafts]  = useState<Record<string, string>>({});
  const [saving,  setSaving]  = useState<Record<string, boolean>>({});
  const [saved,   setSaved]   = useState<Record<string, boolean>>({});
  const [errors,  setErrors]  = useState<Record<string, string>>({});

  const fetch_ = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch("/api/admin/tier-thresholds", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setRow(await res.json());
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to load tier thresholds");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { fetch_(); }, [fetch_]);

  const save = useCallback(async (field: string) => {
    if (!token || !row) return;
    const raw = drafts[field];
    const num = Number(raw);
    if (isNaN(num)) {
      setErrors(p => ({ ...p, [field]: "Must be a number" }));
      return;
    }
    setSaving(p => ({ ...p, [field]: true }));
    try {
      const res = await fetch(`/api/admin/tier-thresholds/${field}`, {
        method: "PATCH",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ value: num }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setRow(await res.json());
      setDrafts(p => { const n = { ...p }; delete n[field]; return n; });
      setSaved(p => ({ ...p, [field]: true }));
      setTimeout(() => setSaved(p => ({ ...p, [field]: false })), 2000);
    } catch (e: unknown) {
      setErrors(p => ({ ...p, [field]: e instanceof Error ? e.message : "Save failed" }));
    } finally {
      setSaving(p => ({ ...p, [field]: false }));
    }
  }, [token, row, drafts]);

  const TIER_FIELDS: { label: string; field: keyof TierThresholdsRow }[][] = [
    [
      { label: "T1 Min Volume",     field: "t1_min_volume" },
      { label: "T1 Min Last Price", field: "t1_min_last_price" },
      { label: "T1 Min OI",         field: "t1_min_oi" },
      { label: "T1 ATM %",          field: "t1_atm_pct" },
      { label: "T1 Max DTE",        field: "t1_max_dte" },
    ],
    [
      { label: "T2 Min Volume",     field: "t2_min_volume" },
      { label: "T2 Min Last Price", field: "t2_min_last_price" },
      { label: "T2 Min OI",         field: "t2_min_oi" },
      { label: "T2 ATM %",          field: "t2_atm_pct" },
      { label: "T2 Max DTE",        field: "t2_max_dte" },
    ],
    [
      { label: "T3 Min Volume",     field: "t3_min_volume" },
      { label: "T3 Min Last Price", field: "t3_min_last_price" },
      { label: "T3 Min OI",         field: "t3_min_oi" },
      { label: "T3 ATM %",          field: "t3_atm_pct" },
      { label: "T3 Max DTE",        field: "t3_max_dte" },
    ],
  ];

  return (
    <AdminCard>
      <CardHeader title="Tier Thresholds" subtitle="Screening parameters for T1 / T2 / T3 classification" />
      {loading && <p className="text-xs font-mono" style={{ color: A.muted }}>Loading…</p>}
      {err    && <p className="text-xs font-mono" style={{ color: A.red }}>{err}</p>}
      {row && !loading && (
        <div className="space-y-4">
          {TIER_FIELDS.map((tierFields, ti) => (
            <div key={ti}>
              <p className="text-xs font-mono mb-2" style={{ color: A.cyan }}>Tier {ti + 1}</p>
              <div className="space-y-2">
                {tierFields.map(({ label, field }) => {
                  const current = String(row[field]);
                  const draft   = drafts[field] ?? current;
                  const dirty   = draft !== current;
                  const errMsg  = errors[field] ?? "";
                  return (
                    <div key={field} className="flex items-center gap-2">
                      <span className="text-xs font-mono w-36 shrink-0" style={{ color: A.muted }}>
                        {label}
                      </span>
                      <FieldInput
                        value={draft}
                        onChange={v => {
                          setDrafts(p => ({ ...p, [field]: v }));
                          setErrors(p => { const n = { ...p }; delete n[field]; return n; });
                        }}
                        onEnter={() => save(field as string)}
                        dirty={dirty}
                        error={errMsg}
                      />
                      <SaveBtn
                        onClick={() => save(field as string)}
                        saving={!!saving[field]}
                        saved={!!saved[field]}
                        dirty={dirty}
                      />
                      {errMsg && (
                        <span className="text-xs font-mono" style={{ color: A.red }}>{errMsg}</span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </AdminCard>
  );
}

/* ─── Ingestion Config card ──────────────────────────────── */

function IngestionConfigCard({ token }: { token: string | null }) {
  const [rows,    setRows]    = useState<ConfigRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [err,     setErr]     = useState<string | null>(null);
  const [drafts,  setDrafts]  = useState<Record<string, string>>({});
  const [saving,  setSaving]  = useState<Record<string, boolean>>({});
  const [saved,   setSaved]   = useState<Record<string, boolean>>({});
  const [errors,  setErrors]  = useState<Record<string, string>>({});

  const fetch_ = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch("/api/admin/config", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setRows(await res.json());
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to load config");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { fetch_(); }, [fetch_]);

  const save = useCallback(async (key: string) => {
    if (!token) return;
    const value = drafts[key];
    setSaving(p => ({ ...p, [key]: true }));
    try {
      const res = await fetch(`/api/admin/config/${key}`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await fetch_();
      setDrafts(p => { const n = { ...p }; delete n[key]; return n; });
      setSaved(p => ({ ...p, [key]: true }));
      setTimeout(() => setSaved(p => ({ ...p, [key]: false })), 2000);
    } catch (e: unknown) {
      setErrors(p => ({ ...p, [key]: e instanceof Error ? e.message : "Save failed" }));
    } finally {
      setSaving(p => ({ ...p, [key]: false }));
    }
  }, [token, drafts, fetch_]);

  return (
    <AdminCard>
      <CardHeader title="Ingestion Config" subtitle="Runtime config values stored in DB" />
      {loading && <p className="text-xs font-mono" style={{ color: A.muted }}>Loading…</p>}
      {err    && <p className="text-xs font-mono" style={{ color: A.red }}>{err}</p>}
      {!loading && rows.length === 0 && !err && (
        <p className="text-xs font-mono" style={{ color: A.muted }}>No config rows found.</p>
      )}
      {rows.map(row => {
        const draft = drafts[row.key] ?? row.value;
        const dirty = draft !== row.value;
        const errMsg = errors[row.key] ?? "";
        return (
          <div key={row.key} className="mb-3">
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono w-48 shrink-0" style={{ color: A.muted }}>
                {row.key}
              </span>
              <FieldInput
                value={draft}
                onChange={v => {
                  setDrafts(p => ({ ...p, [row.key]: v }));
                  setErrors(p => { const n = { ...p }; delete n[row.key]; return n; });
                }}
                onEnter={() => save(row.key)}
                dirty={dirty}
                error={errMsg}
              />
              <SaveBtn
                onClick={() => save(row.key)}
                saving={!!saving[row.key]}
                saved={!!saved[row.key]}
                dirty={dirty}
              />
            </div>
            {row.description && (
              <p className="text-xs font-mono mt-1 ml-52" style={{ color: A.faint }}>
                {row.description}
              </p>
            )}
            {errMsg && (
              <p className="text-xs font-mono mt-1 ml-52" style={{ color: A.red }}>{errMsg}</p>
            )}
          </div>
        );
      })}
    </AdminCard>
  );
}

/* ─── How It Works card ──────────────────────────────────── */

function HowItWorksCard() {
  const steps = [
    { label: "Symbols",    desc: "CBOE universe filtered by Tradier liquidity screen → tiered watchlist" },
    { label: "Stream",     desc: "Tradier WebSocket delivers live option ticks for all T1/T2/T3 symbols" },
    { label: "Classify",   desc: "Each tick parsed, deduped, and written to flow_events + accumulators" },
    { label: "Signals",    desc: "Composite signal engine reads accumulators → emits smart_signals" },
    { label: "Dashboard",  desc: "Frontend polls /api/flow and /api/signals for live UI updates" },
  ];
  return (
    <AdminCard>
      <CardHeader title="Pipeline Overview" subtitle="End-to-end data flow" />
      <div className="flex flex-col gap-3">
        {steps.map((s, i) => (
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

/* ─── Tier Distribution card (4A-OI) ────────────────────── */

function TierDistributionCard({ token }: { token: string | null }) {
  const [data,    setData]    = useState<TierDistribution | null>(null);
  const [loading, setLoading] = useState(true);
  const [err,     setErr]     = useState<string | null>(null);

  const fetch_ = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch("/api/admin/tier-distribution", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to load tier distribution");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { fetch_(); }, [fetch_]);

  const TIER_COLORS = [
    { color: A.cyan,   dim: A.cyanDim,   border: A.cyanBorder   },
    { color: A.indigo, dim: A.indigoDim, border: A.indigoBorder },
    { color: A.amber,  dim: A.amberDim,  border: A.amberBorder  },
  ];

  return (
    <AdminCard>
      <CardHeader
        title="Tier Distribution"
        subtitle="Symbol counts + OI samples per tier (from live registry snapshot)"
      />
      {loading && <p className="text-xs font-mono" style={{ color: A.muted }}>Loading…</p>}
      {err     && <p className="text-xs font-mono" style={{ color: A.red }}>{err}</p>}
      {data && !loading && (
        <>
          <p className="text-xs font-mono mb-4" style={{ color: A.muted }}>
            Snapshot <span style={{ color: A.faint }}>{data.snapshot_id}</span>
            {" · "}
            <span style={{ color: A.text }}>{data.total}</span> total symbols
          </p>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {(["1", "2", "3"] as const).map((tier, ti) => {
              const t = data.tiers[tier];
              const c = TIER_COLORS[ti];
              return (
                <div
                  key={tier}
                  className="rounded-lg p-4"
                  style={{ background: A.surface2, border: `1px solid ${A.border}` }}
                >
                  <div className="flex items-center justify-between mb-3">
                    <span
                      className="text-xs font-mono px-2 py-0.5 rounded"
                      style={{ background: c.dim, color: c.color, border: `1px solid ${c.border}` }}
                    >
                      TIER {tier}
                    </span>
                    <span className="text-sm font-semibold font-mono" style={{ color: A.text }}>
                      {t.count} symbols
                    </span>
                  </div>
                  <div className="space-y-1">
                    {t.samples.map(s => (
                      <div key={s.symbol} className="flex items-center justify-between">
                        <span className="text-xs font-mono" style={{ color: A.muted }}>{s.symbol}</span>
                        <span className="text-xs font-mono" style={{ color: A.faint }}>
                          OI: {s.open_interest?.toLocaleString() ?? "—"}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}
    </AdminCard>
  );
}
