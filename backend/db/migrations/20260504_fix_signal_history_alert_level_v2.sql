-- Migration: fix_signal_history_alert_level_v2
-- Applied: 2026-05-04 to cipher-database (kpajucxqlrteckfuafvq)
--
-- PROBLEM:
--   signal_store.py inserts into signal_history (_TABLE = "signal_history").
--   Prior migration 20260504_fix_alert_level_constraint.sql targeted signal_feed_log
--   but the constraint was actually applied to signal_history under the misnamed
--   identifier signal_feed_log_alert_level_check, with the OLD vocabulary:
--     CONVICTION | STRONG_SIGNAL | ALERT | WATCH
--   This is misaligned with signal_store._VALID_ALERT_LEVELS (Fix 7) which uses:
--     CONVICTION | WHALE | INSTITUTIONAL | LARGE | RETAIL
--   All 33,000+ existing rows stored stale values — every LARGE signal was WATCH,
--   every INSTITUTIONAL was ALERT, every WHALE was STRONG_SIGNAL.
--
-- DISCOVERY:
--   Backfill was initially blocked because the old constraint was still active,
--   rejecting the UPDATE. Correct fix order: drop constraint first, then backfill,
--   then recreate with correct name and vocabulary.
--
-- VERIFICATION (run after applying):
--   SELECT alert_level, COUNT(*) FROM signal_history
--   GROUP BY alert_level ORDER BY COUNT(*) DESC;
--   -- Should show only: CONVICTION | WHALE | INSTITUTIONAL | LARGE | RETAIL
--
--   SELECT conname, pg_get_constraintdef(oid)
--   FROM pg_constraint
--   WHERE conrelid = 'signal_history'::regclass
--     AND contype = 'c'
--     AND conname ILIKE '%alert_level%';
--   -- Should show: signal_history_alert_level_check with correct 5-value set

-- Step 1: Drop the stale misnamed constraint (old vocab, wrong name)
ALTER TABLE signal_history
    DROP CONSTRAINT IF EXISTS signal_feed_log_alert_level_check;

-- Step 2: Also drop any pre-existing correctly-named constraint (idempotent)
ALTER TABLE signal_history
    DROP CONSTRAINT IF EXISTS signal_history_alert_level_check;

-- Step 3: Backfill all rows with stale vocabulary to new vocabulary
--   WATCH         -> LARGE         (default non-notable flow)
--   ALERT         -> INSTITUTIONAL (mid-tier institutional)
--   STRONG_SIGNAL -> WHALE         (large institutional)
--   NORMAL        -> LARGE         (legacy default)
UPDATE signal_history
SET alert_level = CASE alert_level
    WHEN 'STRONG_SIGNAL' THEN 'WHALE'
    WHEN 'ALERT'         THEN 'INSTITUTIONAL'
    WHEN 'WATCH'         THEN 'LARGE'
    WHEN 'NORMAL'        THEN 'LARGE'
    ELSE alert_level
END
WHERE alert_level NOT IN ('CONVICTION','WHALE','INSTITUTIONAL','LARGE','RETAIL');

-- Step 4: Recreate constraint with correct name and vocabulary
ALTER TABLE signal_history
    ADD CONSTRAINT signal_history_alert_level_check
    CHECK (alert_level IN ('CONVICTION','WHALE','INSTITUTIONAL','LARGE','RETAIL'));
