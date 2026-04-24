-- Migration: 003_flow_events_expiry_nullable
-- Already applied to Supabase on 2026-04-24 (C-010 fix).
-- Tracked here for repo completeness and local dev reproducibility.
--
-- Root cause: Tradier streaming events do not include expiration_date or
-- strike at the top level — they are embedded in the OCC option symbol
-- string (e.g. 'AAPL  260117C00180000'). When OCC parsing failed (because
-- `symbol` was mistakenly treated as the underlying ticker), expiry arrived
-- as "" and strike as 0. The NOT NULL DATE constraint on flow_events.expiry
-- rejected every row, causing silent insert failures.
--
-- This migration:
--   1. Changes flow_events.expiry  → TEXT, nullable
--   2. Makes  flow_events.strike   nullable
--   3. Makes  flow_episodes.expiry nullable
--   4. Makes  flow_episodes.strike nullable
--   5. Adds composite indexes for contract_type query patterns

-- 1. flow_events.expiry: DATE NOT NULL → TEXT nullable
ALTER TABLE flow_events ALTER COLUMN expiry DROP NOT NULL;
ALTER TABLE flow_events ALTER COLUMN expiry TYPE TEXT USING expiry::text;

-- 2. flow_events.strike: nullable
ALTER TABLE flow_events ALTER COLUMN strike DROP NOT NULL;

-- 3. flow_episodes.expiry: nullable
ALTER TABLE flow_episodes ALTER COLUMN expiry DROP NOT NULL;

-- 4. flow_episodes.strike: nullable
ALTER TABLE flow_episodes ALTER COLUMN strike DROP NOT NULL;

-- 5. Composite indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_flow_events_ticker_contract
  ON flow_events (ticker, contract_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_flow_episodes_ticker_contract
  ON flow_episodes (ticker, contract_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_flow_events_contract_type
  ON flow_events (contract_type);

CREATE INDEX IF NOT EXISTS idx_flow_episodes_direction
  ON flow_episodes (direction);
