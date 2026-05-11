-- REARCH-003: Backfill event quality tag columns for pre-existing flow_events rows
-- Migration 027
--
-- is_ask_side: backfill using stored fill_price / ask.
--   fill_price >= ask * 0.98  -> TRUE, else FALSE.
--   Both columns exist on all rows; safe to compute directly.
--
-- vol_oi_signal: CANNOT be backfilled from historical data.
--   chain_store vol/OI snapshots are in-memory only; not persisted.
--   Set to 'UNKNOWN' for all pre-existing rows (already the column default,
--   but explicit UPDATE makes the intent clear and allows verification).
--
-- normalized_premium: backfill as premium / underlying_price where possible.
--   NULL when underlying_price = 0 or underlying_price IS NULL.
--
-- Batching strategy: 10,000-row chunks via ctid range to avoid a full-table
-- lock on large flow_events tables. Run in a DO block so Supabase migrations
-- execute it as a single transaction per batch.
--
-- NOTE: This migration may take several minutes on tables with >1M rows.
--       It is safe to run during low-traffic periods only.
--       Progress can be monitored via: SELECT COUNT(*) FROM flow_events WHERE vol_oi_signal != 'UNKNOWN';

DO $$
DECLARE
  batch_size INT := 10000;
  rows_updated INT;
BEGIN
  LOOP
    UPDATE flow_events
    SET
      is_ask_side = CASE
        WHEN ask IS NOT NULL AND ask > 0 AND fill_price >= ask * 0.98 THEN TRUE
        ELSE FALSE
      END,
      vol_oi_signal = 'UNKNOWN',
      normalized_premium = CASE
        WHEN underlying_price IS NOT NULL AND underlying_price > 0
          THEN ROUND(premium / underlying_price, 4)
        ELSE NULL
      END
    WHERE id IN (
      SELECT id FROM flow_events
      WHERE vol_oi_signal = 'UNKNOWN'
        AND is_ask_side = FALSE
        AND normalized_premium IS NULL
      LIMIT batch_size
    );

    GET DIAGNOSTICS rows_updated = ROW_COUNT;
    EXIT WHEN rows_updated = 0;
    PERFORM pg_sleep(0.05);  -- brief yield between batches
  END LOOP;
END;
$$;
