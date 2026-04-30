/**
 * Cipher — consolidated shared types.
 * Re-exports types from api.ts and adds UI-specific types.
 * All API response shapes live in lib/api.ts — import from there directly
 * when you need the full API client.
 */

// ── Re-export API types ──
export type {
  FlowEvent,
  FlowEventRaw,
  FlowEpisode,
  FlowEventsResponse,
  FlowEpisodesResponse,
  SimulationResult,
  CompositeSignal,
  StreamStats,
  SignalHistoryItem,
  SignalHistoryResponse,
} from "@/lib/api";

// ── Dashboard navigation ──
export const DASHBOARD_TABS = [
  "flow_events",
  "flow_episodes",
  "signals",
  "simulation",
  "composite",
  "history",
] as const;

export type DashboardTab = (typeof DASHBOARD_TABS)[number];

export const TAB_META: Record<DashboardTab, { label: string; icon: string; shortLabel: string }> = {
  flow_events:   { label: "Flow Events",   icon: "⟁", shortLabel: "Flow"      },
  flow_episodes: { label: "Episodes",       icon: "◎", shortLabel: "Episodes"  },
  signals:       { label: "Live Signals",   icon: "◉", shortLabel: "Signals"   },
  simulation:    { label: "AI Simulation",  icon: "⬡", shortLabel: "Sim"       },
  composite:     { label: "Composite",      icon: "◈", shortLabel: "Composite" },
  history:       { label: "Signal History", icon: "🕐", shortLabel: "History"   },
};

// ── Admin types (migrated from admin/page.tsx) ──
export interface ConfigRow {
  key:         string;
  value:       string;
  value_type:  string;
  description: string;
  updated_at:  string;
  updated_by:  string | null;
}

export interface TierThresholdsRow {
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

export interface CacheMeta {
  warm:        boolean;
  age_seconds: number | null;
  ttl_seconds: number;
}

export interface StreamHealth {
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

export interface TierDistributionSample {
  symbol:        string;
  open_interest: number | null;
}

export interface TierDistributionTier {
  count:   number;
  samples: TierDistributionSample[];
}

export interface TierDistribution {
  snapshot_id: string;
  total:       number;
  tiers: {
    "1": TierDistributionTier;
    "2": TierDistributionTier;
    "3": TierDistributionTier;
  };
}

// ── Stream / signal feed ──
export type StreamMode = "live" | "demo" | "reconnecting" | "stopped" | "unknown";
export type MarketStatus = "open" | "closed" | "pre" | "after";

export interface MarketStatusInfo {
  status: MarketStatus;
  label:  string;
  color:  string;
}

// ── UI component helpers ──
export type Verdict = "BUY" | "SELL" | "HOLD";
export type Tier    = 1 | 2 | 3;
export type Size    = "sm" | "md" | "lg";
