-- Migration 012: Catch-up schema delta — codify live schema post-011
--
-- Context
-- -------
-- All DDL in this file was applied directly to the Supabase project after
-- migration 011_unique_constraints.sql without a corresponding migration
-- file in this repo. This migration closes the schema drift so that:
--
--   1. Fresh Supabase environments / branches reproduce the live schema
--   2. A database reset does not lose these columns and indexes
--   3. GitHub issue tracking for #67 and #69 (S2.5 migration) is resolved
--
-- Every statement is guarded with IF NOT EXISTS so it is a no-op on the
-- live production database and safe to run during CI against a fresh DB.
--
-- Tables affected
-- ---------------
--   flow_events   — ING-006 aggression + ING-007 order-side classifier columns
--   flow_episodes — S12 multi-day repeat flag + ING-006 aggression flag
--
-- Indexes
-- -------
--   idx_flow_events_is_aggressive     — ING-007 lookback stratification
--   idx_flow_events_contract_day      — ING-007 compound lookback query
--   idx_flow_events_order_side        — order-side classifier queries
--   idx_flow_episodes_is_aggressive   — episode-level aggression filter

-- ==========================================================================
-- flow_events
-- ==========================================================================

-- ING-006: directional aggression flag (S2.5 — filed in #67, #69)
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS is_aggressive BOOLEAN NOT NULL DEFAULT FALSE;

-- ING-005 / golden sweep gate
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS is_golden_sweep BOOLEAN NOT NULL DEFAULT FALSE;

-- OCC symbol string (e.g. AAPL  260117C00200000) for contract identity
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS occ_symbol TEXT;

-- Quote provenance — distinguishes live Tradier stream vs synthetic fills
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS is_synthetic_quote BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS quote_source TEXT NOT NULL DEFAULT 'live';

-- MIAX-normalised exchange count (raw exchange_count can double-count MIAX legs)
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS miax_normalized_exchange_count INTEGER NOT NULL DEFAULT 1;

-- ING-006 / order_side_classifier.py output (BUY | SELL | UNKNOWN)
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS order_side TEXT NOT NULL DEFAULT 'UNKNOWN';

-- ING-006: strong_sentiment flag (high-conviction directional classification)
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS strong_sentiment BOOLEAN NOT NULL DEFAULT FALSE;

-- ING-006 / ING-007: execution mechanic classification
-- (AGGRESSIVE_BUY | AGGRESSIVE_SELL | PASSIVE_BUY | PASSIVE_SELL | AMBIGUOUS_LONG | ...)
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS execution_mechanic TEXT NOT NULL DEFAULT 'AMBIGUOUS_LONG';

-- ==========================================================================
-- flow_episodes
-- ==========================================================================

-- S12: multi-day repeat conviction flag
ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS is_multi_day_repeat BOOLEAN NOT NULL DEFAULT FALSE;

-- ING-006: episode-level aggression flag (mirrors flow_events.is_aggressive
-- but aggregated at the episode level — TRUE when weighted-aggressive tick
-- count meets Gate-2 threshold in repetition_accumulator.py)
ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS is_aggressive BOOLEAN NOT NULL DEFAULT FALSE;

-- ==========================================================================
-- Indexes
-- ==========================================================================

-- ING-007 lookback query: filter flow_events by ticker + aggression + recency
CREATE INDEX IF NOT EXISTS idx_flow_events_is_aggressive
  ON flow_events (ticker, is_aggressive, created_at DESC);

-- ING-007 compound lookback: full contract-day window query
CREATE INDEX IF NOT EXISTS idx_flow_events_contract_day
  ON flow_events (ticker, contract_type, strike, expiry, created_at DESC);

-- order-side classifier queries
CREATE INDEX IF NOT EXISTS idx_flow_events_order_side
  ON flow_events (ticker, order_side, created_at DESC);

-- Episode-level aggression filter (S8 backtest stratification)
CREATE INDEX IF NOT EXISTS idx_flow_episodes_is_aggressive
  ON flow_episodes (ticker, is_aggressive, created_at DESC);
