-- REARCH-003: Add event quality tag columns to flow_events
-- Migration 026
-- Safe to run multiple times (IF NOT EXISTS guards).
-- bid_ask_class already exists — no ADD needed for that column.
--
-- SA-1 fix (2026-05-11): vol_oi_signal was TEXT NOT NULL DEFAULT 'UNKNOWN'.
--   flow_store.py computes this as Python bool | None via compute_vol_oi_signal().
--   A TEXT column rejects Python True/False — PostgREST returns 400 on every
--   event insert. Fixed to BOOLEAN DEFAULT NULL (NULL = cache miss, per deliberation).
--
-- SA-2 fix (2026-05-11): normalized_oi was NUMERIC(18,6).
--   _compute_vol_oi_ratio() rounds to 4dp — extra precision digits are wasted storage.
--   Fixed to NUMERIC(18,4) to match the rounding contract in flow_store.py.
--
-- normalized_oi: added here because flow_store.py writes this key in the row dict.
--   Missing column would cause a PostgREST 400 on insert.

ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS is_ask_side           BOOLEAN  NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS vol_oi_signal          BOOLEAN  DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS normalized_premium     NUMERIC(18, 4),
  ADD COLUMN IF NOT EXISTS normalized_oi          NUMERIC(18, 4);

COMMENT ON COLUMN flow_events.is_ask_side IS
  'True when fill_price >= ask * 0.98 (ask-side aggression). Computed at persist time by classify_bid_ask().';

COMMENT ON COLUMN flow_events.vol_oi_signal IS
  'True when intraday contract_volume / contract_oi >= 0.5. False when below threshold. '
  'NULL when vol or OI snapshot is unavailable (cache miss). Never a gate condition.';

COMMENT ON COLUMN flow_events.normalized_premium IS
  'premium / underlying_price. NULL when underlying_price is 0 or NULL.';

COMMENT ON COLUMN flow_events.normalized_oi IS
  'open_interest (event-level OI from Tradier) / contract_oi (chain_store intraday snapshot). '
  'Rounded to 4dp via _compute_vol_oi_ratio(). NULL when contract_oi is unavailable or zero.';

-- Index: vol_oi_signal TRUE rows are the primary signal-engine read target.
-- CONCURRENTLY means the migration does not lock the table for reads/writes.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_flow_events_vol_oi_high
  ON flow_events (vol_oi_signal)
  WHERE vol_oi_signal IS TRUE;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_flow_events_ask_side
  ON flow_events (is_ask_side)
  WHERE is_ask_side IS TRUE;
