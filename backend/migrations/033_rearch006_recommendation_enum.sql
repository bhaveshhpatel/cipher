-- ============================================================================
-- Migration 033 — REARCH-006: recommendation column enum hardening
--
-- Changes:
--   1. Change DEFAULT on signal_history.recommendation from 'HOLD' → 'NO_ACTION'
--      ('HOLD' was a legacy carryover from the old swarm-vote system and has
--      no meaning in the Steamroom framework — PBE deliberation consensus).
--
--   2. Drop any existing CHECK constraint on recommendation (idempotent — safe
--      to run even if the constraint was never added or was named differently).
--
--   3. Add a CHECK constraint enforcing the 5-value Steamroom enum:
--         BUY_CALLS | BUY_PUTS | FOLLOW_SWEEP | WATCH | NO_ACTION
--      (QA requirement: without this, typos from any caller silently persist).
--
--   4. Backfill: UPDATE any rows where recommendation = 'HOLD' → 'NO_ACTION'
--      so existing data satisfies the new constraint.
--
-- Safe to re-run: all steps use IF EXISTS / IF NOT EXISTS guards.
-- ============================================================================

BEGIN;

-- ── Step 1: Backfill legacy 'HOLD' rows before adding the constraint ─────────
-- Must happen before the CHECK is added or the constraint will reject them.
UPDATE signal_history
SET    recommendation = 'NO_ACTION'
WHERE  recommendation = 'HOLD';

-- Also backfill NULL rows to the new default so the constraint is satisfied
-- for any rows that slipped through with recommendation IS NULL.
UPDATE signal_history
SET    recommendation = 'NO_ACTION'
WHERE  recommendation IS NULL;

-- ── Step 2: Change the column DEFAULT ────────────────────────────────────────
ALTER TABLE signal_history
    ALTER COLUMN recommendation SET DEFAULT 'NO_ACTION';

-- ── Step 3: Drop any existing CHECK constraint on recommendation ─────────────
-- We don't know what name (if any) a prior constraint was given, so we use
-- a DO block that catches the "does not exist" error gracefully.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT conname
        FROM   pg_constraint
        WHERE  conrelid = 'signal_history'::regclass
          AND  contype  = 'c'
          AND  pg_get_constraintdef(oid) LIKE '%recommendation%'
    LOOP
        EXECUTE format('ALTER TABLE signal_history DROP CONSTRAINT IF EXISTS %I', r.conname);
    END LOOP;
END;
$$;

-- ── Step 4: Add the authoritative CHECK constraint ───────────────────────────
-- Named explicitly so future migrations can reference or drop it by name.
ALTER TABLE signal_history
    ADD CONSTRAINT chk_recommendation_enum
    CHECK (
        recommendation IN (
            'BUY_CALLS',
            'BUY_PUTS',
            'FOLLOW_SWEEP',
            'WATCH',
            'NO_ACTION'
        )
    );

COMMIT;

-- Verification query (run manually to confirm):
-- SELECT recommendation, COUNT(*)
-- FROM   signal_history
-- GROUP  BY recommendation
-- ORDER  BY recommendation;
