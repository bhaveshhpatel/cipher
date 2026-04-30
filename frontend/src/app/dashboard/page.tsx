"use client";
import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/hooks/useAuth";
import { useSimulation } from "@/hooks/useSimulation";
import { useSignalStream } from "@/hooks/useSignalStream";
import { useFlowEvents } from "@/hooks/useFlowEvents";
import { useFlowEpisodes } from "@/hooks/useFlowEpisodes";
import { api } from "@/lib/api";
import type { StreamStats, CompositeSignal, FlowEvent } from "@/lib/api";
import type { FlowEventsFilters } from "@/hooks/useFlowEvents";
import type { FlowEpisodesFilters } from "@/hooks/useFlowEpisodes";
import type { DashboardTab } from "@/types";

import { DashboardLayout } from "@/components/layout";
import { FlowTable } from "@/components/dashboard/FlowTable";
import { SignalFeed } from "@/components/dashboard/SignalFeed";
import { SimulationPanel } from "@/components/dashboard/SimulationPanel";
import { CompositeCard } from "@/components/dashboard/CompositeCard";
import { SignalHistory } from "@/components/dashboard/SignalHistory";
import { FlowEventsTab } from "@/components/dashboard/FlowEventsTab";
import { FlowEpisodesTab } from "@/components/dashboard/FlowEpisodesTab";

const FLOW_REFRESH_MS  = 30_000;
const STATS_REFRESH_MS = 15_000;

export default function DashboardPage() {
  const router = useRouter();
  const { token, email, isAuthenticated, ready, logout } = useAuth();
  const { result: simResult, loading: simLoading, error: simError, progress, run: runSim } = useSimulation(token);
  const { signals, connected } = useSignalStream(token);

  // Flow scan state — imperative fetch pattern used by simulation tab
  const [events,       setEvents]       = useState<FlowEvent[]>([]);
  const [flowLoading,  setFlowLoading]  = useState(false);
  const [flowError,    setFlowError]    = useState<string | null>(null);

  const [flowEventsFilters,   setFlowEventsFilters]   = useState<FlowEventsFilters>({});
  const [flowEpisodesFilters, setFlowEpisodesFilters] = useState<FlowEpisodesFilters>({});

  const { events: flowEventRows, loading: feLoading, error: feError } = useFlowEvents(token,  flowEventsFilters);
  const { episodes,              loading: epLoading, error: epError } = useFlowEpisodes(token, flowEpisodesFilters);

  const [flowTicker,       setFlowTicker]       = useState("");
  const [compositeTicker,  setCompositeTicker]  = useState("");
  const [tab,              setTab]              = useState<DashboardTab>("flow_events");
  const [stats,            setStats]            = useState<StreamStats | null>(null);
  const [composite,        setComposite]        = useState<CompositeSignal | null>(null);
  const [compositeLoading, setCompositeLoading] = useState(false);
  const [flowCountdown,    setFlowCountdown]    = useState(FLOW_REFRESH_MS / 1000);

  // Auth guard
  useEffect(() => {
    if (!ready) return;
    if (!isAuthenticated) router.replace("/");
  }, [ready, isAuthenticated, router]);

  const doFetchFlow = useCallback(async (ticker: string) => {
    if (!token) return;
    setFlowLoading(true);
    setFlowError(null);
    setFlowCountdown(FLOW_REFRESH_MS / 1000);
    try {
      const d = await api.getFlow(ticker, token);
      setEvents(d.events);
    } catch (e) {
      setFlowError(e instanceof Error ? e.message : "Failed to load flow");
    } finally {
      setFlowLoading(false);
    }
  }, [token]);

  useEffect(() => {
    if (token) doFetchFlow("");
  }, [token, doFetchFlow]);

  // Auto-refresh countdown for simulation tab flow scan
  useEffect(() => {
    if (!token) return;
    const iv = setInterval(() => {
      setFlowCountdown(c => {
        if (c <= 1) { doFetchFlow(flowTicker); return FLOW_REFRESH_MS / 1000; }
        return c - 1;
      });
    }, 1000);
    return () => clearInterval(iv);
  }, [token, flowTicker, doFetchFlow]);

  // Stream stats polling
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
    if (!token || !events.length) return;
    const ticker = flowTicker || events[0]?.ticker || "UNKNOWN";
    runSim(ticker, events, 6, 3);
    setTab("simulation");
  };

  const handleComposite = async (t: string) => {
    if (!token || !t) return;
    setCompositeTicker(t);
    setCompositeLoading(true);
    try {
      const c = await api.getComposite(t, token);
      setComposite(c);
    } catch {
      setComposite(null);
    } finally {
      setCompositeLoading(false);
    }
  };

  if (!ready || !isAuthenticated) return null;

  return (
    <DashboardLayout
      email={email}
      stats={stats}
      onLogout={logout}
      activeTab={tab}
      onTabChange={setTab}
      signalCount={signals.length}
    >
      {/* ── Flow Events ── */}
      {tab === "flow_events" && token && (
        <div className="flex flex-col gap-4">
          <div>
            <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Flow Events</h1>
            <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
              Raw per-trade rows from live stream · 10s auto-refresh
            </p>
          </div>
          <FlowEventsTab
            events={flowEventRows}
            loading={feLoading}
            error={feError}
            onFiltersChange={setFlowEventsFilters}
          />
        </div>
      )}

      {/* ── Episodes ── */}
      {tab === "flow_episodes" && token && (
        <div className="flex flex-col gap-4">
          <div>
            <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Repetition Episodes</h1>
            <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
              Aggregated repetition clusters · 30s auto-refresh
            </p>
          </div>
          <FlowEpisodesTab
            episodes={episodes}
            loading={epLoading}
            error={epError}
            onFiltersChange={setFlowEpisodesFilters}
          />
        </div>
      )}

      {/* ── Live Signals ── */}
      {tab === "signals" && (
        <div className="flex flex-col gap-4">
          <div>
            <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Live Signal Feed</h1>
            <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
              {connected
                ? "WebSocket connected · streaming real-time signals"
                : "Connecting to stream…"}
            </p>
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
              <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
                6-agent consensus engine · BUY / SELL / HOLD verdict
              </p>
            </div>
            {events.length > 0 && (
              <button
                onClick={handleSimulate}
                disabled={simLoading}
                className="btn btn-primary text-sm px-4"
              >
                {simLoading ? `Running… ${progress}%` : "Re-run Simulation"}
              </button>
            )}
          </div>
          <SimulationPanel
            result={simResult}
            loading={simLoading}
            error={simError}
            progress={progress}
          />
        </div>
      )}

      {/* ── Composite Signal ── */}
      {tab === "composite" && (
        <div className="flex flex-col gap-4">
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div>
              <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Composite Signal</h1>
              <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
                Multi-factor scoring: flow + backtest + swarm consensus
              </p>
            </div>
            <TickerSearchBar
              placeholder="Enter ticker…"
              onScan={handleComposite}
              onClear={() => { setComposite(null); setCompositeTicker(""); }}
              loading={compositeLoading}
              activeTicker={compositeTicker}
              scanLabel="Analyze"
            />
          </div>
          {!compositeTicker && !composite && (
            <div className="card flex flex-col items-center justify-center py-20 gap-3">
              <span className="text-4xl" style={{ color: "var(--faint)" }}>◈</span>
              <p className="text-base font-semibold" style={{ color: "var(--muted)" }}>
                Enter a ticker above and click Analyze
              </p>
              <p className="text-sm" style={{ color: "var(--faint)" }}>
                Composite scores are computed on-demand from live flow + backtest data.
              </p>
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
            <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
              Persisted composite signals · flow × 0.55 + backtest × 0.35 + volume-premium × 0.10
            </p>
          </div>
          <SignalHistory token={token} />
        </div>
      )}
    </DashboardLayout>
  );
}

// ── Local helper ───────────────────────────────────────────────────────────────────────────
// TickerSearchBar is kept local — used only by the composite tab.
// Will be extracted to @/components/ui in a later PR if reuse grows.
function TickerSearchBar({
  placeholder = "Ticker…",
  onScan,
  onClear,
  loading,
  activeTicker,
  scanLabel = "Scan",
}: {
  placeholder?:  string;
  onScan:        (t: string) => void;
  onClear:       () => void;
  loading:       boolean;
  activeTicker:  string;
  scanLabel?:    string;
}) {
  const [local, setLocal] = useState(activeTicker);
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
      <button type="submit" disabled={loading} className="btn btn-primary text-xs px-3 py-1.5">
        {loading ? (
          <svg
            className="animate-spin"
            width="13" height="13"
            viewBox="0 0 24 24"
            fill="none" stroke="currentColor" strokeWidth="2.5"
          >
            <path d="M21 12a9 9 0 1 1-6.219-8.56" />
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
