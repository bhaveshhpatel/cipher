-- Migration 011: Add UNIQUE constraints required for upsert idempotency
--
-- WITHOUT these constraints Supabase PostgREST silently falls back to a
-- plain INSERT when on_conflict is specified, producing duplicate rows on
-- every deployment/restart.  The application-level upsert logic in
-- universe_store.py and chain_store.py was already correct — this migration
-- is the missing DB-side enforcement.
--
-- Safe to run multiple times (IF NOT EXISTS guards).

-- -----------------------------------------------------------------------
-- options_universe_symbols
-- -----------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'options_universe_symbols_snapshot_symbol_key'
  ) THEN
    ALTER TABLE options_universe_symbols
      ADD CONSTRAINT options_universe_symbols_snapshot_symbol_key
      UNIQUE (snapshot_id, symbol);
    RAISE NOTICE 'Created UNIQUE constraint on options_universe_symbols(snapshot_id, symbol)';
  ELSE
    RAISE NOTICE 'UNIQUE constraint on options_universe_symbols already exists — skipping';
  END IF;
END $$;

-- -----------------------------------------------------------------------
-- options_chain_cache
-- -----------------------------------------------------------------------
-- Prevents exponential row growth when chain data is refreshed on every
-- deployment.  The upsert in chain_store.save_chain() uses
-- on_conflict=(snapshot_id, underlying, expiration, strike, option_type).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'options_chain_cache_natural_key'
  ) THEN
    ALTER TABLE options_chain_cache
      ADD CONSTRAINT options_chain_cache_natural_key
      UNIQUE (snapshot_id, underlying, expiration, strike, option_type);
    RAISE NOTICE 'Created UNIQUE constraint on options_chain_cache natural key';
  ELSE
    RAISE NOTICE 'UNIQUE constraint on options_chain_cache already exists — skipping';
  END IF;
END $$;
