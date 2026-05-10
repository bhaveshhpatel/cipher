-- REARCH-010 | Stage 6: Add new Steamroom columns
--
-- flow_episodes: 6 new columns (required by REARCH-003 and REARCH-004)
-- signal_history: 3 new snapshot columns (required by REARCH-006)
--
-- PG14+: ADD COLUMN with NOT NULL DEFAULT is a catalog-only operation
-- (no table rewrite, no row-level lock beyond catalog AccessExclusiveLock).
-- All existing rows receive the DEFAULT value instantly.

BEGIN;

-- -------------------------------------------------------------------------
-- flow_episodes — 6 Steamroom columns
-- -------------------------------------------------------------------------

-- 1. WSJ Steamroom 5-dimension conviction score (0-5)
--    1 point each for: ask-side majority, vol>OI, premium >= NOTEWORTHY,
--    DTE in signal window, trade_count >= min_trade_count.
--    Computed and incremented at episode merge time by REARCH-004.
ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS episode_steamroom_score INTEGER NOT NULL DEFAULT 0
    CHECK (episode_steamroom_score BETWEEN 0 AND 5);

COMMENT ON COLUMN flow_episodes.episode_steamroom_score IS
  'WSJ Steamroom 5-dimension conviction score 0-5. 1pt each: ask-side majority, vol>OI, premium>=NOTEWORTHY, DTE in signal window, trade_count>=min_trade_count. Computed at episode merge time (REARCH-004).';

-- 2. Ask-side print count
ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS ask_side_count INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN flow_episodes.ask_side_count IS
  'Count of constituent flow_events where bid_ask_class IN (AT_ASK, ABOVE_ASK). Updated at each episode merge.';

-- 3. Ask-side percentage (pre-computed ratio, NULL when trade_count = 0)
ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS ask_side_pct NUMERIC(5,4) DEFAULT NULL;

COMMENT ON COLUMN flow_episodes.ask_side_pct IS
  'ask_side_count / trade_count. Pre-computed at each episode PATCH. NULL when trade_count = 0.';

-- 4. Vol > OI signal boolean
ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS vol_oi_signal BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN flow_episodes.vol_oi_signal IS
  'TRUE when contract_volume_at_close > contract_oi_at_open for the episode contract. Derived from chain_cache at episode merge time.';

-- 5. Notional tier at episode level
ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS notional_tier TEXT DEFAULT NULL
    CHECK (notional_tier IS NULL OR notional_tier = ANY (
      ARRAY['WATCH', 'NOTEWORTHY', 'BLOCK', 'GOLDEN']
    ));

COMMENT ON COLUMN flow_episodes.notional_tier IS
  'Alert tier based on total_premium at most recent episode update. WATCH<$50K, NOTEWORTHY $50K-$500K, BLOCK $500K-$1M, GOLDEN>=$1M. NULL until first premium threshold is crossed.';

-- 6. DTE bucket label
ALTER TABLE flow_episodes
  ADD COLUMN IF NOT EXISTS dte_bucket TEXT DEFAULT NULL
    CHECK (dte_bucket IS NULL OR dte_bucket = ANY (
      ARRAY['0-7', '8-30', '31-60', '61-90', '90+']
    ));

COMMENT ON COLUMN flow_episodes.dte_bucket IS
  'DTE range bucket derived from expiry of episode constituent prints. Pre-computed for signal gate filtering. NULL until first event sets the expiry.';

-- -------------------------------------------------------------------------
-- signal_history — 3 Steamroom snapshot columns
-- Snapshots of episode-level state at signal emission time.
-- All nullable — pre-REARCH signals have no Steamroom data.
-- -------------------------------------------------------------------------

-- 1. Steamroom score snapshot
ALTER TABLE signal_history
  ADD COLUMN IF NOT EXISTS episode_steamroom_score INTEGER DEFAULT NULL
    CHECK (episode_steamroom_score IS NULL OR episode_steamroom_score BETWEEN 0 AND 5);

COMMENT ON COLUMN signal_history.episode_steamroom_score IS
  'Snapshot of flow_episodes.episode_steamroom_score at signal emission time. NULL for pre-REARCH signals.';

-- 2. Ask-side pct snapshot
ALTER TABLE signal_history
  ADD COLUMN IF NOT EXISTS ask_side_pct NUMERIC(5,4) DEFAULT NULL;

COMMENT ON COLUMN signal_history.ask_side_pct IS
  'Snapshot of flow_episodes.ask_side_pct at signal emission time. NULL for pre-REARCH signals.';

-- 3. Vol/OI ratio snapshot
ALTER TABLE signal_history
  ADD COLUMN IF NOT EXISTS vol_oi_ratio NUMERIC(10,4) DEFAULT NULL;

COMMENT ON COLUMN signal_history.vol_oi_ratio IS
  'Snapshot of flow_episodes.volume_oi_ratio at signal emission time. NULL for pre-REARCH signals.';

COMMIT;

-- POST-STAGE VERIFICATION:

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

-- NOT NULL default check on flow_episodes new columns
SELECT
  COUNT(*) FILTER (WHERE episode_steamroom_score IS NULL) AS steamroom_nulls,
  COUNT(*) FILTER (WHERE ask_side_count IS NULL)          AS ask_side_count_nulls,
  COUNT(*) FILTER (WHERE vol_oi_signal IS NULL)           AS vol_oi_signal_nulls
FROM flow_episodes;
-- All three must be 0.
