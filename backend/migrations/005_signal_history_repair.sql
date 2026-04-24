-- Migration 005: signal_history repair
--
-- Handles two scenarios:
--   A) signal_feed_log exists (old/renamed table) → rename it to signal_history
--      and backfill any missing columns
--   B) Neither table exists → create signal_history from scratch
--
-- This is safe to run multiple times (idempotent).

DO $$
BEGIN
  -- Scenario A: signal_feed_log exists but signal_history does not
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'signal_feed_log'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'signal_history'
  ) THEN
    ALTER TABLE public.signal_feed_log RENAME TO signal_history;
    RAISE NOTICE 'Renamed signal_feed_log → signal_history';
  END IF;
END
$$;

-- Scenario B (and post-rename): ensure the table exists with all required columns
CREATE TABLE IF NOT EXISTS signal_history (
    id                    BIGSERIAL PRIMARY KEY,
    ticker                TEXT        NOT NULL,
    recommendation        TEXT        NOT NULL,
    composite_score       NUMERIC(5,3) NOT NULL,
    flow_score            NUMERIC(5,3) NOT NULL,
    backtest_score        NUMERIC(5,3) NOT NULL,
    volume_premium_factor NUMERIC(5,3) NOT NULL DEFAULT 0.5,
    reasoning             TEXT,
    contract_type         TEXT,
    direction             TEXT,
    influence_tier        TEXT,
    total_premium         NUMERIC(14,2),
    trade_count           INTEGER,
    is_accelerating       BOOLEAN     NOT NULL DEFAULT false,
    signal_ts             TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Backfill any columns that may be missing if the table was renamed
-- from an older schema (signal_feed_log may have had different columns)
ALTER TABLE signal_history
  ADD COLUMN IF NOT EXISTS recommendation        TEXT        DEFAULT 'HOLD',
  ADD COLUMN IF NOT EXISTS composite_score       NUMERIC(5,3) DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS flow_score            NUMERIC(5,3) DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS backtest_score        NUMERIC(5,3) DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS volume_premium_factor NUMERIC(5,3) DEFAULT 0.5,
  ADD COLUMN IF NOT EXISTS reasoning             TEXT,
  ADD COLUMN IF NOT EXISTS contract_type         TEXT,
  ADD COLUMN IF NOT EXISTS direction             TEXT,
  ADD COLUMN IF NOT EXISTS influence_tier        TEXT,
  ADD COLUMN IF NOT EXISTS total_premium         NUMERIC(14,2),
  ADD COLUMN IF NOT EXISTS trade_count           INTEGER,
  ADD COLUMN IF NOT EXISTS is_accelerating       BOOLEAN     DEFAULT false,
  ADD COLUMN IF NOT EXISTS signal_ts             TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS created_at            TIMESTAMPTZ DEFAULT now();

-- Phase 5A swarm fields (idempotent — same as migration 004)
ALTER TABLE signal_history
  ADD COLUMN IF NOT EXISTS swarm_direction   TEXT    DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS swarm_confidence  NUMERIC DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS swarm_agents      JSONB   DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS swarm_bull_votes  INTEGER DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS swarm_bear_votes  INTEGER DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS swarm_hold_votes  INTEGER DEFAULT NULL;

-- Indexes (idempotent)
CREATE INDEX IF NOT EXISTS idx_signal_history_ticker
    ON signal_history (ticker);
CREATE INDEX IF NOT EXISTS idx_signal_history_recommendation
    ON signal_history (recommendation);
CREATE INDEX IF NOT EXISTS idx_signal_history_composite_score
    ON signal_history (composite_score DESC);
CREATE INDEX IF NOT EXISTS idx_signal_history_created_at
    ON signal_history (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_history_direction
    ON signal_history (direction);
CREATE INDEX IF NOT EXISTS idx_signal_history_influence_tier
    ON signal_history (influence_tier);
CREATE INDEX IF NOT EXISTS idx_signal_history_rec_score
    ON signal_history (recommendation, composite_score DESC, created_at DESC);

-- Enable RLS
ALTER TABLE signal_history ENABLE ROW LEVEL SECURITY;

-- Service role can insert; anon key cannot
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'signal_history' AND policyname = 'service_insert'
  ) THEN
    CREATE POLICY service_insert ON signal_history
      FOR INSERT TO service_role WITH CHECK (true);
  END IF;
END
$$;

COMMENT ON TABLE signal_history IS
    'Persisted CompositeSignal records from composite_signal_engine. '
    'Written exclusively by signal_store.py using SUPABASE_SERVICE_KEY. '
    'Never insert from anon key — RLS will reject with 42501.';
