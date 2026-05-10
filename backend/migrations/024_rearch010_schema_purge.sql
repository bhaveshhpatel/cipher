-- =============================================================================
-- 024_rearch010_schema_purge.sql
-- REARCH-010 — DB Schema Purge: Retire Pre-REARCH Tables, Columns & Constraints;
--              Add Steamroom Fields
--
-- Run order MATTERS — do not reorder sections.
-- This migration is idempotent via IF EXISTS guards throughout.
-- Entire migration runs inside a single transaction; it rolls back cleanly
-- if any statement fails.
--
-- ⚠️  READ BEFORE APPLYING TO PROD:
--   1. Confirm signal_history backfill mappings with PBE (see issue #111 deliberations)
--   2. Confirm API endpoints no longer SELECT retired columns (REARCH-010 AC #3)
--   3. Confirm backend Python sweep is clean (no swarm_*, conviction_score, flow_score, etc.)
--   4. Run EXPLAIN on the backfill UPDATE — 28,504 rows, fast but worth checking.
--   5. Apply during low-traffic window; flow_events DROP COLUMN locks table briefly.
-- =============================================================================

BEGIN;

-- =============================================================================
-- SECTION 1: Backfill signal_history.alert_level
-- Old vocab: CONVICTION, WHALE, INSTITUTIONAL, LARGE, RETAIL
-- New vocab: WATCH, NOTEWORTHY, BLOCK, GOLDEN
--
-- Mapping rationale (confirm with PBE before applying to prod):
--   CONVICTION → BLOCK   (high-conviction pre-REARCH signals → large notional)
--   WHALE      → BLOCK   (whale-tier prints → BLOCK / GOLDEN threshold)
--   INSTITUTIONAL → NOTEWORTHY
--   LARGE      → NOTEWORTHY
--   RETAIL     → WATCH
-- Any rows with NULL or unrecognised values are set to WATCH (safe default).
--
-- ⚠️  GOLDEN cannot be retroactively assigned: doing so would require
--     re-computing all 5 Steamroom dimensions (ask_side_pct, vol>OI,
--     premium tier, DTE bucket, trade_count gate) against historical
--     flow_events. BLOCK is the safe-maximum for pre-REARCH rows.
--     If historical GOLDEN attribution is ever needed, file a dedicated
--     REARCH-006 backfill task — it does not belong in this migration.
-- =============================================================================

UPDATE signal_history
SET alert_level = CASE alert_level
    WHEN 'CONVICTION'     THEN 'BLOCK'
    WHEN 'WHALE'          THEN 'BLOCK'
    WHEN 'INSTITUTIONAL'  THEN 'NOTEWORTHY'
    WHEN 'LARGE'          THEN 'NOTEWORTHY'
    WHEN 'RETAIL'         THEN 'WATCH'
    -- Already-valid REARCH values — leave untouched
    WHEN 'WATCH'          THEN 'WATCH'
    WHEN 'NOTEWORTHY'     THEN 'NOTEWORTHY'
    WHEN 'BLOCK'          THEN 'BLOCK'
    WHEN 'GOLDEN'         THEN 'GOLDEN'
    ELSE 'WATCH'  -- NULL or unknown → safe default
END
WHERE alert_level IS NULL
   OR alert_level NOT IN ('WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN');

-- =============================================================================
-- SECTION 2: Backfill signal_history.direction
-- Old vocab: BUY, SELL, HOLD
-- New vocab: BULLISH, BEARISH, NEUTRAL
-- =============================================================================

UPDATE signal_history
SET direction = CASE direction
    WHEN 'BUY'      THEN 'BULLISH'
    WHEN 'SELL'     THEN 'BEARISH'
    WHEN 'HOLD'     THEN 'NEUTRAL'
    -- Already-valid REARCH values — leave untouched
    WHEN 'BULLISH'  THEN 'BULLISH'
    WHEN 'BEARISH'  THEN 'BEARISH'
    WHEN 'NEUTRAL'  THEN 'NEUTRAL'
    ELSE 'NEUTRAL'  -- NULL or unknown → safe default
END
WHERE direction IS NULL
   OR direction NOT IN ('BULLISH', 'BEARISH', 'NEUTRAL');

-- =============================================================================
-- SECTION 3: DROP deprecated tables
-- Order: audit table first (no deps), then gate_configs (FK from gate_config_audit
-- already gone), then backtest_results (standalone).
-- =============================================================================

DROP TABLE IF EXISTS gate_config_audit;
DROP TABLE IF EXISTS gate_configs;
DROP TABLE IF EXISTS backtest_results;

-- =============================================================================
-- SECTION 4: DROP deprecated columns — flow_events
-- 130,487 rows; each DROP COLUMN acquires an ACCESS EXCLUSIVE lock briefly.
-- Grouped into one ALTER to minimise lock round-trips.
-- =============================================================================

ALTER TABLE flow_events
    DROP COLUMN IF EXISTS is_golden_sweep,
    DROP COLUMN IF EXISTS influence_tier,
    DROP COLUMN IF EXISTS conviction_score;

-- =============================================================================
-- SECTION 5: DROP deprecated columns — flow_episodes
-- =============================================================================

ALTER TABLE flow_episodes
    DROP COLUMN IF EXISTS seed_episode;

-- =============================================================================
-- SECTION 6: DROP deprecated columns — signal_history
-- 6 swarm columns + volume_premium_factor + influence_tier + flow_score = 9 drops.
--
-- flow_score added here (D1 fix): composite_score is the sole score surface
-- per REARCH-010. flow_score was still being written by signal_store._build_row()
-- but read by no SELECT in any router or service — making it a write-only orphan.
-- Retire it here alongside the other pre-REARCH columns.
-- companion Python change: remove flow_score key from signal_store._build_row()
-- return dict (tracked in this same PR commit).
-- =============================================================================

ALTER TABLE signal_history
    DROP COLUMN IF EXISTS swarm_direction,
    DROP COLUMN IF EXISTS swarm_confidence,
    DROP COLUMN IF EXISTS swarm_agents,
    DROP COLUMN IF EXISTS swarm_bull_votes,
    DROP COLUMN IF EXISTS swarm_bear_votes,
    DROP COLUMN IF EXISTS swarm_hold_votes,
    DROP COLUMN IF EXISTS volume_premium_factor,
    DROP COLUMN IF EXISTS influence_tier,
    DROP COLUMN IF EXISTS flow_score;

-- =============================================================================
-- SECTION 7: UPDATE CHECK constraints on signal_history
-- Drop old constraint, re-add with REARCH vocab.
-- Backfill in sections 1 & 2 guarantees all existing rows satisfy new constraint.
-- =============================================================================

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

-- =============================================================================
-- SECTION 8: ADD Steamroom columns to flow_episodes (6 columns)
-- Required by REARCH-003 (event tagging) and REARCH-004 (episode enrichment).
-- All additions are IF NOT EXISTS safe via DO block.
-- =============================================================================

DO $$
BEGIN
    -- episode_steamroom_score
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'flow_episodes' AND column_name = 'episode_steamroom_score'
    ) THEN
        ALTER TABLE flow_episodes
            ADD COLUMN episode_steamroom_score INTEGER NOT NULL DEFAULT 0
            CHECK (episode_steamroom_score BETWEEN 0 AND 5);
        COMMENT ON COLUMN flow_episodes.episode_steamroom_score IS
            'WSJ Steamroom 5-dimension conviction score (0-5). 1pt each: ask-side dominant, vol>OI, premium tier >= NOTEWORTHY, DTE in signal window, trade_count >= min_trade_count. Computed at episode merge time.';
    END IF;

    -- ask_side_count
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'flow_episodes' AND column_name = 'ask_side_count'
    ) THEN
        ALTER TABLE flow_episodes
            ADD COLUMN ask_side_count INTEGER NOT NULL DEFAULT 0;
        COMMENT ON COLUMN flow_episodes.ask_side_count IS
            'Count of constituent flow_events where bid_ask_class = AT_ASK or ABOVE_ASK.';
    END IF;

    -- ask_side_pct
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'flow_episodes' AND column_name = 'ask_side_pct'
    ) THEN
        ALTER TABLE flow_episodes
            ADD COLUMN ask_side_pct NUMERIC(5,4);
        COMMENT ON COLUMN flow_episodes.ask_side_pct IS
            'ask_side_count / trade_count, pre-computed at episode merge. NULL when trade_count = 0.';
    END IF;

    -- vol_oi_signal
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'flow_episodes' AND column_name = 'vol_oi_signal'
    ) THEN
        ALTER TABLE flow_episodes
            ADD COLUMN vol_oi_signal BOOLEAN NOT NULL DEFAULT FALSE;
        COMMENT ON COLUMN flow_episodes.vol_oi_signal IS
            'TRUE when contract_volume_at_close > contract_oi_at_open. Updated at each episode PATCH.';
    END IF;

    -- notional_tier
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'flow_episodes' AND column_name = 'notional_tier'
    ) THEN
        ALTER TABLE flow_episodes
            ADD COLUMN notional_tier TEXT
            CHECK (notional_tier = ANY (ARRAY['WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN']));
        COMMENT ON COLUMN flow_episodes.notional_tier IS
            'Alert tier based on total_premium. WATCH < $50k, NOTEWORTHY $50k-$500k, BLOCK $500k-$1M, GOLDEN >= $1M.';
    END IF;

    -- dte_bucket
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'flow_episodes' AND column_name = 'dte_bucket'
    ) THEN
        ALTER TABLE flow_episodes
            ADD COLUMN dte_bucket TEXT
            CHECK (dte_bucket = ANY (ARRAY['0-7', '8-30', '31-60', '61-90', '90+']));
        COMMENT ON COLUMN flow_episodes.dte_bucket IS
            'DTE range bucket derived from episode constituent print expiry. Pre-computed for signal gate filtering.';
    END IF;
END
$$;

-- =============================================================================
-- SECTION 9: ADD Steamroom snapshot columns to signal_history (3 columns)
-- Required by REARCH-006 to snapshot episode quality at signal emission time.
-- =============================================================================

DO $$
BEGIN
    -- episode_steamroom_score snapshot
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'signal_history' AND column_name = 'episode_steamroom_score'
    ) THEN
        ALTER TABLE signal_history
            ADD COLUMN episode_steamroom_score INTEGER;
        COMMENT ON COLUMN signal_history.episode_steamroom_score IS
            'Snapshot of flow_episodes.episode_steamroom_score at signal emission time. Used for signal quality attribution.';
    END IF;

    -- ask_side_pct snapshot
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'signal_history' AND column_name = 'ask_side_pct'
    ) THEN
        ALTER TABLE signal_history
            ADD COLUMN ask_side_pct NUMERIC(5,4);
        COMMENT ON COLUMN signal_history.ask_side_pct IS
            'Snapshot of flow_episodes.ask_side_pct at signal emission time.';
    END IF;

    -- vol_oi_ratio snapshot
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'signal_history' AND column_name = 'vol_oi_ratio'
    ) THEN
        ALTER TABLE signal_history
            ADD COLUMN vol_oi_ratio NUMERIC(10,4);
        COMMENT ON COLUMN signal_history.vol_oi_ratio IS
            'Snapshot of flow_episodes.volume_oi_ratio at signal emission time.';
    END IF;
END
$$;

-- =============================================================================
-- SECTION 10: Post-migration validation assertions
-- These will cause the transaction to ROLLBACK if any expectation is violated,
-- preventing a partial migration from being committed.
-- =============================================================================

DO $$
DECLARE
    v_count INTEGER;
BEGIN
    -- Assert: backtest_results does not exist
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'backtest_results') THEN
        RAISE EXCEPTION 'VALIDATION FAILED: backtest_results table still exists';
    END IF;

    -- Assert: gate_configs does not exist
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'gate_configs') THEN
        RAISE EXCEPTION 'VALIDATION FAILED: gate_configs table still exists';
    END IF;

    -- Assert: gate_config_audit does not exist
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'gate_config_audit') THEN
        RAISE EXCEPTION 'VALIDATION FAILED: gate_config_audit table still exists';
    END IF;

    -- Assert: flow_events retired columns are gone
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'flow_events' AND column_name IN ('is_golden_sweep', 'influence_tier', 'conviction_score')) THEN
        RAISE EXCEPTION 'VALIDATION FAILED: retired column(s) still present on flow_events';
    END IF;

    -- Assert: flow_episodes.seed_episode is gone
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'flow_episodes' AND column_name = 'seed_episode') THEN
        RAISE EXCEPTION 'VALIDATION FAILED: seed_episode column still present on flow_episodes';
    END IF;

    -- Assert: signal_history swarm + legacy score columns are gone (D1 fix: flow_score added)
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'signal_history'
          AND column_name IN ('swarm_direction','swarm_confidence','swarm_agents',
                              'swarm_bull_votes','swarm_bear_votes','swarm_hold_votes',
                              'volume_premium_factor','flow_score')
    ) THEN
        RAISE EXCEPTION 'VALIDATION FAILED: retired swarm/legacy score column(s) still present on signal_history';
    END IF;

    -- Assert: no signal_history rows violate the new alert_level constraint
    SELECT COUNT(*) INTO v_count
    FROM signal_history
    WHERE alert_level NOT IN ('WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN');
    IF v_count > 0 THEN
        RAISE EXCEPTION 'VALIDATION FAILED: % signal_history rows have non-conforming alert_level after backfill', v_count;
    END IF;

    -- Assert: no signal_history rows violate the new direction constraint
    SELECT COUNT(*) INTO v_count
    FROM signal_history
    WHERE direction NOT IN ('BULLISH', 'BEARISH', 'NEUTRAL');
    IF v_count > 0 THEN
        RAISE EXCEPTION 'VALIDATION FAILED: % signal_history rows have non-conforming direction after backfill', v_count;
    END IF;

    -- Assert: new flow_episodes Steamroom columns exist
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'flow_episodes' AND column_name = 'episode_steamroom_score') THEN
        RAISE EXCEPTION 'VALIDATION FAILED: episode_steamroom_score not added to flow_episodes';
    END IF;

    -- Assert: flow_score is fully retired from signal_history
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'signal_history' AND column_name = 'flow_score') THEN
        RAISE EXCEPTION 'VALIDATION FAILED: flow_score column still present on signal_history — should have been dropped in Section 6';
    END IF;

    RAISE NOTICE 'VALIDATION PASSED: All post-migration assertions satisfied.';
END
$$;

COMMIT;

-- =============================================================================
-- POST-COMMIT: Run after the transaction commits successfully.
-- Copy-paste these queries manually or into a post-migration CI check.
-- (Cannot run inside the transaction above as they are advisory, not blocking.)
-- =============================================================================

-- Quick schema sanity check:
-- SELECT column_name FROM information_schema.columns WHERE table_name = 'flow_episodes' ORDER BY ordinal_position;
-- SELECT column_name FROM information_schema.columns WHERE table_name = 'signal_history' ORDER BY ordinal_position;
-- SELECT column_name FROM information_schema.columns WHERE table_name = 'flow_events'   ORDER BY ordinal_position;
-- SELECT table_name   FROM information_schema.tables  WHERE table_schema = 'public' ORDER BY table_name;

-- Constraint verification:
-- SELECT conname, consrc FROM pg_constraint WHERE conrelid = 'signal_history'::regclass AND contype = 'c';

-- Row count sanity:
-- SELECT COUNT(*), COUNT(episode_steamroom_score) FROM flow_episodes;
-- SELECT COUNT(*), COUNT(ask_side_pct) FROM signal_history;

-- Verify flow_score is fully retired:
-- SELECT COUNT(*) FROM signal_history WHERE flow_score IS NOT NULL;  -- should error (column gone)
-- SELECT column_name FROM information_schema.columns WHERE table_name = 'signal_history' AND column_name = 'flow_score';  -- 0 rows expected
