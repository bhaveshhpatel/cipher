-- REARCH-010 | Stage 1: Backfill signal_history.alert_level
-- Maps pre-REARCH vocabulary to REARCH vocabulary.
-- Run in 1,000-row batches. Old constraint is still in place during this stage —
-- no row can violate it until Stage 5. Safe to re-run (idempotent WHERE clause).
--
-- IMPORTANT: Confirm batch bounds before running.
-- Get min/max id: SELECT MIN(id), MAX(id) FROM signal_history;
-- Then loop: $batch_start = MIN(id), $batch_end = $batch_start + 999, increment by 1000.
--
-- Mapping:
--   CONVICTION  → BLOCK       (highest old tier; GOLDEN not assigned retroactively)
--   WHALE       → BLOCK       (notional $500K+ equivalent)
--   INSTITUTIONAL → NOTEWORTHY ($100K–$500K range)
--   LARGE       → NOTEWORTHY  ($50K–$100K range)
--   RETAIL      → WATCH       (minimum qualifier)
--
-- NOTE: No existing row is mapped to GOLDEN. GOLDEN requires all 5 live Steamroom
-- dimensions confirmed at signal emission time — this cannot be retroactively computed.
-- This is an accepted limitation documented in the REARCH-010 deliberation.

BEGIN;

UPDATE signal_history
SET alert_level = CASE
  WHEN alert_level IN ('CONVICTION', 'WHALE')        THEN 'BLOCK'
  WHEN alert_level IN ('INSTITUTIONAL', 'LARGE')     THEN 'NOTEWORTHY'
  WHEN alert_level = 'RETAIL'                        THEN 'WATCH'
  ELSE alert_level  -- defensive: already valid REARCH value, leave untouched
END
WHERE alert_level NOT IN ('WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN');
-- Remove the batch range filter when running interactively in Supabase SQL Editor
-- (full table is only 28,504 rows — single-pass is safe at this size).
-- If running programmatically on a larger dataset, add:
--   AND id BETWEEN :batch_start AND :batch_end

COMMIT;

-- POST-STAGE VERIFICATION (run immediately after COMMIT):
-- Must return 0 rows. Any result means an unrecognized value exists — do not proceed to Stage 5.
SELECT DISTINCT alert_level
FROM signal_history
WHERE alert_level NOT IN ('WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN');

-- Distribution audit — paste output into PR comment for record.
SELECT alert_level, COUNT(*) AS row_count
FROM signal_history
GROUP BY alert_level
ORDER BY row_count DESC;
