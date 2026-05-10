-- ============================================================
-- Migration 024: REARCH-010-STAGE6
-- Drop retired columns from flow_events.
--
-- Background:
--   flow_store.py fix #10 (2026-05-09) removed is_golden_sweep,
--   influence_tier, and conviction_score from the persist_flow_event()
--   row dict. Any attempt to write these columns now causes PostgREST
--   error 42703 (column does not exist), so the Python side already
--   treats them as absent. This migration makes the DB schema agree.
--
-- Safety:
--   All three columns are confirmed absent from the flow_store.py
--   writer (verified via information_schema.columns audit in fix #10
--   docstring). No downstream query, RPC, view, or index references
--   these columns per REARCH-010 audit. Column drops are irreversible
--   — verify on a branch DB before running on production.
--
-- Idempotency:
--   Each DROP uses IF EXISTS so re-running is a no-op.
-- ============================================================

ALTER TABLE public.flow_events
  DROP COLUMN IF EXISTS is_golden_sweep,
  DROP COLUMN IF EXISTS influence_tier,
  DROP COLUMN IF EXISTS conviction_score;

-- Verify columns are gone (raises nothing if already absent)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'flow_events'
      AND column_name  IN ('is_golden_sweep', 'influence_tier', 'conviction_score')
  ) THEN
    RAISE EXCEPTION 'REARCH-010 column drop incomplete — retired columns still present in flow_events';
  END IF;
END;
$$;
