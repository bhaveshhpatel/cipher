-- Migration 013: Add UNIQUE constraint to options_universe_symbols(snapshot_id, symbol)
--
-- ROOT CAUSE FIX (2026-04-28):
--   Supabase PostgREST upsert with on_conflict="snapshot_id,symbol" requires a
--   UNIQUE constraint at the database level to fire correctly. Without it, PostgREST
--   silently falls back to a plain INSERT on every call, causing exponential row
--   growth across deployments (Issue 1).
--
--   This migration:
--     1. Deduplicates existing rows (keeps the most recently updated row per pair)
--     2. Adds the missing UNIQUE constraint
--     3. Cleans up orphaned options_chain_cache rows tied to pruned snapshot_ids
--
-- Run once in Supabase SQL Editor or via run_migrations.py

-- Step 1: Remove duplicate (snapshot_id, symbol) rows, keeping the row with the
-- highest ctid (most recently inserted physical row) for each unique pair.
DELETE FROM options_universe_symbols
WHERE ctid NOT IN (
  SELECT MAX(ctid)
  FROM options_universe_symbols
  GROUP BY snapshot_id, symbol
);

-- Step 2: Add the unique constraint that PostgREST on_conflict requires.
-- IF NOT EXISTS prevents errors if this migration is accidentally run twice.
ALTER TABLE options_universe_symbols
  ADD CONSTRAINT IF NOT EXISTS options_universe_symbols_snapshot_symbol_key
  UNIQUE (snapshot_id, symbol);

-- Step 3: Clean up orphaned options_chain_cache rows whose snapshot_id no longer
-- exists in options_universe_snapshots. These accumulate every deployment when
-- new snapshot_ids are created because on_conflict never fired.
-- Safe to delete: the cache is rebuilt from scratch on each registry build().
DELETE FROM options_chain_cache
WHERE snapshot_id IS NOT NULL
  AND snapshot_id NOT IN (
    SELECT id FROM options_universe_snapshots
  );

-- Verification queries (run manually to confirm):
-- SELECT COUNT(*) FROM options_universe_symbols;           -- should be ~1-2k rows
-- SELECT COUNT(*) FROM options_chain_cache;                -- should be dramatically smaller
-- SELECT snapshot_id, COUNT(*) FROM options_universe_symbols GROUP BY snapshot_id;
-- \d options_universe_symbols                              -- should show the unique index
