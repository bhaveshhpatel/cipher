-- REARCH-010 | Stage 7: Post-migration validation suite
-- Run this entire file after Stages 1-6 are complete.
-- Every query is annotated with its expected result.
-- Paste the full output as a comment on the PR before requesting review.

-- =========================================================================
-- 1. ROW COUNT INTEGRITY
-- Confirm no rows were lost during migration.
-- =========================================================================
SELECT
  (SELECT COUNT(*) FROM flow_events)    AS flow_events_count,
  (SELECT COUNT(*) FROM flow_episodes)  AS flow_episodes_count,
  (SELECT COUNT(*) FROM signal_history) AS signal_history_count;
-- Expected: 130487, 11428, 28504 (± any rows written during migration window)

-- =========================================================================
-- 2. RETIRED TABLES GONE
-- Must return 0 rows.
-- =========================================================================
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('backtest_results', 'gate_configs', 'gate_config_audit');

-- =========================================================================
-- 3. RETIRED COLUMNS GONE
-- All three queries must return 0 rows.
-- =========================================================================

-- flow_events
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'flow_events'
  AND column_name IN ('is_golden_sweep', 'influence_tier', 'conviction_score');

-- flow_episodes
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'flow_episodes'
  AND column_name = 'seed_episode';

-- signal_history
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'signal_history'
  AND column_name IN (
    'swarm_direction', 'swarm_confidence', 'swarm_agents',
    'swarm_bull_votes', 'swarm_bear_votes', 'swarm_hold_votes',
    'volume_premium_factor', 'influence_tier'
  );

-- =========================================================================
-- 4. NEW COLUMNS EXIST
-- =========================================================================

-- flow_episodes: must return 6 rows
SELECT column_name, data_type, column_default, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'flow_episodes'
  AND column_name IN (
    'episode_steamroom_score', 'ask_side_count', 'ask_side_pct',
    'vol_oi_signal', 'notional_tier', 'dte_bucket'
  )
ORDER BY column_name;

-- signal_history: must return 3 rows
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'signal_history'
  AND column_name IN ('episode_steamroom_score', 'ask_side_pct', 'vol_oi_ratio')
ORDER BY column_name;

-- =========================================================================
-- 5. NOT NULL / DEFAULT INTEGRITY
-- All counts must be 0.
-- =========================================================================
SELECT
  COUNT(*) FILTER (WHERE episode_steamroom_score IS NULL) AS steamroom_score_nulls,
  COUNT(*) FILTER (WHERE ask_side_count IS NULL)          AS ask_side_count_nulls,
  COUNT(*) FILTER (WHERE vol_oi_signal IS NULL)           AS vol_oi_signal_nulls
FROM flow_episodes;

-- =========================================================================
-- 6. CONSTRAINT VERIFICATION
-- =========================================================================

-- Constraints must exist with correct definitions.
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'signal_history'::regclass
  AND contype = 'c'
  AND conname IN (
    'signal_history_alert_level_check',
    'signal_history_direction_check'
  );
-- Must return 2 rows.

-- No rows violate either constraint.
SELECT COUNT(*) AS constraint_violations
FROM signal_history
WHERE alert_level NOT IN ('WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN')
   OR direction NOT IN ('BULLISH', 'BEARISH', 'NEUTRAL');
-- Must be 0.

-- =========================================================================
-- 7. ALERT LEVEL + DIRECTION DISTRIBUTION (for PR audit trail)
-- =========================================================================
SELECT alert_level, COUNT(*) AS row_count
FROM signal_history
GROUP BY alert_level
ORDER BY row_count DESC;

SELECT direction, COUNT(*) AS row_count
FROM signal_history
GROUP BY direction
ORDER BY row_count DESC;

-- =========================================================================
-- 8. RLS POLICY AUDIT
-- Manually inspect output for any reference to retired column names.
-- =========================================================================
SELECT tablename, policyname, qual, with_check
FROM pg_policies
WHERE tablename IN ('flow_events', 'flow_episodes', 'signal_history');

-- =========================================================================
-- 9. INGESTION_CONFIG PARITY (confirm gate_configs replacement is complete)
-- Must return 9 rows.
-- =========================================================================
SELECT key, value, description
FROM ingestion_config
WHERE key IN (
  'min_premium_t1', 'min_premium_t2', 'min_premium_t3',
  'min_dte', 'max_dte',
  'merge_window_seconds',
  'min_ask_side_pct',
  'require_vol_gt_oi',
  'min_trade_count'
)
ORDER BY key;

-- =========================================================================
-- 10. STREAMING BOUNDARY CONFIRMATION
-- Confirm none of the frozen streaming tables were touched.
-- These row counts / column counts should be identical to pre-migration baseline.
-- =========================================================================
SELECT
  (SELECT COUNT(*) FROM options_chain_cache)       AS chain_cache_rows,
  (SELECT COUNT(*) FROM options_universe_symbols)  AS universe_symbols_rows,
  (SELECT COUNT(*) FROM options_universe_snapshots) AS universe_snapshots_rows,
  (SELECT COUNT(*) FROM ingestion_config)           AS ingestion_config_rows,
  (SELECT COUNT(*) FROM tier_thresholds)            AS tier_thresholds_rows;
-- Expected baseline: 173498, 12504, 3, 9, 1
-- Any deviation from baseline is a red flag — investigate before merging.
