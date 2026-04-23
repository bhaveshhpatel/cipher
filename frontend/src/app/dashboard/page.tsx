"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/hooks/useAuth";
import { useFlow } from "@/hooks/useFlow";
import { useSimulation } from "@/hooks/useSimulation";
import { useSignalStream } from "@/hooks/useSignalStream";
import { api } from "@/lib/api";
import type { StreamStats, CompositeSignal } from "@/lib/api";

import { CipherLogo } from "@/components/CipherLogo";
import { ThemeToggle } from "@/components/ThemeToggle";
import { StreamStatsBar } from "@/components/dashboard/StreamStatsBar";
import { FlowTable } from "@/components/dashboard/FlowTable";
import { SignalFeed } from "@/components/dashboard/SignalFeed";
import { SimulationPanel } from "@/components/dashboard/SimulationPanel";
import { CompositeCard } from "@/components/dashboard/CompositeCard";

type Tab = "flow" | "signals" | "simulation" | "composite";

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: "flow",       label: "Flow Scanner", icon: "⟁" },
  { id: "signals",    label: "Live Signals", icon: "◉" },
  { id: "simulation", label: "AI Simulation", icon: "⬡" },
  { id: "composite",  label: "Composite",    icon: "◈" },
];

const DEFAULT_TICKER = "SPY";

export default function DashboardPage() {
  const router = useRouter();
  const { token, email, isAuthenticated, logout } = useAuth();
  const { events, loading: flowLoading, error: flowError, fetch: fetchFlow } = useFlow(token);
  const { result: simResult, loading: simLoading, error: simError, progress, run: runSim } = useSimulation(token);
  const { signals, connected } = useSignalStream(token);

  const [ticker,    setTicker]    = useState(DEFAULT_TICKER);
  const [tab,       setTab]       = useState<Tab>("flow");
  const [stats,     setStats]     = useState<StreamStats | null>(null);
  const [composite, setComposite] = useState<CompositeSignal | null>(null);
  const [compositeLoading, setCompositeLoading] = useState(false);

  // Auth guard
  useEffect(() => {
    if (!isAuthenticated) router.push("/");
  }, [isAuthenticated, router]);

  // Poll stream stats every 15s
  useEffect(() => {
    if (!token) return;
    const load = async () => {
      try { const d = await api.getStats(token); setStats(d.stats); } catch {}
    };
    load();
    const iv = setInterval(load, 15_000);
    return () => clearInterval(iv);
  }, [token]);

  // Auto-load flow on mount
  useEffect(() => {
    if (token) fetchFlow(DEFAULT_TICKER);
  }, [token]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleFlowScan = (t: string) => {
    setTicker(t);
    fetchFlow(t);
  };

  const handleSimulate = () => {
    if (!events.length) return;
    runSim(ticker, events, 6, 3);
    setTab("simulation");
  };

  const handleComposite = async (t: string) => {
    if (!token) return;
    setCompositeLoading(true);
    try { const d = await api.getComposite(t, token); setComposite(d); }
    catch {}
    finally { setCompositeLoading(false); }
  };

  if (!isAuthenticated) return null;

  return (
    <div className="min-h-dvh flex flex-col" style={{ background: "var(--bg)" }}>

      {/* ── Top Nav ─────────────────────────────────────── */}
      <header
        className="sticky top-0 z-40 flex items-center justify-between px-5 py-3 gap-4"
        style={{
          background:    "var(--surface)",
          borderBottom:  "1px solid var(--border)",
          backdropFilter:"blur(8px)",
        }}
      >
        {/* Left: Logo + ticker input */}
        <div className="flex items-center gap-4 min-w-0">
          <CipherLogo size={32} />
          <TickerInput
            value={ticker}
            onScan={handleFlowScan}
            onComposite={handleComposite}
            loading={flowLoading}
          />
        </div>

        {/* Center: Stream health pill */}
        <div className="hidden sm:flex items-center gap-2">
          <span
            className="pulse-dot inline-block w-2 h-2 rounded-full"
            style={{ background: connected ? "var(--green)" : "var(--faint)" }}
          />
          <span className="text-xs font-mono" style={{ color: "var(--muted)" }}>
            {connected ? "LIVE" : "OFFLINE"}
          </span>
          {stats && (
            <span className="text-xs font-mono tabular" style={{ color: "var(--faint)" }}>
              · {stats.active_symbols.toLocaleString()} symbols · {stats.signals.toLocaleString()} signals
            </span>
          )}
        </div>

        {/* Right: User + theme */}
        <div className="flex items-center gap-2 shrink-0">
          <span className="hidden md:block text-xs font-mono truncate max-w-[160px]"
                style={{ color: "var(--muted)" }}>
            {email}
          </span>
          <ThemeToggle />
          <button
            onClick={logout}
            className="btn btn-ghost text-xs px-3 py-1.5"
          >
            Sign out
          </button>
        </div>
      </header>

      {/* ── Stream stats bar ─────────────────────────────── */}
      {stats && (
        <div style={{ borderBottom: "1px solid var(--border)", background: "var(--surface-2)" }}>
          <StreamStatsBar stats={stats} />
        </div>
      )}

      {/* ── Tab bar ──────────────────────────────────────── */}
      <nav
        className="flex items-center gap-1 px-4 py-2 overflow-x-auto"
        style={{ borderBottom: "1px solid var(--border)", background: "var(--surface)" }}
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className="flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-semibold whitespace-nowrap transition-all"
            style={{
              background:   tab === t.id ? "rgba(232,160,32,0.1)" : "transparent",
              color:        tab === t.id ? "var(--amber)"          : "var(--muted)",
              borderBottom: tab === t.id ? "2px solid var(--amber)": "2px solid transparent",
              borderRadius: "6px 6px 0 0",
            }}
          >
            <span className="text-base leading-none">{t.icon}</span>
            {t.label}
            {t.id === "signals" && signals.length > 0 && (
              <span
                className="inline-flex items-center justify-center w-5 h-5 rounded-full text-2xs font-bold"
                style={{ background: "var(--amber)", color: "#1a0f00" }}
              >
                {signals.length > 99 ? "99+" : signals.length}
              </span>
            )}
          </button>
        ))}
      </nav>

      {/* ── Main content ─────────────────────────────────── */}
      <main className="flex-1 p-4 md:p-6" style={{ maxWidth: 1400, width: "100%", margin: "0 auto" }}>

        {tab === "flow" && (
          <div className="flex flex-col gap-4">
            <SectionHeader
              title="Options Flow Scanner"
              subtitle={`Scanning ${ticker} · ${events.length} events loaded`}
            >
              {events.length > 0 && (
                <button onClick={handleSimulate} className="btn btn-primary text-sm px-4">
                  ⬡ Run AI Simulation
                </button>
              )}
            </SectionHeader>
            <FlowTable
              events={events}
              loading={flowLoading}
              error={flowError}
              ticker={ticker}
              onScan={handleFlowScan}
            />
          </div>
        )}

        {tab === "signals" && (
          <div className="flex flex-col gap-4">
            <SectionHeader
              title="Live Signal Feed"
              subtitle={connected ? "WebSocket connected · streaming real-time signals" : "Connecting to stream…"}
            />
            <SignalFeed signals={signals} connected={connected} />
          </div>
        )}

        {tab === "simulation" && (
          <div className="flex flex-col gap-4">
            <SectionHeader
              title="AI Swarm Simulation"
              subtitle="6-agent consensus engine · BUY / SELL / HOLD verdict"
            >
              {events.length > 0 && (
                <button
                  onClick={handleSimulate}
                  disabled={simLoading}
                  className="btn btn-primary text-sm px-4"
                >
                  {simLoading ? `Running… ${progress}%` : "Re-run Simulation"}
                </button>
              )}
            </SectionHeader>
            <SimulationPanel
              result={simResult}
              loading={simLoading}
              error={simError}
              progress={progress}
            />
          </div>
        )}

        {tab === "composite" && (
          <div className="flex flex-col gap-4">
            <SectionHeader
              title="Composite Signal"
              subtitle="Multi-factor scoring: flow + backtest + swarm consensus"
            >
              <button
                onClick={() => handleComposite(ticker)}
                disabled={compositeLoading}
                className="btn btn-primary text-sm px-4"
              >
                {compositeLoading ? "Analyzing…" : `Analyze ${ticker}`}
              </button>
            </SectionHeader>
            <CompositeCard
              signal={composite}
              loading={compositeLoading}
              ticker={ticker}
            />
          </div>
        )}
      </main>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function SectionHeader({
  title, subtitle, children
}: { title: string; subtitle?: string; children?: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 flex-wrap">
      <div>
        <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>{title}</h1>
        {subtitle && (
          <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>{subtitle}</p>
        )}
      </div>
      {children && <div className="flex items-center gap-2">{children}</div>}
    </div>
  );
}

function TickerInput({
  value, onScan, onComposite, loading
}: {
  value: string;
  onScan: (t: string) => void;
  onComposite: (t: string) => void;
  loading: boolean;
}) {
  const [local, setLocal] = useState(value);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const t = local.trim().toUpperCase();
    if (t) { onScan(t); onComposite(t); }
  };

  return (
    <form onSubmit={submit} className="flex items-center gap-2">
      <input
        value={local}
        onChange={(e) => setLocal(e.target.value.toUpperCase())}
        placeholder="Ticker…"
        maxLength={6}
        className="w-24 px-3 py-1.5 rounded-lg text-sm font-mono font-semibold uppercase outline-none transition-all"
        style={{
          background:  "var(--surface-2)",
          border:      "1px solid var(--border)",
          color:       "var(--text)",
        }}
        onFocus={(e) => (e.target.style.borderColor = "var(--amber)")}
        onBlur={(e)  => (e.target.style.borderColor = "var(--border)")}
      />
      <button
        type="submit"
        disabled={loading}
        className="btn btn-primary text-xs px-3 py-1.5"
      >
        {loading ? (
          <svg className="animate-spin" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
          </svg>
        ) : "Scan"}
      </button>
    </form>
  );
}
