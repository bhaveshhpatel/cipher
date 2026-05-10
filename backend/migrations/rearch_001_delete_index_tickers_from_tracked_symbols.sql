-- REARCH-001: Index Symbol Purge
-- Migration: delete_index_tickers_from_tracked_symbols
--
-- Removes all index ticker rows from the symbol registry tables.
-- Must run BEFORE rearch_001_add_index_blacklist_constraint.sql so that
-- existing rows do not violate the new CHECK constraint at ADD time.
--
-- Deleted tickers: SPX, SPXW, SPXPM, NDX, NDXP, VIX, VIXW, RUT, MRUT, DJX, XSP
-- Deleted prefix:  '$%' (any dollar-prefixed index notation)
--
-- A NOTIFY is issued after each table so that any connected application
-- workers that cache symbol lists can reload immediately.
--
-- Safe to re-run: DELETE WHERE is idempotent.

BEGIN;

DELETE FROM tracked_symbols
WHERE UPPER(symbol) IN (
  'SPX','SPXW','SPXPM','NDX','NDXP',
  'VIX','VIXW','RUT','MRUT','DJX','XSP'
)
OR symbol LIKE '$%';

NOTIFY tracked_symbols_changed, 'rearch_001_index_purge';

DELETE FROM options_universe_symbols
WHERE UPPER(symbol) IN (
  'SPX','SPXW','SPXPM','NDX','NDXP',
  'VIX','VIXW','RUT','MRUT','DJX','XSP'
)
OR symbol LIKE '$%';

NOTIFY options_universe_symbols_changed, 'rearch_001_index_purge';

COMMIT;
