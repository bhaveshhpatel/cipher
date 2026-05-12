-- REARCH-003 | backfill_event_quality_tags.sql
-- Step 2 of 3: Batched backfill of event quality tags for existing rows.
-- Run AFTER add_event_quality_tag_columns_to_flow_events.sql.
-- Run BEFORE add_event_quality_tag_constraints.sql.
--
-- Strategy:
--   Processes flow_events in batches of 5 000 rows ordered by created_at ASC.
--   For each row, recomputes all 6 tag columns using the same pure-SQL
--   expressions that mirror flow_store.py's Python helpers exactly.
--   Safe to re-run: WHERE clause targets rows that still hold old placeholder
--   enum values OR NULL, so re-running after a partial run is harmless.
--
--   OLD (wrong) enums written by the first backfill run:
--     dte_bucket:    '0dte' | 'weekly' | 'monthly' | 'quarterly' | 'leaps'
--     notional_tier: 'small' | 'medium' | 'large' | 'whale'
--
--   CANONICAL Steamroom enums (matching processor.py + flow_store.py):
--     dte_bucket:    '0DTE' | '1-4' | '5-60' | '61-90' | '90+'
--     notional_tier: 'WATCH' | 'NOTEWORTHY' | 'BLOCK' | 'GOLDEN'
--
--   bid_ask_class / is_ask_side: rewritten to canonical 'ASK' | 'BID' | 'MID'.
--   vol_oi_signal: BOOLEAN (TRUE = high, FALSE = normal, NULL = no data).
--   event_cipher_score is NOT touched here -- owned by REARCH-004.
--
-- Threshold constants (must match processor.py and flow_store.py exactly):
--   _BID_ASK_ASK_THRESHOLD  = 0.98    fill >= ask * 0.98  -> 'ASK'
--   _BID_ASK_BID_THRESHOLD  = 1.02    fill <= bid * 1.02  -> 'BID'
--   VOL_OI_HIGH_THRESHOLD   = 0.5     vol/OI >= 0.5       -> TRUE
--   _NOTIONAL_GOLDEN        = 500000  premium >= $500k    -> 'GOLDEN'
--   _NOTIONAL_BLOCK         = 100000  premium >= $100k    -> 'BLOCK'
--   _NOTIONAL_NOTEWORTHY    =  50000  premium >= $50k     -> 'NOTEWORTHY'
--                                     premium <  $50k     -> 'WATCH'
--   DTE buckets:
--     dte = 0           -> '0DTE'
--     1  <= dte <= 4    -> '1-4'
--     5  <= dte <= 60   -> '5-60'
--     61 <= dte <= 90   -> '61-90'
--     dte > 90 or NULL  -> '90+'

DO $$
DECLARE
  BATCH_SIZE   CONSTANT INT := 5000;
  last_cursor  TIMESTAMPTZ  := '1970-01-01'::TIMESTAMPTZ;
  rows_done    BIGINT       := 0;
  batch_rows   INT;
BEGIN
  RAISE NOTICE 'REARCH-003 backfill (canonical enums) started at %', clock_timestamp();

  LOOP
    WITH batch AS (
      SELECT
        id,
        created_at,

        -- is_ask_side
        CASE
          WHEN bid IS NOT NULL AND ask IS NOT NULL
               AND bid > 0 AND ask > 0
               AND bid <= ask
          THEN fill_price >= ask * 0.98
          ELSE FALSE
        END AS calc_is_ask_side,

        -- bid_ask_class
        CASE
          WHEN bid IS NOT NULL AND ask IS NOT NULL
               AND bid > 0 AND ask > 0
               AND bid <= ask
          THEN
            CASE
              WHEN fill_price >= ask * 0.98   THEN 'ASK'
              WHEN fill_price <= bid * 1.02   THEN 'BID'
              ELSE                                 'MID'
            END
          ELSE 'MID'
        END AS calc_bid_ask_class,

        -- vol_oi_signal
        CASE
          WHEN contract_volume_snapshot IS NULL
            OR contract_oi IS NULL
            OR contract_oi = 0
          THEN NULL
          ELSE (contract_volume_snapshot::NUMERIC / contract_oi) >= 0.5
        END AS calc_vol_oi_signal,

        -- normalized_premium
        CASE
          WHEN underlying_price IS NOT NULL AND underlying_price > 0
          THEN ROUND(premium / underlying_price, 4)
          ELSE NULL
        END AS calc_normalized_premium,

        -- dte_bucket
        CASE
          WHEN dte IS NULL OR dte < 0 THEN '90+'
          WHEN dte = 0                THEN '0DTE'
          WHEN dte <= 4               THEN '1-4'
          WHEN dte <= 60              THEN '5-60'
          WHEN dte <= 90              THEN '61-90'
          ELSE                             '90+'
        END AS calc_dte_bucket,

        -- notional_tier
        CASE
          WHEN premium IS NULL OR premium = 0 THEN 'WATCH'
          WHEN premium >= 500000              THEN 'GOLDEN'
          WHEN premium >= 100000              THEN 'BLOCK'
          WHEN premium >= 50000               THEN 'NOTEWORTHY'
          ELSE                                     'WATCH'
        END AS calc_notional_tier

      FROM public.flow_events
      WHERE created_at > last_cursor
        AND (
          dte_bucket IS NULL
          OR dte_bucket IN ('0dte', 'weekly', 'monthly', 'quarterly', 'leaps')
          OR notional_tier IS NULL
          OR notional_tier IN ('small', 'medium', 'large', 'whale')
        )
      ORDER BY created_at ASC
      LIMIT BATCH_SIZE
    ),
    updated AS (
      UPDATE public.flow_events fe
      SET
        is_ask_side        = b.calc_is_ask_side,
        bid_ask_class      = b.calc_bid_ask_class,
        vol_oi_signal      = b.calc_vol_oi_signal,
        normalized_premium = b.calc_normalized_premium,
        dte_bucket         = b.calc_dte_bucket,
        notional_tier      = b.calc_notional_tier
      FROM batch b
      WHERE fe.id = b.id
      RETURNING fe.created_at
    )
    SELECT COUNT(*), MAX(created_at) INTO batch_rows, last_cursor FROM updated;

    rows_done := rows_done + batch_rows;
    RAISE NOTICE '  ... backfilled % rows (total so far: %)', batch_rows, rows_done;

    EXIT WHEN batch_rows < BATCH_SIZE;
  END LOOP;

  RAISE NOTICE 'REARCH-003 backfill complete: % total rows updated at %', rows_done, clock_timestamp();
END $$;
