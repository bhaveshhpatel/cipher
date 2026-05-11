-- REARCH-003: Add event quality tag columns to flow_events
-- Migration 026
-- Safe to run multiple times (IF NOT EXISTS guards).
-- bid_ask_class already exists -- no ADD needed for that column.

ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS is_ask_side        BOOLEAN  NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS vol_oi_signal       TEXT     NOT NULL DEFAULT 'UNKNOWN',
  ADD COLUMN IF NOT EXISTS normalized_premium  NUMERIC(18, 4);

COMMENT ON COLUMN flow_events.is_ask_side IS
  'True when fill_price >= ask * 0.98 (ask-side aggression). Computed at persist time.';

COMMENT ON COLUMN flow_events.vol_oi_signal IS
  'HIGH when intraday contract_volume / contract_oi >= 0.5, NORMAL otherwise, UNKNOWN on cache miss.';

COMMENT ON COLUMN flow_events.normalized_premium IS
  'premium / underlying_price. NULL when underlying_price is 0 or NULL.';
