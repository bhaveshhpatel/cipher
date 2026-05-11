-- REARCH-003 | Chunk 3b: Batched backfill of event quality tags for existing rows
--
-- Strategy:
--   • Processes flow_events in batches of 5 000 rows ordered by created_at ASC.
--   • For each row, recomputes all 7 tag columns using the same pure-SQL
--     expressions that IngestionProcessor.enrich_tags() uses in Python.
--     Keeping both in sync is a deliberate trade-off: if the Python logic
--     changes, this migration is already applied and historical rows keep
--     their original backfill values — which is correct behaviour.
--   • Safe to re-run: WHERE clause targets only rows missing the 3 new
--     REARCH-003 columns (dte_bucket, notional_tier, event_cipher_score).
--     bid_ask_class / is_ask_side were set by prior migrations and are NOT
--     NULL constrained; we still recompute them here but with safe fallbacks.
--   • Expected throughput: ~5 000 rows/s on Supabase shared tier.
--     Adjust BATCH_SIZE if the instance times out.
--
-- Schema corrections vs original draft:
--   price          -> fill_price
--   volume         -> contract_volume_snapshot
--   open_interest  -> contract_oi
--   id (BIGINT)    -> created_at cursor (id col is UUID)
--   bid_ask_class  -> COALESCE(..., 'unknown')  [NOT NULL constraint]
--   is_ask_side    -> COALESCE(..., FALSE)       [NOT NULL constraint]

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

        -- is_ask_side: trade fill closer to ask than bid.
        -- Falls back to FALSE when spread is indeterminate (NOT NULL constraint).
        COALESCE(
          CASE
            WHEN bid IS NOT NULL AND ask IS NOT NULL AND bid < ask
            THEN (fill_price - bid) >= (ask - fill_price)
            ELSE NULL
          END,
          FALSE
        ) AS calc_is_ask_side,

        -- bid_ask_class: where fill landed relative to quoted spread.
        -- Falls back to 'unknown' when spread is indeterminate (NOT NULL constraint).
        COALESCE(
          CASE
            WHEN bid IS NOT NULL AND ask IS NOT NULL AND bid < ask THEN
              CASE
                WHEN fill_price >= ask                            THEN 'above_ask'
                WHEN fill_price <= bid                           THEN 'below_bid'
                WHEN (fill_price - bid) >= (ask - fill_price)    THEN 'ask_side'
                ELSE                                                  'bid_side'
              END
            ELSE NULL
          END,
          'unknown'
        ) AS calc_bid_ask_class,

        -- vol_oi_signal: contract_volume_snapshot vs contract_oi ratio buckets.
        CASE
          WHEN contract_oi IS NULL OR contract_oi = 0                         THEN NULL
          WHEN contract_volume_snapshot::NUMERIC / contract_oi >= 3.0         THEN 'extreme'
          WHEN contract_volume_snapshot::NUMERIC / contract_oi >= 1.0         THEN 'high'
          WHEN contract_volume_snapshot::NUMERIC / contract_oi >= 0.25        THEN 'moderate'
          ELSE                                                                      'low'
        END AS calc_vol_oi_signal,

        -- normalized_premium: premium as % of underlying price.
        CASE
          WHEN underlying_price IS NOT NULL AND underlying_price > 0
          THEN ROUND((premium / underlying_price) * 100, 2)
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
        END AS calc_notional_tier,

        -- event_cipher_score: weighted composite (0-1 scale).
        -- Weights match IngestionProcessor._score_event():
        --   bid_ask_class 0.35 | vol_oi_signal 0.35 | notional_tier 0.30
        ROUND(
          COALESCE(
            CASE
              WHEN bid IS NOT NULL AND ask IS NOT NULL AND bid < ask THEN
                CASE
                  WHEN fill_price >= ask                           THEN 1.00
                  WHEN (fill_price - bid) >= (ask - fill_price)    THEN 0.65
                  WHEN fill_price <= bid                           THEN 0.10
                  ELSE                                                  0.35
                END
              ELSE 0.50
            END, 0.50
          ) * 0.35
          +
          COALESCE(
            CASE
              WHEN contract_oi IS NULL OR contract_oi = 0                  THEN 0.50
              WHEN contract_volume_snapshot::NUMERIC / contract_oi >= 3.0  THEN 1.00
              WHEN contract_volume_snapshot::NUMERIC / contract_oi >= 1.0  THEN 0.75
              WHEN contract_volume_snapshot::NUMERIC / contract_oi >= 0.25 THEN 0.45
              ELSE                                                               0.20
            END, 0.50
          ) * 0.35
          +
          COALESCE(
            CASE
              WHEN premium IS NULL    THEN 0.50
              WHEN premium >= 1000000 THEN 1.00
              WHEN premium >= 250000  THEN 0.75
              WHEN premium >= 50000   THEN 0.50
              ELSE                        0.25
            END, 0.50
          ) * 0.30
        , 4) AS calc_event_cipher_score

      FROM public.flow_events
      WHERE created_at > last_cursor
        AND (
          dte_bucket          IS NULL
          OR notional_tier    IS NULL
          OR event_cipher_score IS NULL
        )
      ORDER BY created_at ASC
      LIMIT BATCH_SIZE
    ),
    updated AS (
      UPDATE public.flow_events fe
      SET
        is_ask_side         = b.calc_is_ask_side,
        bid_ask_class       = b.calc_bid_ask_class,
        vol_oi_signal       = b.calc_vol_oi_signal,
        normalized_premium  = b.calc_normalized_premium,
        dte_bucket          = b.calc_dte_bucket,
        notional_tier       = b.calc_notional_tier,
        event_cipher_score  = b.calc_event_cipher_score
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
