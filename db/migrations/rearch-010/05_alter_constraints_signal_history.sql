-- REARCH-010 | Stage 5: Update CHECK constraints on signal_history
--
-- HARD PREREQUISITE: Stage 1 and Stage 2 backfills must be complete.
-- Confirm BEFORE running this file:
--
--   SELECT DISTINCT alert_level FROM signal_history
--   WHERE alert_level NOT IN ('WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN');
--   -- Must return 0 rows
--
--   SELECT DISTINCT direction FROM signal_history
--   WHERE direction NOT IN ('BULLISH', 'BEARISH', 'NEUTRAL');
--   -- Must return 0 rows
--
-- If either returns rows, STOP. Run the backfill files again before proceeding.
--
-- Note: DROP CONSTRAINT by name requires knowing the actual constraint name.
-- The names below match standard Supabase/Postgres auto-generated names.
-- Verify with:
--   SELECT conname FROM pg_constraint
--   WHERE conrelid = 'signal_history'::regclass AND contype = 'c';
-- Update the names below if they differ.

BEGIN;

-- alert_level constraint
ALTER TABLE signal_history
  DROP CONSTRAINT IF EXISTS signal_history_alert_level_check;

ALTER TABLE signal_history
  ADD CONSTRAINT signal_history_alert_level_check
    CHECK (alert_level = ANY (ARRAY['WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN']));

-- direction constraint
ALTER TABLE signal_history
  DROP CONSTRAINT IF EXISTS signal_history_direction_check;

ALTER TABLE signal_history
  ADD CONSTRAINT signal_history_direction_check
    CHECK (direction = ANY (ARRAY['BULLISH', 'BEARISH', 'NEUTRAL']));

COMMIT;

-- POST-STAGE VERIFICATION:
-- Confirm both constraints exist with correct names.
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'signal_history'::regclass
  AND contype = 'c'
  AND conname IN ('signal_history_alert_level_check', 'signal_history_direction_check');
-- Must return 2 rows with the correct ARRAY definitions.

-- Confirm no rows violate either constraint (belt-and-suspenders).
SELECT COUNT(*) AS violations
FROM signal_history
WHERE alert_level NOT IN ('WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN')
   OR direction NOT IN ('BULLISH', 'BEARISH', 'NEUTRAL');
-- Must return 0.
