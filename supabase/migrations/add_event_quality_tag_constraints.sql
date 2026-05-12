-- REARCH-003 | add_event_quality_tag_constraints.sql
-- Step 3 of 3: Add CHECK constraints now that all rows hold canonical values.
-- Run AFTER backfill_event_quality_tags.sql.
--
-- Constraints added:
--   chk_flow_events_bid_ask_class  (ASK | BID | MID | NULL)
--   chk_flow_events_dte_bucket     (0DTE | 1-4 | 5-60 | 61-90 | 90+ | NULL)
--   chk_flow_events_notional_tier  (WATCH | NOTEWORTHY | BLOCK | GOLDEN | NULL)

BEGIN;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_schema = 'public' AND table_name = 'flow_events'
      AND constraint_name = 'chk_flow_events_bid_ask_class'
  ) THEN
    ALTER TABLE public.flow_events
      ADD CONSTRAINT chk_flow_events_bid_ask_class
      CHECK (bid_ask_class IS NULL OR bid_ask_class IN ('ASK', 'BID', 'MID'));
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_schema = 'public' AND table_name = 'flow_events'
      AND constraint_name = 'chk_flow_events_dte_bucket'
  ) THEN
    ALTER TABLE public.flow_events
      ADD CONSTRAINT chk_flow_events_dte_bucket
      CHECK (dte_bucket IS NULL OR dte_bucket IN ('0DTE', '1-4', '5-60', '61-90', '90+'));
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_schema = 'public' AND table_name = 'flow_events'
      AND constraint_name = 'chk_flow_events_notional_tier'
  ) THEN
    ALTER TABLE public.flow_events
      ADD CONSTRAINT chk_flow_events_notional_tier
      CHECK (notional_tier IS NULL OR notional_tier IN ('WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN'));
  END IF;
END $$;

COMMIT;
