/**
 * GateControlPanel.tsx — 5×3 gate threshold table for the admin panel.
 * ADMIN-UI-001 | Chunk 3
 *
 * Renders a row per gate_name × column per tier (T1 / T2 / T3).
 * Each cell is a <GateCellInput> connected to useGateConfig (read) and
 * useGatePatch (write). tier_independent gates show the same value across
 * all 3 tiers but still allow independent edits.
 *
 * Auth guard: requires token + isAdmin; renders an access-denied banner
 * if either is missing so the component is safely embeddable in any layout.
 *
 * 428 market-hours flow:
 *   patch()        → backend returns 428 → status → "market_confirm"
 *   GateCellInput  → amber inline banner with Confirm / Cancel
 *   onConfirm      → confirmPatch() re-sends with confirm_market_hours: true
 *   onCancelConfirm→ cancelConfirm() resets status to idle
 *
 * Error surfacing: per-cell error messages live in useGatePatch.errorMap
 * and are passed to GateCellInput as `saveError`.
 */
"use client";
import React from "react";
import {
  A,
  AdminCard,
  CardHeader,
  ErrorBanner,
} from "./_shared";
import {
  GATE_LABELS,
  GATE_ORDER,
} from "@/types/gates";
import type { GateRow } from "@/types/gates";
import { useGateConfig } from "@/hooks/useGateConfig";
import { useGatePatch }  from "@/hooks/useGatePatch";
import { GateCellInput } from "./GateCellInput";

export interface GateControlPanelProps {
  token:   string | null;
  isAdmin: boolean;
}

const TIERS = [1, 2, 3] as const;

/** Group a flat GateRow[] into a map: gate_name → {1: row, 2: row, 3: row} */
function groupByGate(gates: GateRow[]): Map<string, Record<1 | 2 | 3, GateRow>> {
  const map = new Map<string, Record<1 | 2 | 3, GateRow>>();
  for (const row of gates) {
    if (!map.has(row.gate_name)) {
      map.set(row.gate_name, {} as Record<1 | 2 | 3, GateRow>);
    }
    map.get(row.gate_name)![row.tier] = row;
  }
  return map;
}

export function GateControlPanel({ token, isAdmin }: GateControlPanelProps) {
  const { data, loading, error, refresh }                     = useGateConfig(token);
  const { statusMap, errorMap, patch, confirmPatch, cancelConfirm } = useGatePatch();

  // ── Auth guard ────────────────────────────────────────────
  if (!token || !isAdmin) {
    return (
      <AdminCard>
        <CardHeader
          title="Gate Control Panel"
          subtitle="Ingestion gate thresholds — admin only"
        />
        <ErrorBanner msg="Access denied — admin role required." />
      </AdminCard>
    );
  }

  // ── Loading ─────────────────────────────────────────────
  if (loading && !data) {
    return (
      <AdminCard>
        <CardHeader
          title="Gate Control Panel"
          subtitle="Ingestion gate thresholds — 5 gates × 3 tiers"
        />
        <p data-testid="loading-msg" className="text-xs font-mono" style={{ color: A.muted }}>
          Loading…
        </p>
      </AdminCard>
    );
  }

  // ── Fetch error (no data yet) ───────────────────────────────
  if (error && !data) {
    return (
      <AdminCard>
        <CardHeader
          title="Gate Control Panel"
          subtitle="Ingestion gate thresholds — 5 gates × 3 tiers"
        />
        <ErrorBanner msg={error} />
      </AdminCard>
    );
  }

  const grouped = groupByGate(data?.gates ?? []);

  const gateNames = GATE_ORDER.filter(name => grouped.has(name));
  grouped.forEach((_, name) => {
    if (!GATE_ORDER.includes(name)) {
      console.warn(
        `[GateControlPanel] API returned unknown gate "${name}" — add it to GATE_ORDER in @/types/gates.ts`,
      );
    }
  });

  async function handleSave(
    gateName: string,
    tier: 1 | 2 | 3,
    newValue: number,
    reason: string | null,
  ) {
    if (!token) return;
    try {
      const result = await patch(token, {
        gate_name: gateName,
        tier,
        value:     newValue,
        reason,
        confirm_market_hours: false,
      });
      // null result means 428 — status is already "market_confirm", no refresh yet.
      if (result) refresh();
    } catch {
      // Error message is in errorMap[key] — no local state needed.
    }
  }

  async function handleConfirm(gateName: string, tier: 1 | 2 | 3) {
    try {
      const result = await confirmPatch(gateName, tier);
      if (result) refresh();
    } catch {
      // Error surfaced via errorMap.
    }
  }

  return (
    <AdminCard>
      <CardHeader
        title="Gate Control Panel"
        subtitle={`Ingestion gate thresholds — epoch ${data?.epoch ?? "—"}`}
        action={
          <button
            data-testid="refresh-btn"
            onClick={refresh}
            disabled={loading}
            className="text-xs font-mono px-3 py-1.5 rounded transition-colors"
            style={{
              background: A.surface2,
              border:     `1px solid ${A.border}`,
              color:      loading ? A.faint : A.muted,
              cursor:     loading ? "not-allowed" : "pointer",
              opacity:    loading ? 0.5 : 1,
            }}
          >
            {loading ? "…" : "↻ Refresh"}
          </button>
        }
      />

      {error && data && <ErrorBanner msg={`⚠ ${error}`} />}

      <div className="overflow-x-auto" role="table" aria-label="Gate Control Panel">
        {/* Header row */}
        <div
          role="row"
          className="grid gap-3 mb-2 text-xs font-mono"
          style={{ gridTemplateColumns: "200px 1fr 1fr 1fr", color: A.muted }}
        >
          <span role="columnheader">Gate</span>
          <span role="columnheader">Tier 1</span>
          <span role="columnheader">Tier 2</span>
          <span role="columnheader">Tier 3</span>
        </div>

        {gateNames.length === 0 && (
          <p data-testid="empty-msg" className="text-xs font-mono py-4" style={{ color: A.muted }}>
            No gate configuration found.
          </p>
        )}

        {gateNames.map(gateName => {
          const tierMap = grouped.get(gateName)!;
          const label   = GATE_LABELS[gateName] ?? gateName;
          return (
            <div
              key={gateName}
              data-testid={`gate-row-${gateName}`}
              role="row"
              className="grid gap-3 py-3 items-start"
              style={{
                gridTemplateColumns: "200px 1fr 1fr 1fr",
                borderTop: `1px solid ${A.border}`,
              }}
            >
              <span
                role="rowheader"
                className="text-xs font-mono pt-1 leading-tight"
                style={{ color: A.text }}
              >
                {label}
                {tierMap[1]?.tier_independent && (
                  <span className="block text-xs mt-0.5" style={{ color: A.faint }}>
                    (tier-independent)
                  </span>
                )}
              </span>
              {TIERS.map(tier => {
                const row     = tierMap[tier];
                const cellKey = `${gateName}:${tier}`;
                const status  = statusMap[cellKey] ?? "idle";
                const saveErr = errorMap[cellKey];
                if (!row) return <div key={tier} />;
                return (
                  <GateCellInput
                    key={tier}
                    row={row}
                    status={status}
                    saveError={saveErr}
                    onSave={(newValue, reason) =>
                      handleSave(gateName, tier, newValue, reason)
                    }
                    onConfirm={() => handleConfirm(gateName, tier)}
                    onCancelConfirm={() => cancelConfirm(gateName, tier)}
                  />
                );
              })}
            </div>
          );
        })}
      </div>
    </AdminCard>
  );
}
