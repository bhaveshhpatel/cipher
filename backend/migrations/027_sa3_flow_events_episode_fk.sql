-- Migration 027: SA-3 — add episode_id FK from flow_events → flow_episodes
--
-- Context
-- -------
-- flow_events and flow_episodes were created independently (migration 006).
-- Every flow_event conceptually belongs to an episode (ING-009, migration
-- context), but no FK column existed.  This migration closes the gap by:
--
--   1. Adding flow_events.episode_id (BIGINT, nullable) with a real FK
--      referencing flow_episodes(id) ON DELETE SET NULL.
--
--   2. Running a safe, batched backfill that joins existing rows on the
--      merge-key (ticker + direction + contract_type + strike + expiry)
--      within the ING-009 merge window (±30 min), setting episode_id on
--      rows where a match is unambiguous.
--
--   3. Pre-migration rows that pre-date ING-009 (before ~2026-05-06) will
--      remain NULL — this is intentional and documented in the ADR.
--
-- FK semantics
-- ------------
--   ON DELETE SET NULL  — if an episode row is deleted (manual cleanup,
--      retention purge) the event row is preserved with episode_id=NULL.
--      This is safer than CASCADE for an audit/telemetry table.
--
-- Safe to run multiple times (idempotent column add guard).
-- Expected runtime on prod: < 5 s for typical row counts.

-- ─────────────────────────────────────────────────────────────────────────
-- Step 1: add the column (idempotent)
-- ─────────────────────────────────────────────────────────────────────────
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'flow_events'
          AND column_name  = 'episode_id'
    ) THEN
        ALTER TABLE public.flow_events
            ADD COLUMN episode_id BIGINT
                REFERENCES public.flow_episodes(id)
                    ON DELETE SET NULL
                    DEFERRABLE INITIALLY DEFERRED;

        COMMENT ON COLUMN public.flow_events.episode_id IS
            'FK → flow_episodes.id.  NULL for rows written before ING-009 '
            '(~2026-05-06) or for rare no-episode ticks.  ON DELETE SET NULL: '
            'episode deletion preserves the event row for audit purposes. '
            'SA-3 (2026-05-24).';
    END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────
-- Step 2: covering index (partial — only non-NULL rows benefit)
-- ─────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_flow_events_episode_id
    ON public.flow_events (episode_id)
    WHERE episode_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────
-- Step 3: backfill — batch-join existing rows to episodes
--
-- Strategy: for each flow_event that is NULL on episode_id, find the
-- single most-recently-opened episode within ±30 min that matches on
-- (ticker, contract_type, strike, expiry).  If exactly one candidate
-- exists, set episode_id.  If zero or >1 candidates exist, leave NULL
-- (ambiguity-safe).
--
-- The CTE approach is a single-pass UPDATE and is safe to re-run:
-- already-set rows are excluded by the WHERE episode_id IS NULL guard.
-- ─────────────────────────────────────────────────────────────────────────
WITH candidates AS (
    SELECT
        fe.id                          AS event_id,
        ep.id                          AS episode_id,
        ROW_NUMBER() OVER (
            PARTITION BY fe.id
            ORDER BY ep.created_at DESC
        )                              AS rn,
        COUNT(*) OVER (
            PARTITION BY fe.id
        )                              AS candidate_count
    FROM public.flow_events  fe
    JOIN public.flow_episodes ep
        ON  ep.ticker         = fe.ticker
        AND ep.contract_type  = fe.contract_type
        AND COALESCE(ep.strike::text, '')  = COALESCE(fe.strike::text, '')
        AND COALESCE(ep.expiry, '')        = COALESCE(fe.expiry, '')
        AND ep.created_at BETWEEN fe.created_at - INTERVAL '30 minutes'
                              AND fe.created_at + INTERVAL '30 minutes'
    WHERE fe.episode_id IS NULL
)
UPDATE public.flow_events fe
SET    episode_id = c.episode_id
FROM   candidates c
WHERE  fe.id      = c.event_id
  AND  c.rn       = 1
  AND  c.candidate_count = 1;  -- unambiguous match only

-- ─────────────────────────────────────────────────────────────────────────
-- Step 4: update table comment to reflect the relationship
-- ─────────────────────────────────────────────────────────────────────────
COMMENT ON TABLE public.flow_events IS
    'One row per classified options tick. Written by flow_store.py via '
    'service role key. Read by frontend via anon key (RLS SELECT policy). '
    'episode_id (FK → flow_episodes) is NULL for pre-ING-009 rows; see ADR SA-3.';
