-- Migration 034: Fix dte_bucket CHECK constraint on flow_episodes
-- =============================================================================
-- Context
-- -------
-- rearch-010/06_add_steamroom_columns.sql attempted to add a dte_bucket column
-- with CHECK constraint using values ['0-7', '8-30', '31-60', '61-90', '90+'].
-- However, the Python code (_compute_dte_bucket in ingestion/processor.py)
-- has always produced different bucket labels:
--   '0DTE'  — dte == 0
--   '1-4'   — dte in 1..4
--   '5-60'  — dte in 5..60
--   '61-90' — dte in 61..90
--   '90+'   — dte > 90
--
-- These are more meaningful as trading labels and are already written to
-- flow_events.dte_bucket (no constraint) in production. Any episode INSERT
-- that sets dte_bucket to '0DTE', '1-4', or '5-60' would fail a CHECK
-- constraint violation if the rearch-010 constraint was applied.
--
-- Additionally, migration 028 added dte_bucket TEXT DEFAULT NULL (no constraint)
-- to flow_episodes. If that ran first, the IF NOT EXISTS clause in rearch-010
-- silently skipped the column add and the CHECK was never applied. This migration
-- handles both cases:
--   1. Drop any existing CHECK constraint on dte_bucket (idempotent)
--   2. Add the correct CHECK matching _compute_dte_bucket() output values
--
-- Same fix applied to flow_events.dte_bucket for consistency (no constraint
-- was ever on that table, but we add it now for schema integrity).
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------
-- flow_episodes.dte_bucket — drop old constraint (if any), add correct one
-- -----------------------------------------------------------------------

-- Drop any existing constraint on dte_bucket (constraint name may vary).
-- We scan pg_constraint for any CHECK on flow_episodes referencing dte_bucket.
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace ns  ON ns.oid  = rel.relnamespace
    WHERE ns.nspname  = 'public'
      AND rel.relname = 'flow_episodes'
      AND con.contype = 'c'
      AND pg_get_constraintdef(con.oid) ILIKE '%dte_bucket%'
  LOOP
    EXECUTE format('ALTER TABLE public.flow_episodes DROP CONSTRAINT IF EXISTS %I', r.conname);
  END LOOP;
END $$;

-- Ensure the column exists (idempotent — no-op if already present).
ALTER TABLE public.flow_episodes
  ADD COLUMN IF NOT EXISTS dte_bucket TEXT DEFAULT NULL;

-- Add constraint matching _compute_dte_bucket() output values.
ALTER TABLE public.flow_episodes
  ADD CONSTRAINT flow_episodes_dte_bucket_check
    CHECK (
      dte_bucket IS NULL
      OR dte_bucket = ANY (ARRAY['0DTE', '1-4', '5-60', '61-90', '90+'])
    );

COMMENT ON COLUMN public.flow_episodes.dte_bucket IS
  'DTE bucket label from _compute_dte_bucket() in ingestion/processor.py. '
  'Values: 0DTE | 1-4 | 5-60 | 61-90 | 90+. Locked at episode open (SA-3 seed-event-only). '
  'NULL until first event sets the expiry. Fixed from erroneous rearch-010 values by migration 034.';

-- -----------------------------------------------------------------------
-- flow_events.dte_bucket — add matching constraint for schema consistency
-- -----------------------------------------------------------------------

DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace ns  ON ns.oid  = rel.relnamespace
    WHERE ns.nspname  = 'public'
      AND rel.relname = 'flow_events'
      AND con.contype = 'c'
      AND pg_get_constraintdef(con.oid) ILIKE '%dte_bucket%'
  LOOP
    EXECUTE format('ALTER TABLE public.flow_events DROP CONSTRAINT IF EXISTS %I', r.conname);
  END LOOP;
END $$;

ALTER TABLE public.flow_events
  ADD COLUMN IF NOT EXISTS dte_bucket TEXT DEFAULT NULL;

ALTER TABLE public.flow_events
  ADD CONSTRAINT flow_events_dte_bucket_check
    CHECK (
      dte_bucket IS NULL
      OR dte_bucket = ANY (ARRAY['0DTE', '1-4', '5-60', '61-90', '90+'])
    );

COMMENT ON COLUMN public.flow_events.dte_bucket IS
  'DTE bucket label from _compute_dte_bucket() in ingestion/processor.py. '
  'Values: 0DTE | 1-4 | 5-60 | 61-90 | 90+. REARCH-003 quality tag. NULL on missing DTE.';

-- -----------------------------------------------------------------------
-- flow_episodes.episode_steamroom_score — ensure column exists with correct type
-- -----------------------------------------------------------------------
-- rearch-010/06 added this as INTEGER NOT NULL DEFAULT 0.
-- If that migration ran successfully the column is already present — no-op.
-- If it was skipped (e.g. partial migration), this ensures it exists.
ALTER TABLE public.flow_episodes
  ADD COLUMN IF NOT EXISTS episode_steamroom_score INTEGER NOT NULL DEFAULT 0
    CHECK (episode_steamroom_score BETWEEN 0 AND 5);

COMMENT ON COLUMN public.flow_episodes.episode_steamroom_score IS
  '5-dimension Steamroom conviction score (0-5). 1pt each: ask-side majority, '
  'vol>OI, premium>=50k (NOTEWORTHY), DTE in near-term bucket, trade_count>=3. '
  'Computed and updated by _compute_episode_steamroom_score() in flow_store.py.';

-- -----------------------------------------------------------------------
-- flow_episodes.vol_oi_signal — ensure column exists with correct type
-- -----------------------------------------------------------------------
ALTER TABLE public.flow_episodes
  ADD COLUMN IF NOT EXISTS vol_oi_signal BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN public.flow_episodes.vol_oi_signal IS
  'TRUE when contract_volume_at_close > contract_oi_at_open at most recent episode update. '
  'Computed by compute_vol_oi_signal() in flow_store.py. Written on every INSERT and PATCH.';

-- -----------------------------------------------------------------------
-- ask_side_count type reconciliation
-- -----------------------------------------------------------------------
-- Migration 028 added ask_side_count as SMALLINT DEFAULT NULL.
-- rearch-010/06 tried to add it as INTEGER NOT NULL DEFAULT 0.
-- The IF NOT EXISTS in rearch-010 may have skipped it if 028 ran first,
-- leaving SMALLINT DEFAULT NULL in production. The Python code writes int values
-- 0 or 1 — SMALLINT is fine. We promote to INTEGER and set NOT NULL DEFAULT 0
-- for consistency with the rearch-010 intent.
DO $$
BEGIN
  -- Only alter if column exists as SMALLINT (safe no-op if already INTEGER).
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'flow_episodes'
      AND column_name  = 'ask_side_count'
      AND data_type    = 'smallint'
  ) THEN
    -- Promote SMALLINT → INTEGER and set NOT NULL DEFAULT 0 to match rearch-010 intent.
    ALTER TABLE public.flow_episodes
      ALTER COLUMN ask_side_count TYPE INTEGER,
      ALTER COLUMN ask_side_count SET NOT NULL,
      ALTER COLUMN ask_side_count SET DEFAULT 0;
  END IF;
END $$;

-- Ensure column exists even if both prior migrations were skipped entirely.
ALTER TABLE public.flow_episodes
  ADD COLUMN IF NOT EXISTS ask_side_count INTEGER NOT NULL DEFAULT 0;

COMMIT;

-- -----------------------------------------------------------------------
-- Post-migration verification
-- -----------------------------------------------------------------------
-- Run these SELECT statements after applying to confirm the schema is correct.

-- flow_episodes: all 4 columns must appear
SELECT
  column_name,
  data_type,
  column_default,
  is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'flow_episodes'
  AND column_name IN (
    'dte_bucket', 'episode_steamroom_score', 'vol_oi_signal', 'ask_side_count'
  )
ORDER BY column_name;

-- dte_bucket constraint must be present on both tables with correct values
SELECT
  rel.relname   AS table_name,
  con.conname   AS constraint_name,
  pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class     rel ON rel.oid = con.conrelid
JOIN pg_namespace ns  ON ns.oid  = rel.relnamespace
WHERE ns.nspname = 'public'
  AND rel.relname IN ('flow_events', 'flow_episodes')
  AND con.contype = 'c'
  AND pg_get_constraintdef(con.oid) ILIKE '%dte_bucket%'
ORDER BY rel.relname;
