-- REARCH-004: Episode Quality Aggregate Columns
-- Migration 028
--
-- Adds four aggregate quality columns to flow_episodes:
--   ask_side_count  — running count of ask-side events in this episode
--   ask_side_pct    — ask_side_count / trade_count (recomputed on every PATCH)
--   dte_bucket      — DTE bucket locked at episode open (seed-event-only, SA-3)
--   notional_tier   — notional tier locked at episode open (seed-event-only, SA-3)
--
-- NULL contract (PBE-1): no backfill — existing rows stay NULL.
-- PATCH paths use COALESCE(ask_side_count, 0) before incrementing.
--
-- TODO: index candidate for REARCH-006:
--   CREATE INDEX ON flow_episodes(ask_side_pct);

ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS ask_side_count SMALLINT DEFAULT NULL;

ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS ask_side_pct NUMERIC(5,4) DEFAULT NULL;

ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS dte_bucket TEXT DEFAULT NULL;

ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS notional_tier TEXT DEFAULT NULL;
