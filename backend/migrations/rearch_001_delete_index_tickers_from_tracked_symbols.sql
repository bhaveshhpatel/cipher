-- REARCH-001: Index Symbol Purge
-- Migration: delete_index_tickers_from_tracked_symbols
--
-- Removes all index ticker rows from options_universe_symbols.
-- NOTE: tracked_symbols does not exist in the cipher schema — only
-- options_universe_symbols is used for symbol registry storage.
--
-- Deleted tickers: SPX, SPXW, SPXPM, NDX, NDXP, VIX, VIXW, RUT, MRUT, DJX, XSP
-- Deleted prefix:  '$%' (any dollar-prefixed index notation)
--
-- A NOTIFY is issued after the table so that any connected application
-- workers that cache symbol lists can reload immediately.
--
-- Safe to re-run: DELETE WHERE is idempotent.

BEGIN;

DELETE FROM options_universe_symbols
WHERE UPPER(symbol) IN (
  'SPX','SPXW','SPXPM','NDX','NDXP',
  'VIX','VIXW','RUT','MRUT','DJX','XSP'
)
OR symbol LIKE '$%';

NOTIFY options_universe_symbols_changed, 'rearch_001_index_purge';

COMMIT;
