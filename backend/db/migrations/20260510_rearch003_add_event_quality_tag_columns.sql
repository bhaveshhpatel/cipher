-- REARCH-003 | Chunk 3a: Add event quality tag columns to flow_events
-- Safe to run multiple times (IF NOT EXISTS guards on each column).
-- Columns added:
--   Group 1 (ING-007 / ING-008 enrichment, may already exist on some envs):
--     is_ask_side          BOOLEAN
--     bid_ask_class        TEXT
--     vol_oi_signal        TEXT
--     normalized_premium   NUMERIC(18,2)
--   Group 2 (REARCH-003 new):
--     dte_bucket           TEXT
--     notional_tier        TEXT
--     event_cipher_score   NUMERIC(6,4)

BEGIN;

-- ── Group 1: bid/ask + vol/OI + normalized premium ───────────────────────────

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'flow_events'
      AND column_name  = 'is_ask_side'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN is_ask_side BOOLEAN DEFAULT NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'flow_events'
      AND column_name  = 'bid_ask_class'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN bid_ask_class TEXT DEFAULT NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'flow_events'
      AND column_name  = 'vol_oi_signal'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN vol_oi_signal TEXT DEFAULT NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'flow_events'
      AND column_name  = 'normalized_premium'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN normalized_premium NUMERIC(18,2) DEFAULT NULL;
  END IF;
END $$;

-- ── Group 2: REARCH-003 dimension tags ───────────────────────────────────────

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'flow_events'
      AND column_name  = 'dte_bucket'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN dte_bucket TEXT DEFAULT NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'flow_events'
      AND column_name  = 'notional_tier'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN notional_tier TEXT DEFAULT NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'flow_events'
      AND column_name  = 'event_cipher_score'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN event_cipher_score NUMERIC(6,4) DEFAULT NULL;
  END IF;
END $$;

-- ── Indexes for downstream query patterns ────────────────────────────────────
-- REARCH-004 (Signal Engine) will filter on dte_bucket + notional_tier.
-- REARCH-006 (Apex) will range-scan event_cipher_score.

CREATE INDEX IF NOT EXISTS idx_flow_events_dte_bucket
  ON public.flow_events (dte_bucket)
  WHERE dte_bucket IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_flow_events_notional_tier
  ON public.flow_events (notional_tier)
  WHERE notional_tier IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_flow_events_event_cipher_score
  ON public.flow_events (event_cipher_score DESC)
  WHERE event_cipher_score IS NOT NULL;

COMMIT;
