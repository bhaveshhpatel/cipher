"use client";
import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import type { CompositeSignal } from "@/lib/api";
import { CompositeCard } from "@/components/dashboard/CompositeCard";

interface Props { token: string | null; }

export function CompositePage({ token }: Props) {
  const [composite,        setComposite]        = useState<CompositeSignal | null>(null);
  const [compositeTicker,  setCompositeTicker]  = useState("");
  const [compositeLoading, setCompositeLoading] = useState(false);
  const [compositeError,   setCompositeError]   = useState<string | null>(null);

  const handleScan = async (t: string) => {
    if (!token || !t) return;
    setCompositeTicker(t);
    setCompositeLoading(true);
    setCompositeError(null);
    try {
      const c = await api.getComposite(t, token);
      setComposite(c);
    } catch (e) {
      setComposite(null);
      setCompositeError(e instanceof Error ? e.message : "Failed to load composite");
    } finally {
      setCompositeLoading(false);
    }
  };

  const handleClear = () => {
    setComposite(null);
    setCompositeTicker("");
    setCompositeError(null);
  };

  return (
    <div className="flex flex-col gap-4" data-testid="composite-page">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-bold" style={{ color: "var(--text)" }}>Composite Signal</h1>
          <p className="text-sm mt-0.5 font-mono" style={{ color: "var(--muted)" }}>
            Multi-factor scoring: flow + backtest + swarm consensus
          </p>
        </div>
        <TickerSearchBar
          placeholder="Enter ticker…"
          onScan={handleScan}
          onClear={handleClear}
          loading={compositeLoading}
          activeTicker={compositeTicker}
          scanLabel="Analyze"
        />
      </div>

      {compositeError && (
        <div
          data-testid="composite-error"
          className="px-4 py-3 rounded-lg text-sm font-mono"
          style={{ background: "rgba(220,53,69,0.07)", color: "var(--red)", border: "1px solid rgba(220,53,69,0.2)" }}
        >
          ⚠ {compositeError}
        </div>
      )}

      {!compositeTicker && !composite && !compositeError && (
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
  );
}

// ── TickerSearchBar (previously inline in page.tsx) ───────────────────────────
export function TickerSearchBar({
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
  useEffect(() => setLocal(activeTicker), [activeTicker]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const t = local.trim().toUpperCase();
    if (t) onScan(t);
  };

  return (
    <form onSubmit={submit} className="flex items-center gap-2" data-testid="ticker-search-bar">
      <input
        data-testid="ticker-input"
        value={local}
        onChange={e => setLocal(e.target.value.toUpperCase())}
        placeholder={placeholder}
        maxLength={6}
        className="w-28 px-3 py-1.5 rounded-lg text-sm font-mono font-semibold uppercase outline-none transition-all"
        style={{ background: "var(--surface-2)", border: "1px solid var(--border)", color: "var(--text)" }}
        onFocus={e => (e.target.style.borderColor = "var(--amber)")}
        onBlur={e  => (e.target.style.borderColor = "var(--border)")}
      />
      <button type="submit" disabled={loading} className="btn btn-primary text-xs px-3 py-1.5" data-testid="ticker-submit">
        {loading ? (
          <svg className="animate-spin" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <path d="M21 12a9 9 0 1 1-6.219-8.56" />
          </svg>
        ) : scanLabel}
      </button>
      {activeTicker && (
        <button
          type="button"
          data-testid="ticker-clear"
          onClick={() => { setLocal(""); onClear(); }}
          className="text-xs px-2 py-1.5 rounded-md transition-all"
          style={{ color: "var(--muted)", background: "var(--surface-2)", border: "1px solid var(--border)" }}
          title="Clear filter — show all"
        >
          ✕ All
        </button>
      )}
    </form>
  );
}
