-- REARCH-003 | add_event_quality_tag_columns_to_flow_events.sql
-- Step 1 of 2: Add columns + indexes.
-- Step 2 (add_event_quality_tag_constraints.sql) adds CHECK constraints
--   after backfill_event_quality_tags.sql has normalized all existing rows.
--
-- Safe to run multiple times (IF NOT EXISTS guards on every ALTER).
--
-- Columns added:
--   Group 1 (ING-007 / ING-008 enrichment, may already exist on some envs):
--     is_ask_side          BOOLEAN
--     bid_ask_class        TEXT
--     vol_oi_signal        BOOLEAN   <- TRUE = high activity (vol/OI >= 0.5)
--     normalized_premium   NUMERIC(10,4)
--   Group 2 (REARCH-003 new):
--     dte_bucket           TEXT
--     notional_tier        TEXT
--
-- NOTE: event_cipher_score is NOT added here.
--   That column is owned by REARCH-004.

BEGIN;

-- Group 1: bid/ask + vol/OI + normalized premium

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'flow_events' AND column_name = 'is_ask_side'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN is_ask_side BOOLEAN DEFAULT NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'flow_events' AND column_name = 'bid_ask_class'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN bid_ask_class TEXT DEFAULT NULL;
  END IF;
END $$;

-- vol_oi_signal: BOOLEAN (TRUE = high, FALSE = normal, NULL = no data).
-- Matches compute_vol_oi_signal() in flow_store.py which returns Optional[bool].
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'flow_events' AND column_name = 'vol_oi_signal'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN vol_oi_signal BOOLEAN DEFAULT NULL;
  END IF;
END $$;

-- normalized_premium: premium / underlying_price, rounded to 4dp.
-- NUMERIC(10,4) matches _compute_normalized_premium() round(..., 4).
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'flow_events' AND column_name = 'normalized_premium'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN normalized_premium NUMERIC(10,4) DEFAULT NULL;
  END IF;
END $$;

-- Group 2: REARCH-003 dimension tags

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'flow_events' AND column_name = 'dte_bucket'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN dte_bucket TEXT DEFAULT NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'flow_events' AND column_name = 'notional_tier'
  ) THEN
    ALTER TABLE public.flow_events ADD COLUMN notional_tier TEXT DEFAULT NULL;
  END IF;
END $$;

-- Indexes for downstream query patterns
-- REARCH-004 (Signal Engine) will filter on dte_bucket + notional_tier.

CREATE INDEX IF NOT EXISTS idx_flow_events_dte_bucket
  ON public.flow_events (dte_bucket)
  WHERE dte_bucket IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_flow_events_notional_tier
  ON public.flow_events (notional_tier)
  WHERE notional_tier IS NOT NULL;

COMMIT;
