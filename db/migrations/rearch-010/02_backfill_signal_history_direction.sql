-- REARCH-010 | Stage 2: Backfill signal_history.direction
-- Maps BUY/SELL/HOLD → BULLISH/BEARISH/NEUTRAL.
-- Direct semantic equivalents. Safe to re-run (idempotent WHERE clause).
-- Old constraint still in place during this stage.

BEGIN;

UPDATE signal_history
SET direction = CASE
  WHEN direction = 'BUY'  THEN 'BULLISH'
  WHEN direction = 'SELL' THEN 'BEARISH'
  WHEN direction = 'HOLD' THEN 'NEUTRAL'
  ELSE direction  -- defensive: already valid REARCH value, leave untouched
END
WHERE direction NOT IN ('BULLISH', 'BEARISH', 'NEUTRAL');

COMMIT;

-- POST-STAGE VERIFICATION (run immediately after COMMIT):
-- Must return 0 rows. Any result means an unrecognized direction value exists.
SELECT DISTINCT direction
FROM signal_history
WHERE direction NOT IN ('BULLISH', 'BEARISH', 'NEUTRAL');

-- Distribution audit — paste output into PR comment for record.
SELECT direction, COUNT(*) AS row_count
FROM signal_history
GROUP BY direction
ORDER BY row_count DESC;
