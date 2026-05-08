/**
 * gates.ts — TypeScript types for the Ingestion Gate Control Panel
 * ADMIN-UI-001
 */

export interface GateRow {
  gate_name: string;
  tier: 1 | 2 | 3;
  value: number;
  min_value: number;
  max_value: number;
  tier_independent: boolean;
}

export interface GateConfigResponse {
  epoch: number;
  gates: GateRow[];
}

export interface PatchGatePayload {
  gate_name: string;
  tier: 1 | 2 | 3;
  value: number;
  reason?: string | null;
  confirm_market_hours: boolean;
}

export interface PatchGateResponse {
  gate_name: string;
  tier: 1 | 2 | 3;
  new_value: number;
  epoch: number;
}

export type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';

/** Maps canonical gate_name → human-readable label */
export const GATE_LABELS: Record<string, string> = {
  min_premium:          'Minimum Premium Floor',
  dte_floor_multiplier: 'DTE Floor Multiplier',
  dedup_window_ms:      'Dedup Window',
  require_oi:           'Require Open Interest',
  signal_debounce_ms:   'Signal Debounce',
  signal_min_premium:   'Signal Minimum Premium',
  exclude_indices:      'Exclude Index ETFs',
};

/** Gates whose display value is divided/multiplied by 1000 (ms ↔ seconds) */
export const MS_GATES = new Set(['dedup_window_ms', 'signal_debounce_ms']);

/** Gates rendered as ON/OFF toggle instead of a number input */
export const TOGGLE_GATES = new Set(['require_oi', 'exclude_indices']);

/** Gates that get a '$' prefix with comma-formatted integer display */
export const DOLLAR_GATES = new Set(['min_premium', 'signal_min_premium']);

/** Ordered list of gate_names for stable card rendering */
export const GATE_ORDER = [
  'min_premium',
  'dte_floor_multiplier',
  'dedup_window_ms',
  'require_oi',
  'signal_debounce_ms',
  'signal_min_premium',
  'exclude_indices',
];
