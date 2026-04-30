"use client";
import { useEffect, useState, useCallback } from "react";
import { A, AdminCard, Stat } from "./_shared";

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

const MODE_COLOR: Record<string, { color: string; bg: string; border: string }> = {
  live:          { color: A.green,  bg: A.greenDim,  border: A.greenBorder  },
  demo:          { color: A.cyan,   bg: A.cyanDim,   border: A.cyanBorder   },
  starting:      { color: A.amber,  bg: A.amberDim,  border: A.amberBorder  },
  reconnecting:  { color: A.amber,  bg: A.amberDim,  border: A.amberBorder  },
  market_closed: { color: A.muted,  bg: A.surface2,  border: A.border       },
  idle:          { color: A.muted,  bg: A.surface2,  border: A.border       },
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

export function StreamHealthCard({ token }: { token: string | null }) {
  const [health,      setHealth]      = useState<StreamHealth | null>(null);
  const [loading,     setLoading]     = useState(true);
  const [fetchErr,    setFetchErr]    = useState<string | null>(null);
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

  useEffect(() => {
    fetchHealth();
    const id = setInterval(fetchHealth, 10_000);
    return () => clearInterval(id);
  }, [fetchHealth]);

  const mode  = health?.mode ?? "unknown";
  const modeC = MODE_COLOR[mode] ?? { color: A.muted, bg: A.surface2, border: A.border };

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
                background:    modeC.bg,
                border:        `1px solid ${modeC.border}`,
                color:         modeC.color,
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

      {loading && <p className="text-xs font-mono" style={{ color: A.muted }}>Loading…</p>}
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
          <div className="grid grid-cols-3 gap-3 mb-4">
            <Stat label="Active Symbols" value={health.active_symbols} />
            <Stat label="Ticks"          value={health.ticks} />
            <Stat label="Classified"     value={health.classified} />
            <Stat label="Deduped"        value={health.deduped} />
            <Stat label="Signals"        value={health.signals} />
            <Stat label="Errors"         value={health.errors} />
          </div>
          <div className="grid grid-cols-2 gap-3 mb-4">
            <Stat label="Reconnects" value={health.reconnects} />
            <Stat label="Uptime"     value={fmtUptime(health.uptime_seconds)} />
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
