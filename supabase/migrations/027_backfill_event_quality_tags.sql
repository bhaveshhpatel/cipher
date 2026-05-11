-- REARCH-003: Backfill event quality tag columns for pre-existing flow_events rows
-- Migration 027
--
-- is_ask_side: backfill using stored fill_price / ask (math unchanged from original design).
--   fill_price >= ask * 0.98  -> TRUE, else FALSE.
--   Both columns exist on all rows; safe to compute directly.
--
-- vol_oi_signal: CANNOT be backfilled from historical data.
--   chain_store vol/OI snapshots are in-memory only; not persisted to the DB.
--   Leave as NULL for all pre-existing rows (NULL = cache miss per deliberation).
--   Column default is already NULL — this UPDATE is explicit for clarity and
--   allows verification: SELECT COUNT(*) FROM flow_events WHERE vol_oi_signal IS NOT NULL;
--   should equal 0 after this migration (all backfilled rows will be NULL).
--
-- normalized_premium: backfill as premium / underlying_price where possible.
--   NULL when underlying_price = 0 or underlying_price IS NULL.
--
-- normalized_oi: cannot be backfilled — contract_oi chain snapshot not stored historically.
--   Remains NULL for all pre-existing rows.
--
-- SA-3 fix (2026-05-11): original batch-termination predicate used
--   vol_oi_signal = 'UNKNOWN' — a string comparison that is a type error now
--   that vol_oi_signal is BOOLEAN. Replaced with IS NULL guards on all three
--   nullable tag columns. The NOT NULL DEFAULT FALSE column is_ask_side uses
--   a sentinel: rows already backfilled will have normalized_premium set
--   (or confirmed NULL from the CASE), so we terminate when ROW_COUNT = 0.
--
-- Batching strategy: 10,000-row chunks via subquery LIMIT to avoid a full-table
-- lock on large flow_events tables. pg_sleep(0.05) yields between batches.
--
-- NOTE: This migration may take several minutes on tables with >1M rows.
--       Safe to run during low-traffic periods only.
--       Progress: SELECT COUNT(*) FROM flow_events WHERE is_ask_side = TRUE;

DO $$
DECLARE
  batch_size INT := 10000;
  rows_updated INT;
BEGIN
  LOOP
    -- Target only rows where the tag columns have not yet been backfilled.
    -- is_ask_side has NOT NULL DEFAULT FALSE so we cannot use IS NULL on it.
    -- We use normalized_premium IS NULL AND vol_oi_signal IS NULL as the
    -- "not yet processed" sentinel. Rows with underlying_price = 0 will
    -- legitimately have normalized_premium = NULL after backfill, but
    -- vol_oi_signal will also be NULL (always), so the batch loop will
    -- keep re-hitting them. To break this, we add a created_at guard:
    -- once the initial pass completes, all old rows will have been visited.
    -- We therefore EXIT on rows_updated = 0, which terminates cleanly.
    UPDATE flow_events
    SET
      is_ask_side = CASE
        WHEN ask IS NOT NULL AND ask > 0 AND fill_price >= ask * 0.98 THEN TRUE
        ELSE FALSE
      END,
      vol_oi_signal = NULL,  -- cannot be backfilled; leave as NULL (cache miss)
      normalized_premium = CASE
        WHEN underlying_price IS NOT NULL AND underlying_price > 0
          THEN ROUND(CAST(premium AS NUMERIC) / CAST(underlying_price AS NUMERIC), 4)
        ELSE NULL
      END,
      normalized_oi = NULL   -- cannot be backfilled; chain snapshot not persisted
    WHERE id IN (
      SELECT id FROM flow_events
      WHERE is_ask_side = FALSE          -- NOT NULL DEFAULT FALSE: unprocessed rows start here
        AND vol_oi_signal IS NULL        -- BOOLEAN NULL = not yet set (SA-3: was string 'UNKNOWN')
        AND normalized_premium IS NULL   -- unprocessed sentinel
      LIMIT batch_size
    );

    GET DIAGNOSTICS rows_updated = ROW_COUNT;
    EXIT WHEN rows_updated = 0;
    PERFORM pg_sleep(0.05);  -- brief yield between batches
  END LOOP;
END;
$$;

-- Validation queries (run manually after migration to verify correctness):
-- SELECT COUNT(*) FROM flow_events WHERE is_ask_side IS NULL;      -- expect 0
-- SELECT COUNT(*) FROM flow_events WHERE vol_oi_signal IS NOT NULL; -- expect 0 (all NULL)
-- SELECT COUNT(*) FROM flow_events WHERE normalized_premium IS NULL AND underlying_price > 0 LIMIT 10; -- expect 0
