-- Migration 035: SA-3 — Add episode_id FK on flow_events → flow_episodes
--
-- Decision
-- --------
-- flow_events and flow_episodes are written in two distinct async paths:
--   1. persist_flow_event()   — called inline for every classified tick
--   2. persist_flow_episode() — called after Signal Gate, may be skipped
--      (e.g. below-threshold ticks that still get persisted as events).
--
-- This means a non-NULL FK is NOT enforceable: legitimate flow_events rows
-- exist with no corresponding episode (below-gate ticks, pre-episode ticks).
--
-- Resolution: nullable FK with ON DELETE SET NULL.
-- See ADR docs/adr/SA-3-flow-events-episode-fk.md for full rationale.
--
-- Backfill strategy (offline, post-deploy)
-- -----------------------------------------
-- Step 1 (immediate): column added as NULL — zero-risk, no lock required
--   beyond the ALTER TABLE metadata lock.
-- Step 2 (offline, off-hours): run the backfill query below in a
--   maintenance window. It matches events to episodes by contract identity
--   (ticker, contract_type, strike, expiry) within the ING-009 merge window
--   (30 min). Only episodes created ON or AFTER the event are candidates,
--   with the closest one chosen.
--
--   UPDATE flow_events fe
--   SET episode_id = (
--     SELECT ep.id
--     FROM flow_episodes ep
--     WHERE ep.ticker        = fe.ticker
--       AND ep.contract_type = fe.contract_type
--       AND ep.strike        = fe.strike
--       AND ep.expiry        = fe.expiry
--       AND ep.created_at >= fe.created_at
--       AND ep.created_at <= fe.created_at + INTERVAL '30 minutes'
--     ORDER BY ep.created_at ASC
--     LIMIT 1
--   )
--   WHERE fe.episode_id IS NULL;
--
-- Step 3: After backfill, run ANALYZE flow_events to update planner stats.
-- Rows that still have NULL after backfill are intentionally unlinked
-- (below-gate events) — leave them NULL, do NOT delete.
--
-- ON DELETE SET NULL rationale
-- ----------------------------
-- Episodes can be pruned by a future retention policy. Setting episode_id
-- to NULL on event rows (rather than CASCADE DELETE) preserves the raw
-- flow record, which has independent audit value.

-- ---------------------------------------------------------------------------
-- 1. Add the column (nullable, no default)
-- ---------------------------------------------------------------------------
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS episode_id UUID;

-- ---------------------------------------------------------------------------
-- 2. Add FK constraint
--    ON DELETE SET NULL: if the parent episode is deleted, event rows lose
--      their link but are NOT deleted — raw tick data is preserved.
--    ON UPDATE CASCADE: if flow_episodes.id is ever changed (rare),
--      references stay consistent.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.table_constraints
    WHERE constraint_name = 'fk_flow_events_episode_id'
      AND table_name = 'flow_events'
      AND table_schema = 'public'
  ) THEN
    ALTER TABLE flow_events
      ADD CONSTRAINT fk_flow_events_episode_id
      FOREIGN KEY (episode_id)
      REFERENCES flow_episodes (id)
      ON DELETE SET NULL
      ON UPDATE CASCADE;
  END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 3. Partial index — only index rows that have an episode_id.
--    NULL rows are excluded, keeping the index lean during the pre-backfill
--    window when most rows are NULL.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_flow_events_episode_id
  ON flow_events (episode_id)
  WHERE episode_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. Composite index for the backfill query (step 2 above) and join queries
--    that look up episodes by contract identity.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_flow_events_contract_episode_lookup
  ON flow_events (ticker, contract_type, strike, expiry, created_at DESC)
  WHERE episode_id IS NULL;
