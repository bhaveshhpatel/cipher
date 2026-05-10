-- REARCH-001: Index Symbol Purge
-- Migration: add_index_blacklist_constraint
--
-- Adds a CHECK constraint to options_universe_symbols so that no index
-- ticker can be inserted or updated into the DB even if application-layer
-- guards are bypassed.
--
-- NOTE: tracked_symbols does not exist in the cipher schema — constraint
-- is applied to options_universe_symbols only.
--
-- Blocked tickers (exact match, case-insensitive via UPPER()):
--   SPX, SPXW, SPXPM, NDX, NDXP, VIX, VIXW, RUT, MRUT, DJX, XSP
-- Blocked prefix: '$' (cash-settled index notation in Tradier OCC format)
--
-- Run AFTER rearch_001_delete_index_tickers_from_tracked_symbols.sql so that
-- no existing index rows violate the constraint at ADD time.
--
-- Safe to re-run: IF NOT EXISTS guard prevents duplicate constraint errors.

BEGIN;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_options_universe_symbols_no_index'
      AND conrelid = 'options_universe_symbols'::regclass
  ) THEN
    ALTER TABLE options_universe_symbols
      ADD CONSTRAINT chk_options_universe_symbols_no_index
      CHECK (
        UPPER(symbol) NOT IN (
          'SPX','SPXW','SPXPM','NDX','NDXP',
          'VIX','VIXW','RUT','MRUT','DJX','XSP'
        )
        AND symbol NOT LIKE '$%'
      );
  END IF;
END $$;

COMMIT;
