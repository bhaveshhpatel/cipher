-- Migration: 001_options_universe
-- Creates the options universe snapshot tables for persisting
-- the validated tradeable options symbol universe.
--
-- options_universe_snapshots  — one row per validated snapshot
-- options_universe_symbols    — normalized symbol membership per snapshot
--
-- Schema reflects actual production columns (updated 2026-04-23, C-006).
-- Columns added vs original: provider (NOT NULL), refresh_reason, meta, created_at.

CREATE TABLE IF NOT EXISTS options_universe_snapshots (
  id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  symbol_count   INT         NOT NULL DEFAULT 0,
  provider       TEXT        NOT NULL,
  source         TEXT        NOT NULL
    CHECK (source IN ('tradier_validated', 'seed_fallback', 'cache')),
  is_active      BOOLEAN     NOT NULL DEFAULT false,
  refresh_reason TEXT        NOT NULL DEFAULT 'startup',
  meta           JSONB       NOT NULL DEFAULT '{}',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS options_universe_symbols (
  snapshot_id  UUID NOT NULL
    REFERENCES options_universe_snapshots(id) ON DELETE CASCADE,
  symbol       TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, symbol)
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_universe_snapshots_active
  ON options_universe_snapshots (is_active, fetched_at DESC);

CREATE INDEX IF NOT EXISTS idx_universe_symbols_snapshot
  ON options_universe_symbols (snapshot_id);

-- Only one snapshot should be active at a time.
-- Enforced in application code (universe_store.py) but this partial unique
-- index provides a DB-level safety net.
CREATE UNIQUE INDEX IF NOT EXISTS idx_universe_snapshots_single_active
  ON options_universe_snapshots (is_active)
  WHERE is_active = true;
