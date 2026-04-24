-- Migration 006: flow_events + flow_episodes tables with RLS
--
-- Creates both tables if they don't exist (idempotent).
-- Enables RLS on both tables.
-- Adds:
--   • anon SELECT policy  → frontend/backend can read via anon key
--   • service_role INSERT policy → backend writes via service role key
--
-- Run this in Supabase SQL editor or via supabase db push.
-- Safe to run multiple times.

-- ─────────────────────────────────────────────
-- flow_events: one row per classified options tick
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.flow_events (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker           TEXT        NOT NULL,
    contract_type    TEXT        NOT NULL,   -- CALL | PUT
    strike           NUMERIC(10,2),
    expiry           TEXT,                   -- YYYY-MM-DD
    premium          NUMERIC(14,2),
    trade_type       TEXT,                   -- SWEEP | BLOCK | SPLIT | SINGLE
    sentiment        TEXT,                   -- BULLISH | BEARISH | NEUTRAL
    influence_tier   TEXT,                   -- WHALE | INSTITUTIONAL | LARGE | RETAIL
    conviction_score NUMERIC(5,3) DEFAULT 0.0,
    is_golden_sweep  BOOLEAN      DEFAULT false,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_flow_events_ticker
    ON public.flow_events (ticker);
CREATE INDEX IF NOT EXISTS idx_flow_events_created_at
    ON public.flow_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_flow_events_ticker_created
    ON public.flow_events (ticker, created_at DESC);

-- ─────────────────────────────────────────────
-- flow_episodes: one row per signal episode
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.flow_episodes (
    id             BIGSERIAL   PRIMARY KEY,
    ticker         TEXT        NOT NULL,
    direction      TEXT,                   -- BULLISH | BEARISH | NEUTRAL
    contract_type  TEXT,                   -- CALL | PUT
    strike         NUMERIC(10,2),
    expiry         TEXT,                   -- YYYY-MM-DD
    total_premium  NUMERIC(14,2),
    trade_count    INTEGER,
    alert_level    TEXT,                   -- LOW | MEDIUM | HIGH | CRITICAL
    is_accelerating BOOLEAN     DEFAULT false,
    seed_episode   BOOLEAN     DEFAULT false,
    signal_ts      TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_flow_episodes_ticker
    ON public.flow_episodes (ticker);
CREATE INDEX IF NOT EXISTS idx_flow_episodes_created_at
    ON public.flow_episodes (created_at DESC);

-- ─────────────────────────────────────────────
-- RLS: flow_events
-- ─────────────────────────────────────────────
ALTER TABLE public.flow_events ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'flow_events' AND policyname = 'anon_select_flow_events'
  ) THEN
    CREATE POLICY anon_select_flow_events
      ON public.flow_events
      FOR SELECT
      TO anon, authenticated
      USING (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'flow_events' AND policyname = 'service_insert_flow_events'
  ) THEN
    CREATE POLICY service_insert_flow_events
      ON public.flow_events
      FOR INSERT
      TO service_role
      WITH CHECK (true);
  END IF;
END $$;

-- ─────────────────────────────────────────────
-- RLS: flow_episodes
-- ─────────────────────────────────────────────
ALTER TABLE public.flow_episodes ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'flow_episodes' AND policyname = 'anon_select_flow_episodes'
  ) THEN
    CREATE POLICY anon_select_flow_episodes
      ON public.flow_episodes
      FOR SELECT
      TO anon, authenticated
      USING (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'flow_episodes' AND policyname = 'service_insert_flow_episodes'
  ) THEN
    CREATE POLICY service_insert_flow_episodes
      ON public.flow_episodes
      FOR INSERT
      TO service_role
      WITH CHECK (true);
  END IF;
END $$;

COMMENT ON TABLE public.flow_events IS
    'One row per classified options tick. Written by flow_store.py via service role key. '
    'Read by frontend via anon key (RLS SELECT policy allows this).';

COMMENT ON TABLE public.flow_episodes IS
    'One row per repetition signal episode. Written by flow_store.py via service role key. '
    'Read by frontend via anon key (RLS SELECT policy allows this).';
