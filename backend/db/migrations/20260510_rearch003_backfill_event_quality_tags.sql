-- REARCH-003 | Chunk 3b: Batched backfill of event quality tags for existing rows
--
-- Strategy:
--   • Processes flow_events in batches of 5 000 rows ordered by created_at ASC.
--   • For each row, recomputes all 6 tag columns using the same pure-SQL
--     expressions that mirror flow_store.py's Python helpers exactly.
--   • Safe to re-run: WHERE clause targets only rows missing dte_bucket OR
--     notional_tier (the 2 new REARCH-003 TEXT columns). Rows that already
--     have both set are skipped.
--   • bid_ask_class / is_ask_side were written by prior inserts with the old
--     lowercase enum ('ask_side', 'above_ask', etc.). This backfill rewrites
--     them to the canonical enum: 'ASK' | 'BID' | 'MID'.
--   • vol_oi_signal was TEXT in the old column definition. The column type is
--     corrected to BOOLEAN by the companion DDL fix migration that must be run
--     BEFORE this backfill.
--   • event_cipher_score is NOT touched here — it is owned by REARCH-004.
--
-- Threshold constants (must match flow_store.py exactly):
--   _BID_ASK_ASK_THRESHOLD = 0.98   fill >= ask * 0.98  → 'ASK'
--   _BID_ASK_BID_THRESHOLD = 1.02   fill <= bid * 1.02  → 'BID'
--   VOL_OI_HIGH_THRESHOLD  = 0.5    vol/OI >= 0.5       → TRUE
--
-- Schema column names used:
--   fill_price, bid, ask, contract_volume_snapshot, contract_oi,
--   underlying_price, premium, dte

DO $$
DECLARE
  BATCH_SIZE   CONSTANT INT := 5000;
  last_cursor  TIMESTAMPTZ  := '1970-01-01'::TIMESTAMPTZ;
  rows_done    BIGINT       := 0;
  batch_rows   INT;
BEGIN
  RAISE NOTICE 'REARCH-003 backfill started at %', clock_timestamp();

  LOOP
    WITH batch AS (
      SELECT
        id,
        created_at,

        -- is_ask_side: TRUE when fill is at or above ask * 0.98 threshold.
        -- Mirrors classify_bid_ask() → (cls == "ASK") in flow_store.py.
        -- NULL bid/ask or ask<=0 or bid<=0 → FALSE (MID path).
        CASE
          WHEN bid IS NOT NULL AND ask IS NOT NULL
               AND bid > 0 AND ask > 0
               AND bid <= ask
          THEN fill_price >= ask * 0.98
          ELSE FALSE
        END AS calc_is_ask_side,

        -- bid_ask_class: canonical enum ASK | BID | MID.
        -- Matches classify_bid_ask() thresholds exactly.
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

        -- vol_oi_signal: BOOLEAN — TRUE when vol/OI >= 0.5, else FALSE, NULL on missing data.
        -- Mirrors compute_vol_oi_signal() → Optional[bool] in flow_store.py.
        CASE
          WHEN contract_volume_snapshot IS NULL
            OR contract_oi IS NULL
            OR contract_oi = 0
          THEN NULL
          ELSE (contract_volume_snapshot::NUMERIC / contract_oi) >= 0.5
        END AS calc_vol_oi_signal,

        -- normalized_premium: premium / underlying_price, 4dp.
        -- Mirrors _compute_normalized_premium() in flow_store.py.
        -- NO * 100 — it is a pure ratio, not a percentage.
        CASE
          WHEN underlying_price IS NOT NULL AND underlying_price > 0
          THEN ROUND(premium / underlying_price, 4)
          ELSE NULL
        END AS calc_normalized_premium,

        -- dte_bucket: time-to-expiry classification.
        CASE
          WHEN dte IS NULL  THEN NULL
          WHEN dte <= 1     THEN '0dte'
          WHEN dte <= 7     THEN 'weekly'
          WHEN dte <= 30    THEN 'monthly'
          WHEN dte <= 90    THEN 'quarterly'
          ELSE                   'leaps'
        END AS calc_dte_bucket,

        -- notional_tier: premium size classification.
        CASE
          WHEN premium IS NULL    THEN NULL
          WHEN premium >= 1000000 THEN 'whale'
          WHEN premium >= 250000  THEN 'large'
          WHEN premium >= 50000   THEN 'medium'
          ELSE                        'small'
        END AS calc_notional_tier

      FROM public.flow_events
      WHERE created_at > last_cursor
        AND (
          dte_bucket       IS NULL
          OR notional_tier IS NULL
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
