"use client";
import { useEffect, useState, useRef, useCallback } from "react";
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
import { SignalHistory } from "@/components/dashboard/SignalHistory";

type Tab = "flow" | "signals" | "simulation" | "composite" | "history";

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: "flow",       label: "Flow Scanner",  icon: "⟁" },
  { id: "signals",    label: "Live Signals",   icon: "◉" },
  { id: "simulation", label: "AI Simulation",  icon: "⬡" },
  { id: "composite",  label: "Composite",      icon: "◈" },
  { id: "history",    label: "Signal History", icon: "🕐" },
];

// Auto-refresh intervals (ms)
const FLOW_REFRESH_MS   = 30_000;
const STATS_REFRESH_MS  = 15_000;

export default function DashboardPage() {
  const router = useRouter();
  const { token, email, isAuthenticated, logout } = useAuth();
  const { events, loading: flowLoading, error: flowError, fetch: fetchFlow } = useFlow(token);
  const { result: simResult, loading: simLoading, error: simError, progress, run: runSim } = useSimulation(token);
  const { signals, connected } = useSignalStream(token);

  // ticker state — empty string means "all tickers"
  const [flowTicker,      setFlowTicker]      = useState("");
  const [compositeTicker, setCompositeTicker] = useState("");

  const [tab,       setTab]       = useState<Tab>("flow");
  const [stats,     setStats]     = useState<StreamStats | null>(null);
  const [composite, setComposite] = useState<CompositeSignal | null>(null);
  const [compositeLoading, setCompositeLoading] = useState(false);

  // flow auto-refresh countdown
  const [flowCountdown, setFlowCountdown] = useState(FLOW_REFRESH_MS / 1000);
  const flowTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Auth guard
  useEffect(() => {
    if (!isAuthenticated) router.push("/");
  }, [isAuthenticated, router]);

  // ── Fetch flow (no ticker = all) ──────────────────────
  const doFetchFlow = useCallback((ticker: string) => {
    fetchFlow(ticker); // empty string → backend returns all
    setFlowCountdown(FLOW_REFRESH_MS / 1000);
  }, [fetchFlow]);

  // Auto-load all flow on mount
  useEffect(() => {
    if (token) doFetchFlow("");
  }, [token]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-refresh flow every 30s
  useEffect(() => {
    if (!token) return;
    // countdown tick
    const countIv = setInterval(() => {
      setFlowCountdown(c => {
        if (c <= 1) { doFetchFlow(flowTicker); return FLOW_REFRESH_MS / 1000; }
        return c - 1;
      });
    }, 1000);
    return () => clearInterval(countIv);
  }, [token, flowTicker, doFetchFlow]);

  // Poll stream stats every 15s
  useEffect(() => {
    if (!token) return;
    const load = async () => {
      try { const d = await api.getStats(token); setStats(d.stats); } catch {}
    };
    load();
    const iv = setInterval(load, STATS_REFRESH_MS);
    return () => clearInterval(iv);
  }, [token]);

  const handleFlowScan = (t: string) => {
    setFlowTicker(t);
    doFetchFlow(t);
  };

  const handleSimulate = () => {
    if (!events.length) return;
    runSim(flowTicker || "MARKET", events, 6, 3);
    setTab("simulation");
  };

  const handleComposite = async (t: string) => {
    if (!token || !t) return;
    setCompositeTicker(t);
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
          background:     "var(--surface)",
          borderBottom:   "1px solid var(--border)",
          backdropFilter: "blur(8px)",
        }}
      >
        {/* Left: logo + live status */}
        <div className="flex items-center gap-4 min-w-0">
          <CipherLogo size={32} />
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
        </div>

        {/* Right: user + controls */}
        <div className="flex items-center gap-2 shrink-0">
          <span className="hidden md:block text-xs font-mono truncate max-w-[160px]"
                style={{ color: "var(--muted)" }}>
            {email}
          </span>
          <ThemeToggle />
          <button onClick={logout} className="btn btn-ghost text-xs px-3 py-1.5">
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

        {/* ── Flow Scanner ── */}
        {tab === "flow" && (
          <div className="flex flex-col gap-4">
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div>
                <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Options Flow Scanner</h1>
                <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
                  {flowTicker ? `Filtered: ${flowTicker}` : "Showing all tickers"}
                  {" · "}{events.length} events
                  {" · "}
                  <span style={{ color: "var(--faint)" }}>auto-refresh in {flowCountdown}s</span>
                </p>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <TickerSearchBar
                  placeholder="Filter by ticker…"
                  onScan={handleFlowScan}
                  onClear={() => handleFlowScan("")}
                  loading={flowLoading}
                  activeTicker={flowTicker}
                />
                {events.length > 0 && (
                  <button onClick={handleSimulate} className="btn btn-primary text-sm px-4">
                    ⬡ Run AI Simulation
                  </button>
                )}
              </div>
            </div>
            <FlowTable
              events={events}
              loading={flowLoading}
              error={flowError}
              ticker={flowTicker}
              onScan={handleFlowScan}
            />
          </div>
        )}

        {/* ── Live Signals ── */}
        {tab === "signals" && (
          <div className="flex flex-col gap-4">
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div>
                <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Live Signal Feed</h1>
                <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
                  {connected ? "WebSocket connected · streaming real-time signals" : "Connecting to stream…"}
                </p>
              </div>
            </div>
            <SignalFeed signals={signals} connected={connected} token={token} />
          </div>
        )}

        {/* ── AI Simulation ── */}
        {tab === "simulation" && (
          <div className="flex flex-col gap-4">
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div>
                <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>AI Swarm Simulation</h1>
                <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>6-agent consensus engine · BUY / SELL / HOLD verdict</p>
              </div>
              {events.length > 0 && (
                <button onClick={handleSimulate} disabled={simLoading} className="btn btn-primary text-sm px-4">
                  {simLoading ? `Running… ${progress}%` : "Re-run Simulation"}
                </button>
              )}
            </div>
            <SimulationPanel result={simResult} loading={simLoading} error={simError} progress={progress} />
          </div>
        )}

        {/* ── Composite ── */}
        {tab === "composite" && (
          <div className="flex flex-col gap-4">
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div>
                <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Composite Signal</h1>
                <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>Multi-factor scoring: flow + backtest + swarm consensus</p>
              </div>
              <div className="flex items-center gap-2">
                <TickerSearchBar
                  placeholder="Enter ticker…"
                  onScan={handleComposite}
                  onClear={() => { setComposite(null); setCompositeTicker(""); }}
                  loading={compositeLoading}
                  activeTicker={compositeTicker}
                  scanLabel="Analyze"
                />
              </div>
            </div>
            {!compositeTicker && !composite && (
              <div className="card flex flex-col items-center justify-center py-20 gap-3">
                <span className="text-4xl" style={{ color: "var(--faint)" }}>◈</span>
                <p className="text-base font-semibold" style={{ color: "var(--muted)" }}>Enter a ticker above and click Analyze</p>
                <p className="text-sm" style={{ color: "var(--faint)" }}>Composite scores are computed on-demand from live flow + backtest data.</p>
              </div>
            )}
            <CompositeCard signal={composite} loading={compositeLoading} ticker={compositeTicker} />
          </div>
        )}

        {/* ── Signal History ── */}
        {tab === "history" && token && (
          <div className="flex flex-col gap-4">
            <div>
              <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Signal History</h1>
              <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>Persisted composite signals · flow × 0.55 + backtest × 0.35 + volume-premium × 0.10</p>
            </div>
            <SignalHistory token={token} />
          </div>
        )}

      </main>
    </div>
  );
}

// ── TickerSearchBar ───────────────────────────────────────────────────────────
// Replaces the old TickerInput that lived in the header.
// Lives inside each tab's header area so it's contextual.

function TickerSearchBar({
  placeholder = "Ticker…",
  onScan,
  onClear,
  loading,
  activeTicker,
  scanLabel = "Scan",
}: {
  placeholder?: string;
  onScan: (t: string) => void;
  onClear: () => void;
  loading: boolean;
  activeTicker: string;
  scanLabel?: string;
}) {
  const [local, setLocal] = useState(activeTicker);

  // keep local in sync if parent clears
  useEffect(() => { setLocal(activeTicker); }, [activeTicker]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const t = local.trim().toUpperCase();
    if (t) onScan(t);
  };

  return (
    <form onSubmit={submit} className="flex items-center gap-2">
      <input
        value={local}
        onChange={(e) => setLocal(e.target.value.toUpperCase())}
        placeholder={placeholder}
        maxLength={6}
        className="w-28 px-3 py-1.5 rounded-lg text-sm font-mono font-semibold uppercase outline-none transition-all"
        style={{
          background: "var(--surface-2)",
          border:     "1px solid var(--border)",
          color:      "var(--text)",
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
        ) : scanLabel}
      </button>
      {activeTicker && (
        <button
          type="button"
          onClick={() => { setLocal(""); onClear(); }}
          className="text-xs px-2 py-1.5 rounded-md transition-all"
          style={{
            color:      "var(--muted)",
            background: "var(--surface-2)",
            border:     "1px solid var(--border)",
          }}
          title="Clear filter — show all"
        >
          ✕ All
        </button>
      )}
    </form>
  );
}
