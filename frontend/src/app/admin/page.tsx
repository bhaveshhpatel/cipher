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
      const res = await fetch("/health/stream", {
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
          {/* Counter grid — 3 cols */}
          <div className="grid grid-cols-3 gap-3 mb-4">
            <Stat label="Active Symbols" value={health.active_symbols} />
            <Stat label="Ticks"          value={health.ticks} />
            <Stat label="Classified"     value={health.classified} />
            <Stat label="Deduped"        value={health.deduped} />
            <Stat label="Signals"        value={health.signals} />
            <Stat label="Reconnects"     value={health.reconnects} />
          </div>

          {/* Timestamps row */}
          <div
            className="rounded-lg p-3 space-y-1.5"
            style={{ background: A.surface2, border: `1px solid ${A.border}` }}
          >
            <div className="flex justify-between">
              <span className="text-xs font-mono" style={{ color: A.muted }}>Uptime</span>
              <span className="text-xs font-mono" style={{ color: A.text }}>
                {fmtUptime(health.uptime_seconds)}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-xs font-mono" style={{ color: A.muted }}>Last Tick</span>
              <span
                className="text-xs font-mono"
                style={{ color: isLive && health.last_tick_at ? A.green : A.muted }}
              >
                {fmtTime(health.last_tick_at)}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-xs font-mono" style={{ color: A.muted }}>Last Reconnect</span>
              <span className="text-xs font-mono" style={{ color: A.muted }}>
                {fmtTime(health.last_reconnect_at)}
              </span>
            </div>
          </div>

          {health.errors > 0 && (
            <p
              className="mt-3 text-xs font-mono px-3 py-2 rounded"
              style={{ color: A.red, background: A.redDim, border: `1px solid ${A.redBorder}` }}
            >
              ⚠ {health.errors} stream error{health.errors !== 1 ? "s" : ""} since start
            </p>
          )}
        </>
      )}

      {lastRefresh && (
        <p className="text-xs font-mono mt-3" style={{ color: A.faint }}>
          Refreshed {lastRefresh.toLocaleTimeString()}
        </p>
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
  status: ReturnType<typeof useAdminDemo>["status"];
  isRunning: boolean;
  loading: boolean;
  error: string | null;
  toggle: (on: boolean) => void;
}) {
  return (
    <AdminCard>
      <div className="flex items-start justify-between mb-5">
        <div>
          <h2 className="text-base font-semibold font-mono tracking-tight" style={{ color: A.text }}>
            Demo Engine
          </h2>
          <p className="text-xs mt-1 font-mono" style={{ color: A.muted }}>
            Emits realistic Tradier timesale events through the full 6-layer pipeline
          </p>
        </div>
        <StatusPill on={isRunning} loading={status === null} />
      </div>

      <div className="flex items-center gap-3 mb-5">
        <button
          disabled={loading || isRunning || status === null}
          onClick={() => toggle(true)}
          className="px-5 py-2 rounded-lg text-sm font-mono font-semibold transition-colors"
          style={{
            background: loading || isRunning || status === null
              ? A.surface2 : "rgba(34,197,94,0.15)",
            color:      loading || isRunning || status === null
              ? A.muted : A.green,
            border: `1px solid ${loading || isRunning || status === null ? A.border : A.greenBorder}`,
            cursor: loading || isRunning || status === null ? "not-allowed" : "pointer",
          }}
        >
          {loading && !isRunning ? "Starting…" : "▶ Start Demo"}
        </button>
        <button
          disabled={loading || !isRunning || status === null}
          onClick={() => toggle(false)}
          className="px-5 py-2 rounded-lg text-sm font-mono font-semibold transition-colors"
          style={{
            background: loading || !isRunning || status === null
              ? A.surface2 : A.redDim,
            color:      loading || !isRunning || status === null
              ? A.muted : A.red,
            border: `1px solid ${loading || !isRunning || status === null ? A.border : A.redBorder}`,
            cursor: loading || !isRunning || status === null ? "not-allowed" : "pointer",
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
        <p
          className="mt-4 text-xs font-mono p-3 rounded"
          style={{ color: A.red, background: A.redDim, border: `1px solid ${A.redBorder}` }}
        >
          {error}
        </p>
      )}
    </AdminCard>
  );
}

/* ─── How It Works card ──────────────────────────────────── */

function HowItWorksCard() {
  const steps = [
    { n: "①", text: "Generates OCC symbol strings (e.g. TSLA  260620C00245000)" },
    { n: "②", text: "Emits timesale envelopes — exact Tradier format (\"last\" field for fill)" },
    { n: "③", text: "Multi-exchange: same trade on 2–4 exchanges (N/C/M/Q) within 200 ms" },
    { n: "④", text: "Layer 3 parser → Layer 4 dedup → accumulator → composite signal" },
    { n: "⑤", text: "Writes to flow_events + flow_episodes + signal_history in Supabase" },
    { n: "⑥", text: "Broadcasts to WebSocket clients — appears live in dashboard" },
  ];

  return (
    <AdminCard>
      <CardHeader
        title="Pipeline Overview"
        subtitle="How the 6-layer demo pipeline flows end-to-end"
      />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {steps.map(s => (
          <div key={s.n} className="flex items-start gap-3">
            <span
              className="text-xs font-mono font-semibold shrink-0 mt-0.5"
              style={{ color: A.cyan }}
            >
              {s.n}
            </span>
            <p className="text-xs font-mono leading-relaxed" style={{ color: A.muted }}>
              {s.text}
            </p>
          </div>
        ))}
      </div>
    </AdminCard>
  );
}

/* ─── Tier Thresholds card (B-019) ───────────────────────── */

const TIER_FIELDS: {
  key: keyof TierThresholdsRow;
  label: string;
  tier: 1 | 2 | 3;
  hint: string;
}[] = [
  { key: "t1_min_volume",     label: "Min Volume",    tier: 1, hint: "e.g. 20000000" },
  { key: "t1_min_last_price", label: "Min Price ($)", tier: 1, hint: "min last price" },
  { key: "t1_min_oi",         label: "Min OI",        tier: 1, hint: "ATM open interest" },
  { key: "t1_atm_pct",        label: "ATM % Range",   tier: 1, hint: "±% e.g. 0.20" },
  { key: "t1_max_dte",        label: "Max DTE",       tier: 1, hint: "days to expiry" },
  { key: "t2_min_volume",     label: "Min Volume",    tier: 2, hint: "e.g. 2000000" },
  { key: "t2_min_last_price", label: "Min Price ($)", tier: 2, hint: "min last price" },
  { key: "t2_min_oi",         label: "Min OI",        tier: 2, hint: "ATM open interest" },
  { key: "t2_atm_pct",        label: "ATM % Range",   tier: 2, hint: "±% e.g. 0.15" },
  { key: "t2_max_dte",        label: "Max DTE",       tier: 2, hint: "days to expiry" },
  { key: "t3_min_volume",     label: "Min Volume",    tier: 3, hint: "e.g. 500000" },
  { key: "t3_min_last_price", label: "Min Price ($)", tier: 3, hint: "min last price" },
  { key: "t3_min_oi",         label: "Min OI",        tier: 3, hint: "ATM open interest" },
  { key: "t3_atm_pct",        label: "ATM % Range",   tier: 3, hint: "±% e.g. 0.10" },
  { key: "t3_max_dte",        label: "Max DTE",       tier: 3, hint: "days to expiry" },
];

const TIER_COLORS: Record<
  1 | 2 | 3,
  { border: string; bg: string; label: string; accent: string }
> = {
  1: {
    border: "rgba(251,191,36,0.3)",
    bg:     "rgba(251,191,36,0.05)",
    label:  "T1 — Liquid Large-Cap",
    accent: "#fbbf24",
  },
  2: {
    border: "rgba(129,140,248,0.3)",
    bg:     "rgba(129,140,248,0.05)",
    label:  "T2 — Mid-Cap",
    accent: "#818cf8",
  },
  3: {
    border: "rgba(107,131,166,0.25)",
    bg:     "rgba(107,131,166,0.04)",
    label:  "T3 — Standard",
    accent: A.muted,
  },
};

function TierThresholdsCard({ token }: { token: string | null }) {
  const [row,      setRow]      = useState<TierThresholdsRow | null>(null);
  const [cache,    setCache]    = useState<CacheMeta | null>(null);
  const [edits,    setEdits]    = useState<Record<string, string>>({});
  const [saving,   setSaving]   = useState<Record<string, boolean>>({});
  const [saved,    setSaved]    = useState<Record<string, boolean>>({});
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  const [loading,  setLoading]  = useState(true);
  const [fetchErr, setFetchErr] = useState<string | null>(null);

  const fetchThresholds = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setFetchErr(null);
    try {
      const res = await fetch("/api/admin/tier-thresholds", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setRow(data.row);
      setCache(data.cache);
      const initial: Record<string, string> = {};
      for (const f of TIER_FIELDS) initial[f.key] = String(data.row[f.key] ?? "");
      setEdits(initial);
    } catch (e: unknown) {
      setFetchErr(e instanceof Error ? e.message : "Failed to load thresholds");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { fetchThresholds(); }, [fetchThresholds]);

  const handleSave = async (key: string) => {
    if (!token || !row) return;
    const numVal = Number(edits[key]);
    if (isNaN(numVal)) {
      setFieldErr(e => ({ ...e, [key]: "Must be a number" }));
      return;
    }
    setSaving(s => ({ ...s, [key]: true }));
    setFieldErr(e => ({ ...e, [key]: "" }));
    setSaved(s => ({ ...s, [key]: false }));
    try {
      const res = await fetch("/api/admin/tier-thresholds", {
        method:  "PATCH",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body:    JSON.stringify({ updates: { [key]: numVal } }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error((err as { detail?: string }).detail ?? `HTTP ${res.status}`);
      }
      setSaved(s => ({ ...s, [key]: true }));
      await fetchThresholds();
      setTimeout(() => setSaved(s => ({ ...s, [key]: false })), 2500);
    } catch (e: unknown) {
      setFieldErr(er => ({ ...er, [key]: e instanceof Error ? e.message : "Save failed" }));
    } finally {
      setSaving(s => ({ ...s, [key]: false }));
    }
  };

  const isDirty = (key: string) =>
    row !== null &&
    edits[key] !== undefined &&
    edits[key] !== String((row as unknown as Record<string, unknown>)[key] ?? "");

  const tierGroups: { tier: 1 | 2 | 3; fields: typeof TIER_FIELDS }[] = [
    { tier: 1, fields: TIER_FIELDS.filter(f => f.tier === 1) },
    { tier: 2, fields: TIER_FIELDS.filter(f => f.tier === 2) },
    { tier: 3, fields: TIER_FIELDS.filter(f => f.tier === 3) },
  ];

  return (
    <AdminCard>
      <div className="flex items-start justify-between mb-2">
        <div>
          <h2 className="text-base font-semibold font-mono tracking-tight" style={{ color: A.text }}>
            Tier Thresholds
          </h2>
          <p className="text-xs mt-1 font-mono" style={{ color: A.muted }}>
            T1/T2/T3 classification rules. Changes apply on next universe refresh.
          </p>
        </div>
        <button
          onClick={fetchThresholds}
          className="text-xs font-mono shrink-0 px-2 py-1 rounded transition-colors"
          style={{ color: A.muted, border: `1px solid ${A.border}`, background: A.surface2 }}
        >
          ↻ Refresh
        </button>
      </div>

      {/* Cache badge */}
      {cache && !loading && (
        <div className="mb-4">
          <span
            className="text-xs font-mono px-2 py-0.5 rounded-full"
            style={{
              border:     cache.warm ? `1px solid ${A.greenBorder}` : `1px solid ${A.border}`,
              background: cache.warm ? A.greenDim : A.surface2,
              color:      cache.warm ? A.green    : A.muted,
            }}
          >
            {cache.warm
              ? `● cache warm — ${cache.age_seconds}s ago (TTL ${cache.ttl_seconds}s)`
              : `○ cache cold`}
          </span>
        </div>
      )}

      {loading && (
        <p className="text-xs font-mono mt-4" style={{ color: A.muted }}>Loading…</p>
      )}
      {fetchErr && (
        <p
          className="text-xs font-mono p-3 rounded mt-4"
          style={{ color: A.red, background: A.redDim, border: `1px solid ${A.redBorder}` }}
        >
          {fetchErr}
        </p>
      )}

      {!loading && row && (
        <div className="space-y-3 mt-1">
          {tierGroups.map(({ tier, fields }) => {
            const c = TIER_COLORS[tier];
            return (
              <div
                key={tier}
                className="rounded-lg p-4"
                style={{ border: `1px solid ${c.border}`, background: c.bg }}
              >
                <p
                  className="text-xs font-mono font-semibold mb-3"
                  style={{ color: c.accent }}
                >
                  {c.label}
                </p>
                <div className="space-y-2">
                  {fields.map(f => (
                    <div key={f.key}>
                      <div className="flex items-center justify-between mb-0.5">
                        <span className="text-xs font-mono" style={{ color: A.muted }}>
                          {f.label}
                        </span>
                        <span
                          className="text-xs font-mono"
                          style={{ color: A.faint }}
                        >
                          {f.hint}
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <FieldInput
                          value={edits[f.key] ?? ""}
                          onChange={v => setEdits(p => ({ ...p, [f.key]: v }))}
                          onEnter={() => handleSave(f.key)}
                          dirty={isDirty(f.key)}
                          error={fieldErr[f.key] ?? ""}
                        />
                        <SaveBtn
                          onClick={() => handleSave(f.key)}
                          saving={!!saving[f.key]}
                          saved={!!saved[f.key]}
                          dirty={isDirty(f.key)}
                        />
                      </div>
                      {fieldErr[f.key] && (
                        <p className="text-xs font-mono mt-0.5" style={{ color: A.red }}>
                          {fieldErr[f.key]}
                        </p>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {row && (
        <p className="text-xs font-mono mt-4" style={{ color: A.faint }}>
          Last updated {new Date(row.updated_at).toLocaleString()}
          {row.updated_by ? ` by ${row.updated_by}` : ""} · row #{row.id}
        </p>
      )}
      <p className="text-xs font-mono mt-1" style={{ color: A.faint }}>
        ⚡ Saves bust the in-process cache immediately. No restart required.
      </p>
    </AdminCard>
  );
}

/* ─── Ingestion Config card ──────────────────────────────── */

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
    <AdminCard>
      <div className="flex items-start justify-between mb-5">
        <div>
          <h2 className="text-base font-semibold font-mono tracking-tight" style={{ color: A.text }}>
            Ingestion Config
          </h2>
          <p className="text-xs mt-1 font-mono" style={{ color: A.muted }}>
            Layer 1 OCC registry + universe pipeline knobs.
            Takes effect on next registry refresh — no restart needed.
          </p>
        </div>
        <button
          onClick={fetchConfig}
          className="text-xs font-mono shrink-0 px-2 py-1 rounded transition-colors"
          style={{ color: A.muted, border: `1px solid ${A.border}`, background: A.surface2 }}
        >
          ↻ Refresh
        </button>
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

      {!loading && rows.length > 0 && (
        <div className="space-y-3">
          {rows.map(row => (
            <div
              key={row.key}
              className="rounded-lg p-4"
              style={{ background: A.surface2, border: `1px solid ${A.border}` }}
            >
              <div className="flex items-start justify-between gap-2 mb-2">
                <div>
                  <p className="text-xs font-mono font-semibold" style={{ color: A.text }}>
                    {row.key}
                  </p>
                  {row.description && (
                    <p className="text-xs font-mono mt-0.5" style={{ color: A.muted }}>
                      {row.description}
                    </p>
                  )}
                </div>
                <span
                  className="text-xs font-mono px-2 py-0.5 rounded shrink-0"
                  style={{
                    background: A.surface,
                    color: A.cyan,
                    border: `1px solid ${A.cyanBorder}`,
                  }}
                >
                  {row.value_type}
                </span>
              </div>

              <div className="flex items-center gap-2">
                <FieldInput
                  value={edits[row.key] ?? row.value}
                  onChange={v => setEdits(p => ({ ...p, [row.key]: v }))}
                  onEnter={() => handleSave(row.key)}
                  dirty={isDirty(row.key, row.value)}
                  error={errors[row.key] ?? ""}
                />
                <SaveBtn
                  onClick={() => handleSave(row.key)}
                  saving={!!saving[row.key]}
                  saved={!!saved[row.key]}
                  dirty={isDirty(row.key, row.value)}
                />
              </div>

              {errors[row.key] && (
                <p className="text-xs font-mono mt-1" style={{ color: A.red }}>
                  {errors[row.key]}
                </p>
              )}

              <p className="text-xs font-mono mt-2" style={{ color: A.faint }}>
                Updated {new Date(row.updated_at).toLocaleString()}
                {row.updated_by ? ` by ${row.updated_by}` : ""}
              </p>
            </div>
          ))}
        </div>
      )}

      {!loading && rows.length === 0 && !fetchErr && (
        <p className="text-xs font-mono" style={{ color: A.muted }}>
          No config rows found.
        </p>
      )}

      <p className="text-xs font-mono mt-4" style={{ color: A.faint }}>
        ⚡ Changes propagate within REGISTRY_REFRESH_MINS minutes (next scheduled rebuild).
      </p>
    </AdminCard>
  );
}

/* ─── Tier Distribution card (4A-OI / B-020) ────────────── */

function TierDistributionCard({ token }: { token: string | null }) {
  const [dist,     setDist]     = useState<TierDistribution | null>(null);
  const [loading,  setLoading]  = useState(true);
  const [fetchErr, setFetchErr] = useState<string | null>(null);

  const fetchDist = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setFetchErr(null);
    try {
      const res = await fetch("/api/admin/tier-distribution", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setDist(await res.json());
    } catch (e: unknown) {
      setFetchErr(e instanceof Error ? e.message : "Failed to fetch tier distribution");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { fetchDist(); }, [fetchDist]);

  const tierKeys: Array<"1" | "2" | "3"> = ["1", "2", "3"];
  const tierNum = (k: "1" | "2" | "3") => parseInt(k, 10) as 1 | 2 | 3;

  return (
    <AdminCard>
      <div className="flex items-start justify-between mb-5">
        <div>
          <h2 className="text-base font-semibold font-mono tracking-tight" style={{ color: A.text }}>
            Tier Distribution
          </h2>
          <p className="text-xs mt-1 font-mono" style={{ color: A.muted }}>
            Active snapshot — up to 10 sample symbols per tier with avg chain OI
          </p>
        </div>
        <button
          onClick={fetchDist}
          className="text-xs font-mono shrink-0 px-2 py-1 rounded transition-colors"
          style={{ color: A.muted, border: `1px solid ${A.border}`, background: A.surface2 }}
        >
          ↻ Refresh
        </button>
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

      {dist && !loading && (
        <>
          {/* Summary stats row */}
          <div className="grid grid-cols-4 gap-3 mb-5">
            <Stat label="Total Symbols" value={dist.total} />
            <Stat label="T1 Count"      value={dist.tiers["1"].count} />
            <Stat label="T2 Count"      value={dist.tiers["2"].count} />
            <Stat label="T3 Count"      value={dist.tiers["3"].count} />
          </div>

          {/* Per-tier sample tables */}
          <div className="space-y-4">
            {tierKeys.map(tk => {
              const tier   = dist.tiers[tk];
              const c      = TIER_COLORS[tierNum(tk)];
              const noData = tier.samples.length === 0;
              return (
                <div
                  key={tk}
                  className="rounded-lg overflow-hidden"
                  style={{ border: `1px solid ${c.border}` }}
                >
                  {/* Tier header */}
                  <div
                    className="px-4 py-2 flex items-center justify-between"
                    style={{ background: c.bg }}
                  >
                    <span className="text-xs font-mono font-semibold" style={{ color: c.accent }}>
                      {c.label}
                    </span>
                    <span className="text-xs font-mono" style={{ color: A.muted }}>
                      {tier.count.toLocaleString()} symbol{tier.count !== 1 ? "s" : ""}
                    </span>
                  </div>

                  {noData ? (
                    <div className="px-4 py-3" style={{ background: A.surface2 }}>
                      <p className="text-xs font-mono" style={{ color: A.faint }}>No symbols</p>
                    </div>
                  ) : (
                    <table className="w-full text-xs font-mono" style={{ background: A.surface2 }}>
                      <thead>
                        <tr style={{ borderBottom: `1px solid ${A.border}` }}>
                          <th
                            className="text-left px-4 py-2"
                            style={{ color: A.muted, fontWeight: 500 }}
                          >
                            Symbol
                          </th>
                          <th
                            className="text-center px-4 py-2"
                            style={{ color: A.muted, fontWeight: 500 }}
                          >
                            Tier
                          </th>
                          <th
                            className="text-right px-4 py-2"
                            style={{ color: A.muted, fontWeight: 500 }}
                            title="Average open interest across loaded option contracts for this symbol"
                          >
                            Avg Chain OI ℹ
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {tier.samples.map((s, i) => (
                          <tr
                            key={s.symbol}
                            style={{
                              borderBottom: i < tier.samples.length - 1
                                ? `1px solid ${A.border}`
                                : undefined,
                            }}
                          >
                            {/* Symbol */}
                            <td className="px-4 py-2" style={{ color: A.text }}>
                              {s.symbol}
                            </td>

                            {/* Tier badge */}
                            <td className="px-4 py-2 text-center">
                              <span
                                className="px-2 py-0.5 rounded-full text-xs font-mono font-semibold"
                                style={{
                                  background: c.bg,
                                  color:      c.accent,
                                  border:     `1px solid ${c.border}`,
                                }}
                              >
                                T{tk}
                              </span>
                            </td>

                            {/* Avg Chain OI */}
                            <td
                              className="px-4 py-2 text-right tabular-nums"
                              title="Average open interest across loaded option contracts for this symbol"
                              style={{
                                color: s.open_interest != null ? A.text : A.faint,
                              }}
                            >
                              {s.open_interest != null
                                ? s.open_interest.toLocaleString()
                                : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              );
            })}
          </div>

          <p className="text-xs font-mono mt-3" style={{ color: A.faint }}>
            Snapshot {dist.snapshot_id.slice(0, 8)}…
            &nbsp;·&nbsp;
            OI populated by 4A-OI two-pass pipeline on cold start / 24h refresh
          </p>
        </>
      )}
    </AdminCard>
  );
}
