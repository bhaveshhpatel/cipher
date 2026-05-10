-- REARCH-010 | Stage 3: Drop retired tables
-- Drops backtest_results, gate_config_audit, gate_configs.
--
-- PREREQUISITE — Run this parity check BEFORE executing the DROPs.
-- Must return exactly 9 rows. If fewer, seed the missing ingestion_config rows first.
--
--   SELECT key FROM ingestion_config
--   WHERE key IN (
--     'min_premium_t1', 'min_premium_t2', 'min_premium_t3',
--     'min_dte', 'max_dte',
--     'merge_window_seconds',
--     'min_ask_side_pct',
--     'require_vol_gt_oi',
--     'min_trade_count'
--   );
--
-- backtest_results: 0 rows — empty pre-REARCH artifact, never shipped.
-- gate_config_audit: 6 rows — audit trail for gate_configs (now superseded by admin_activity_log).
-- gate_configs: 21 rows — superseded by ingestion_config (confirmed above).
--
-- ORDER: Drop audit table before parent table (no FK, but logical order).

BEGIN;

DROP TABLE IF EXISTS backtest_results;
DROP TABLE IF EXISTS gate_config_audit;
DROP TABLE IF EXISTS gate_configs;

COMMIT;

-- POST-STAGE VERIFICATION:
-- Must return 0 rows — all 3 tables gone.
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('backtest_results', 'gate_configs', 'gate_config_audit');
