-- Migration 015: add missing columns to flow_episodes
--
-- The original 006 schema omitted 4 columns that flow_store.py writes
-- and flow.py's _query_episodes_v2 selects:
--
--   last_signaled_premium  — premium value at the time the last signal fired
--   duration_seconds       — wall-clock seconds from first to latest trade in episode
--   started_at             — timestamp of the first trade that opened the episode
--   updated_at             — timestamp of the most recent trade / signal update
--
-- Also adds:
--   • idx_flow_episodes_updated_at  — supports ORDER BY updated_at DESC in /episodes
--   • service_role UPDATE policy    — allows flow_store to UPDATE existing episode rows
--
-- Safe to run multiple times (all statements are idempotent).

-- ── Add missing columns ─────────────────────────────────────────────────────

ALTER TABLE public.flow_episodes
    ADD COLUMN IF NOT EXISTS last_signaled_premium NUMERIC(14,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS duration_seconds      INTEGER,
    ADD COLUMN IF NOT EXISTS started_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at            TIMESTAMPTZ  DEFAULT now();

-- ── Index for ORDER BY updated_at DESC ──────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_flow_episodes_updated_at
    ON public.flow_episodes (updated_at DESC);

-- ── Service role UPDATE policy (idempotent) ──────────────────────────────────
-- flow_store.py uses upsert/update to increment trade_count, total_premium,
-- last_signaled_premium, duration_seconds, and updated_at on existing episodes.

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'flow_episodes'
      AND policyname = 'service_update_flow_episodes'
  ) THEN
    CREATE POLICY service_update_flow_episodes
      ON public.flow_episodes
      FOR UPDATE
      TO service_role
      USING (true)
      WITH CHECK (true);
  END IF;
END $$;

COMMENT ON COLUMN public.flow_episodes.last_signaled_premium IS
    'Total premium at the time the most recent signal fired for this episode.';
COMMENT ON COLUMN public.flow_episodes.duration_seconds IS
    'Elapsed seconds from started_at to the most recent trade in this episode.';
COMMENT ON COLUMN public.flow_episodes.started_at IS
    'Timestamp of the first trade that opened this episode.';
COMMENT ON COLUMN public.flow_episodes.updated_at IS
    'Timestamp of the most recent trade or signal update for this episode.';
