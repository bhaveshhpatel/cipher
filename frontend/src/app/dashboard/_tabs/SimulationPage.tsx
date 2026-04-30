"use client";
import { useState, useCallback, useEffect } from "react";
import { useSimulation } from "@/hooks/useSimulation";
import { api } from "@/lib/api";
import type { FlowEvent } from "@/lib/api";
import { SimulationPanel } from "@/components/dashboard/SimulationPanel";

const FLOW_REFRESH_MS = 30_000;

interface Props { token: string | null; }

export function SimulationPage({ token }: Props) {
  const { result, loading: simLoading, error: simError, progress, run: runSim } = useSimulation(token);

  const [events,      setEvents]      = useState<FlowEvent[]>([]);
  const [flowLoading, setFlowLoading] = useState(false);
  const [flowError,   setFlowError]   = useState<string | null>(null);
  const [flowTicker,  setFlowTicker]  = useState("");
  const [countdown,   setCountdown]   = useState(FLOW_REFRESH_MS / 1000);

  const doFetchFlow = useCallback(async (ticker: string) => {
    if (!token) return;
    setFlowLoading(true);
    setFlowError(null);
    setCountdown(FLOW_REFRESH_MS / 1000);
    try {
      const d = await api.getFlow(ticker, token);
      setEvents(d.events);
    } catch (e) {
      setFlowError(e instanceof Error ? e.message : "Failed to load flow");
    } finally {
      setFlowLoading(false);
    }
  }, [token]);

  // Initial fetch on mount
  useEffect(() => {
    if (token) doFetchFlow("");
  }, [token, doFetchFlow]);

  // Auto-refresh countdown
  useEffect(() => {
    if (!token) return;
    const iv = setInterval(() => {
      setCountdown(c => {
        if (c <= 1) { doFetchFlow(flowTicker); return FLOW_REFRESH_MS / 1000; }
        return c - 1;
      });
    }, 1000);
    return () => clearInterval(iv);
  }, [token, flowTicker, doFetchFlow]);

  const handleScan = (t: string) => {
    setFlowTicker(t);
    doFetchFlow(t);
  };

  const handleSimulate = () => {
    if (!token || !events.length) return;
    const ticker = flowTicker || events[0]?.ticker || "UNKNOWN";
    runSim(ticker, events, 6, 3);
  };

  return (
    <div className="flex flex-col gap-4" data-testid="simulation-page">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>AI Swarm Simulation</h1>
          <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
            6-agent consensus engine · BUY / SELL / HOLD verdict
          </p>
        </div>
        {events.length > 0 && (
          <button
            data-testid="rerun-btn"
            onClick={handleSimulate}
            disabled={simLoading}
            className="btn btn-primary text-sm px-4"
          >
            {simLoading ? `Running… ${progress}%` : "Re-run Simulation"}
          </button>
        )}
      </div>

      {/* Ticker scan bar */}
      <div className="flex items-center gap-3">
        <FlowScanBar
          flowTicker={flowTicker}
          flowLoading={flowLoading}
          countdown={countdown}
          onScan={handleScan}
        />
        {flowError && (
          <span className="text-xs font-mono" style={{ color: "var(--red)" }}>⚠ {flowError}</span>
        )}
      </div>

      <SimulationPanel
        result={result}
        loading={simLoading}
        error={simError}
        progress={progress}
      />
    </div>
  );
}

// ── Local: flow scan bar ──────────────────────────────────────────────────────
function FlowScanBar({
  flowTicker,
  flowLoading,
  countdown,
  onScan,
}: {
  flowTicker:  string;
  flowLoading: boolean;
  countdown:   number;
  onScan:      (t: string) => void;
}) {
  const [local, setLocal] = useState(flowTicker);
  useEffect(() => setLocal(flowTicker), [flowTicker]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const t = local.trim().toUpperCase();
    onScan(t);
  };

  return (
    <form onSubmit={submit} className="flex items-center gap-2">
      <input
        value={local}
        onChange={e => setLocal(e.target.value.toUpperCase())}
        placeholder="Filter by ticker…"
        maxLength={6}
        className="w-36 px-3 py-1.5 rounded-lg text-sm font-mono uppercase outline-none transition-all"
        style={{ background: "var(--surface-2)", border: "1px solid var(--border)", color: "var(--text)" }}
        onFocus={e => (e.target.style.borderColor = "var(--amber)")}
        onBlur={e  => (e.target.style.borderColor = "var(--border)")}
      />
      <button type="submit" disabled={flowLoading} className="btn btn-primary text-xs px-3 py-1.5">
        {flowLoading ? "Scanning…" : "Scan Flow"}
      </button>
      <span
        data-testid="flow-countdown"
        className="text-xs font-mono tabular"
        style={{ color: "var(--faint)" }}
      >
        Next refresh: {countdown}s
      </span>
    </form>
  );
}
