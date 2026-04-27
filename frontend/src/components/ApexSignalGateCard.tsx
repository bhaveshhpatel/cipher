"use client";
/**
 * ApexSignalGateCard
 *
 * Admin card for the Apex Layer-1 aggression gate.
 * Displays current mode (HARD REJECT / SOFT PENALISE) and lets an admin
 * toggle it at runtime via PATCH /api/apex-gate.
 *
 * Named "Apex Signal Gate" in the UI — avoids internal jargon while still
 * being meaningful to ops staff.
 */
import { useCallback, useEffect, useState } from "react";

/* ── Palette (matches admin page) ───────────────────────── */
const A = {
  bg:           "#080c14",
  surface:      "#0e1422",
  surface2:     "#131927",
  border:       "#1e2d45",
  border2:      "#263754",
  text:         "#dce6f5",
  muted:        "#6b83a6",
  faint:        "#364d6b",
  cyan:         "#22d3ee",
  cyanDim:      "rgba(34,211,238,0.12)",
  cyanBorder:   "rgba(34,211,238,0.25)",
  indigo:       "#818cf8",
  indigoDim:    "rgba(129,140,248,0.12)",
  indigoBorder: "rgba(129,140,248,0.25)",
  amber:        "#fbbf24",
  amberDim:     "rgba(251,191,36,0.10)",
  amberBorder:  "rgba(251,191,36,0.25)",
  green:        "rgb(74,222,128)",
  greenDim:     "rgba(74,222,128,0.10)",
  greenBorder:  "rgba(74,222,128,0.25)",
  red:          "#f87171",
  redDim:       "rgba(248,113,113,0.10)",
  redBorder:    "rgba(248,113,113,0.25)",
};

/* ── API shape ───────────────────────────────────────────── */
export interface GateConfig {
  hard_reject:             boolean;
  source:                  "env" | "override";
  max_aggression_penalty:  number;
  flat_aggression_penalty: number;
  stats: {
    gate_total_seen:           number;
    gate_hard_rejected:        number;
    gate_soft_rejected:        number;
    gate_passed:               number;
    gate_flagged_aggression:   number;
    aggression_hard_reject:    boolean;
    [key: string]: unknown;
  };
}

/* ── Sub-components ──────────────────────────────────────── */

function StatBox({ label, value }: { label: string; value: string | number }) {
  return (
    <div
      className="rounded-lg p-3"
      style={{ background: A.surface2, border: `1px solid ${A.border}` }}
    >
      <p className="text-xs font-mono mb-1" style={{ color: A.muted }}>{label}</p>
      <p className="text-sm font-semibold font-mono tabular-nums" style={{ color: A.text }}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
    </div>
  );
}

/* ── Main component ──────────────────────────────────────── */

export function ApexSignalGateCard({ token }: { token: string | null }) {
  const [config,  setConfig]  = useState<GateConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [toggling, setToggling] = useState(false);
  const [err,     setErr]     = useState<string | null>(null);
  const [lastSaved, setLastSaved] = useState<string | null>(null);

  const fetchConfig = useCallback(async () => {
    if (!token) return;
    setErr(null);
    try {
      const res = await fetch("/api/apex-gate", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setConfig(await res.json());
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to fetch gate config");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { fetchConfig(); }, [fetchConfig]);

  const toggle = useCallback(async (hardReject: boolean) => {
    if (!token) return;
    setToggling(true);
    setErr(null);
    try {
      const res = await fetch("/api/apex-gate", {
        method:  "PATCH",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body:    JSON.stringify({ hard_reject: hardReject }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated: GateConfig = await res.json();
      setConfig(updated);
      setLastSaved(new Date().toLocaleTimeString());
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to update gate config");
    } finally {
      setToggling(false);
    }
  }, [token]);

  const isHard = config?.hard_reject ?? false;

  const modePill = isHard
    ? { label: "● HARD REJECT",  color: A.red,   bg: A.redDim,   border: A.redBorder }
    : { label: "● SOFT PENALISE", color: A.amber, bg: A.amberDim, border: A.amberBorder };

  return (
    <div
      data-testid="apex-signal-gate-card"
      className="rounded-xl p-6"
      style={{ background: A.surface, border: `1px solid ${A.border}` }}
    >
      {/* Header */}
      <div className="flex items-start justify-between mb-5">
        <div>
          <h2 className="text-base font-semibold font-mono tracking-tight" style={{ color: A.text }}>
            Apex Signal Gate
          </h2>
          <p className="text-xs mt-1 font-mono leading-relaxed" style={{ color: A.muted }}>
            Controls how non-aggressive option fills are handled.
            Soft mode applies a proportional conviction penalty;
            Hard mode drops the event entirely.
          </p>
        </div>
        {!loading && config && (
          <span
            data-testid="mode-pill"
            className="text-xs font-mono px-3 py-1 rounded-full shrink-0"
            style={{ background: modePill.bg, border: `1px solid ${modePill.border}`, color: modePill.color }}
          >
            {modePill.label}
          </span>
        )}
      </div>

      {/* Error */}
      {err && (
        <p
          data-testid="error-msg"
          className="text-xs font-mono p-3 rounded mb-4"
          style={{ color: A.red, background: A.redDim, border: `1px solid ${A.redBorder}` }}
        >
          {err}
        </p>
      )}

      {/* Loading */}
      {loading && (
        <p className="text-xs font-mono" style={{ color: A.muted }}>Loading…</p>
      )}

      {config && !loading && (
        <>
          {/* Config metadata */}
          <div className="grid grid-cols-2 gap-3 mb-4">
            <StatBox
              label="Source"
              value={config.source === "override" ? "Runtime override" : "Env var default"}
            />
            <StatBox
              label="Max Penalty Cap"
              value={`${(config.max_aggression_penalty * 100).toFixed(0)}%`}
            />
          </div>

          {/* Stats */}
          <div className="grid grid-cols-3 gap-3 mb-5">
            <StatBox label="Total Seen"       value={config.stats.gate_total_seen} />
            <StatBox label="Passed"           value={config.stats.gate_passed} />
            <StatBox label="Aggression Flags" value={config.stats.gate_flagged_aggression} />
          </div>

          {/* Toggle buttons */}
          <div className="flex gap-3">
            <button
              data-testid="btn-soft"
              onClick={() => toggle(false)}
              disabled={toggling || !isHard}
              className="flex-1 py-2 rounded text-xs font-mono font-semibold transition-colors"
              style={{
                background: !isHard ? A.amberDim  : A.surface2,
                color:      !isHard ? A.amber     : A.muted,
                border:     !isHard ? `1px solid ${A.amberBorder}` : `1px solid ${A.border}`,
                cursor:     toggling || !isHard ? "not-allowed" : "pointer",
              }}
            >
              ⚡ Soft Penalise
            </button>
            <button
              data-testid="btn-hard"
              onClick={() => toggle(true)}
              disabled={toggling || isHard}
              className="flex-1 py-2 rounded text-xs font-mono font-semibold transition-colors"
              style={{
                background: isHard  ? A.redDim    : A.surface2,
                color:      isHard  ? A.red       : A.muted,
                border:     isHard  ? `1px solid ${A.redBorder}` : `1px solid ${A.border}`,
                cursor:     toggling || isHard ? "not-allowed" : "pointer",
              }}
            >
              🔴 Hard Reject
            </button>
          </div>

          {lastSaved && (
            <p className="text-xs font-mono mt-3" style={{ color: A.faint }}>
              Last updated: {lastSaved}
            </p>
          )}
        </>
      )}
    </div>
  );
}
