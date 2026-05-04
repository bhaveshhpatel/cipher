-- Migration: 20260504_fix_alert_level_constraint.sql
--
-- Problem:
--   signal_feed_log_alert_level_check was created with the old signal-quality
--   vocabulary (CONVICTION | STRONG_SIGNAL | ALERT | WATCH), which never matched
--   what RepetitionAccumulator.get_alert_level() actually emits. The accumulator
--   has always used the size-tier vocabulary (CONVICTION | WHALE | INSTITUTIONAL
--   | LARGE | RETAIL). Every DB insert from the accumulator path was either
--   silently falling back through _normalise_alert_level() or producing a 23514
--   CHECK violation.
--
-- Fix:
--   Drop the stale constraint and recreate it with the correct vocabulary.
--   signal_store._VALID_ALERT_LEVELS and _build_row() score branches are
--   updated in the same commit (Fix 7 in signal_store.py).
--
-- Safe to run on live DB:
--   The constraint drop is instantaneous (no table rewrite).
--   The new constraint addition scans existing rows once — any rows that
--   already stored STRONG_SIGNAL/ALERT/WATCH (from the old fallback path)
--   will fail the new constraint. Run the backfill UPDATE below first if
--   the table has existing rows with old vocab values.
--
-- Backfill (run before adding constraint if table has existing data):
--   UPDATE signal_feed_log
--   SET alert_level = CASE alert_level
--       WHEN 'STRONG_SIGNAL' THEN 'WHALE'
--       WHEN 'ALERT'         THEN 'INSTITUTIONAL'
--       WHEN 'WATCH'         THEN 'LARGE'
--       WHEN 'NORMAL'        THEN 'LARGE'
--       ELSE alert_level
--   END
--   WHERE alert_level NOT IN ('CONVICTION','WHALE','INSTITUTIONAL','LARGE','RETAIL');

-- Step 1: Drop the stale constraint
ALTER TABLE signal_feed_log
    DROP CONSTRAINT IF EXISTS signal_feed_log_alert_level_check;

-- Step 2: Recreate with correct vocabulary
ALTER TABLE signal_feed_log
    ADD CONSTRAINT signal_feed_log_alert_level_check
    CHECK (alert_level IN ('CONVICTION','WHALE','INSTITUTIONAL','LARGE','RETAIL'));
