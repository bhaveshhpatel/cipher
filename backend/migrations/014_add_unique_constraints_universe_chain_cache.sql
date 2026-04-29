-- Migration 014: Add UNIQUE constraints to options_universe_symbols and options_chain_cache
--
-- ROOT CAUSE:
--   PostgREST upsert with on_conflict="col1,col2" requires a UNIQUE constraint at
--   the DB level to fire correctly. Without it, PostgREST silently falls back to a
--   plain INSERT on every call — causing unbounded row growth across restarts.
--
--   Migration 013 added the constraint for options_universe_symbols but missed
--   options_chain_cache, which shares the same (snapshot_id, occ_symbol) upsert
--   pattern in _sync_save_chain_cache. This migration closes that gap.
--
-- APPLIED: 2026-04-29 to cipher-database (kpajucxqlrteckfuafvq)
--
-- This migration is fully idempotent — safe to re-run.

-- -------------------------------------------------------------------------
-- options_universe_symbols: UNIQUE (snapshot_id, symbol)
-- -------------------------------------------------------------------------

-- Step 1: Remove any duplicate (snapshot_id, symbol) rows, keeping the most
-- recently inserted physical row for each unique pair.
DELETE FROM options_universe_symbols
WHERE ctid NOT IN (
  SELECT MAX(ctid)
  FROM options_universe_symbols
  GROUP BY snapshot_id, symbol
);

-- Step 2: Drop both possible prior constraint names (idempotent).
ALTER TABLE options_universe_symbols
  DROP CONSTRAINT IF EXISTS options_universe_symbols_snapshot_symbol_key;

ALTER TABLE options_universe_symbols
  DROP CONSTRAINT IF EXISTS uq_universe_symbols_snapshot_symbol;

-- Step 3: Add the canonical constraint.
ALTER TABLE options_universe_symbols
  ADD CONSTRAINT uq_universe_symbols_snapshot_symbol
  UNIQUE (snapshot_id, symbol);

-- -------------------------------------------------------------------------
-- options_chain_cache: UNIQUE (snapshot_id, occ_symbol)
-- -------------------------------------------------------------------------

-- Step 4: Remove any duplicate (snapshot_id, occ_symbol) rows.
DELETE FROM options_chain_cache
WHERE ctid NOT IN (
  SELECT MAX(ctid)
  FROM options_chain_cache
  GROUP BY snapshot_id, occ_symbol
);

-- Step 5: Drop prior constraint if it exists (idempotent).
ALTER TABLE options_chain_cache
  DROP CONSTRAINT IF EXISTS uq_chain_cache_snapshot_occ;

-- Step 6: Add the constraint.
ALTER TABLE options_chain_cache
  ADD CONSTRAINT uq_chain_cache_snapshot_occ
  UNIQUE (snapshot_id, occ_symbol);

-- -------------------------------------------------------------------------
-- Verification (run manually)
-- -------------------------------------------------------------------------
-- SELECT indexname, indexdef FROM pg_indexes
--   WHERE tablename IN ('options_universe_symbols', 'options_chain_cache')
--   ORDER BY tablename, indexname;
--
-- Expected output includes:
--   options_chain_cache    | uq_chain_cache_snapshot_occ    | UNIQUE (snapshot_id, occ_symbol)
--   options_universe_symbols | uq_universe_symbols_snapshot_symbol | UNIQUE (snapshot_id, symbol)
