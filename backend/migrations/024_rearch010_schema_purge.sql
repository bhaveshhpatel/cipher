-- =============================================================================
-- Migration 024: REARCH-010 Schema Purge
-- =============================================================================
-- Removes all database artefacts that belong to retired subsystems:
--
--   1. signal_history — drop swarm columns (004/005), influence_tier (003/005),
--                       backtest_score (003/005), composite_score (003/005),
--                       flow_score (003/005), volume_premium_factor (003/005)
--                       + all dependent indexes
--
--   2. gate_configs        — DROP TABLE + indexes + trigger + function (019)
--   3. gate_config_audit   — DROP TABLE + indexes (019)
--
-- What is NOT touched (live REARCH columns — hands off):
--   flow_episodes : last_signaled_premium, duration_seconds, started_at, updated_at  (015)
--   flow_events   : dte, fill_price, bid, ask, size, bid_ask_class, is_aggressive,
--                   exchange_count, fill_count, open_interest, iv, underlying_price,
--                   occ_symbol (017), order_side, strong_sentiment,
--                   execution_mechanic (018)
--
-- Replacement columns for the purged signal_history fields will be added by
-- REARCH-007 (signal_config / steamroom score columns).
--
-- Safe to re-run: all DROP statements use IF EXISTS.
-- =============================================================================


-- =============================================================================
-- SECTION 1 — signal_history: drop retired indexes then columns
-- =============================================================================

-- ── 1a. Indexes that reference columns being dropped ─────────────────────────
-- Must drop before the columns; Postgres will error if a column backing an
-- index is dropped while the index still exists.

DROP INDEX IF EXISTS public.idx_signal_history_influence_tier;
DROP INDEX IF EXISTS public.idx_signal_history_composite_score;
-- idx_signal_history_rec_score spans (recommendation, composite_score DESC, created_at DESC)
-- composite_score is going away so this composite index must be rebuilt later by REARCH-007
DROP INDEX IF EXISTS public.idx_signal_history_rec_score;

-- ── 1b. Drop swarm columns (added in 004 / re-added idempotently in 005) ─────

ALTER TABLE public.signal_history
  DROP COLUMN IF EXISTS swarm_direction,
  DROP COLUMN IF EXISTS swarm_confidence,
  DROP COLUMN IF EXISTS swarm_agents,
  DROP COLUMN IF EXISTS swarm_bull_votes,
  DROP COLUMN IF EXISTS swarm_bear_votes,
  DROP COLUMN IF EXISTS swarm_hold_votes;

-- ── 1c. Drop legacy scoring + classification columns (003 / 005) ─────────────
-- influence_tier  — replaced by APEX alert_level in REARCH-007
-- backtest_score  — backtest engine retired; REARCH-010 decision
-- composite_score — formula broken without backtest_score; REARCH-007 adds replacement
-- flow_score      — raw intermediate; superseded by episode_steamroom_score (REARCH-007)
-- volume_premium_factor — V>OI derived field, no longer a scoring input column

ALTER TABLE public.signal_history
  DROP COLUMN IF EXISTS influence_tier,
  DROP COLUMN IF EXISTS backtest_score,
  DROP COLUMN IF EXISTS composite_score,
  DROP COLUMN IF EXISTS flow_score,
  DROP COLUMN IF EXISTS volume_premium_factor;


-- =============================================================================
-- SECTION 2 — gate_configs: drop trigger, function, indexes, table
-- =============================================================================

-- ── 2a. Trigger (must be dropped before the function that backs it) ───────────

DROP TRIGGER IF EXISTS trg_gate_configs_updated_at ON public.gate_configs;

-- ── 2b. Trigger function ──────────────────────────────────────────────────────

DROP FUNCTION IF EXISTS public._set_gate_configs_updated_at();

-- ── 2c. Indexes ───────────────────────────────────────────────────────────────

DROP INDEX IF EXISTS public.idx_gate_configs_gate_name;
DROP INDEX IF EXISTS public.idx_gate_configs_gate_tier;

-- ── 2d. Table (CASCADE removes the RLS policies automatically) ────────────────

DROP TABLE IF EXISTS public.gate_configs CASCADE;


-- =============================================================================
-- SECTION 3 — gate_config_audit: drop indexes, table
-- =============================================================================

DROP INDEX IF EXISTS public.idx_gate_config_audit_changed_at;
DROP INDEX IF EXISTS public.idx_gate_config_audit_gate_name;
DROP INDEX IF EXISTS public.idx_gate_config_audit_tier;

DROP TABLE IF EXISTS public.gate_config_audit CASCADE;


-- =============================================================================
-- SECTION 4 — Verification (non-destructive, returns 0 rows on success)
-- =============================================================================

-- Should return 0 rows — none of the purged columns should exist
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (
    (table_name = 'signal_history' AND column_name IN (
      'swarm_direction', 'swarm_confidence', 'swarm_agents',
      'swarm_bull_votes', 'swarm_bear_votes', 'swarm_hold_votes',
      'influence_tier', 'backtest_score', 'composite_score',
      'flow_score', 'volume_premium_factor'
    ))
  )
ORDER BY table_name, column_name;

-- Should return 0 rows — neither table should exist
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('gate_configs', 'gate_config_audit');
