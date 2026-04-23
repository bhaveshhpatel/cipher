-- Migration: 002_universe_symbols_quotes
-- Adds quote-derived columns to options_universe_symbols:
--   stream_eligible  — computed from last_price + volume thresholds
--   last_price       — latest trade price fetched via Tradier /v1/markets/quotes
--   volume           — daily volume fetched via Tradier /v1/markets/quotes
--
-- Step 3 of the universe pipeline:
--   After Tradier expiration validation, batch-fetch quotes (200 symbols/request,
--   ~28 parallel requests) to compute stream_eligible per symbol.
--
-- Thresholds (configurable via env):
--   UNIVERSE_MIN_PRICE  default 1.0   — exclude sub-penny / illiquid
--   UNIVERSE_MIN_VOLUME default 100000 — exclude low-activity tickers

ALTER TABLE options_universe_symbols
  ADD COLUMN IF NOT EXISTS stream_eligible BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS last_price      NUMERIC(12, 4),
  ADD COLUMN IF NOT EXISTS volume          BIGINT;

-- Index for fast StreamPoolManager queries: fetch all eligible symbols
-- from the active snapshot in one query.
CREATE INDEX IF NOT EXISTS idx_universe_symbols_eligible
  ON options_universe_symbols (snapshot_id, stream_eligible)
  WHERE stream_eligible = true;
