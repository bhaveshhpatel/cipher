-- Migration 017 — flow_events missing columns
-- Date: 2026-05-01
--
-- flow_store.py::persist_flow_event() writes 24 fields per row but the
-- table (created in 006 + amended in 008/009) only has 13 columns.
-- Every insert from the live stream is hitting a PostgREST 400
-- (unknown column) error and silently dropping flow data.
--
-- This migration adds the 11 missing columns so the table schema
-- matches the row dict built in persist_flow_event():
--
--   dte              INT           -- days to expiration at time of trade
--   fill_price       NUMERIC(10,4) -- execution price per contract
--   bid              NUMERIC(10,4) -- NBBO bid at time of trade
--   ask              NUMERIC(10,4) -- NBBO ask at time of trade
--   size             INT           -- number of contracts in this fill
--   bid_ask_class    TEXT          -- BID | MID | ASK
--   is_aggressive    BOOLEAN       -- true when fill at or above ask
--   exchange_count   INT           -- number of exchanges with prints
--   fill_count       INT           -- number of separate fills in this event
--   open_interest    INT           -- OI for the contract at trade time
--   iv               NUMERIC(8,5)  -- implied volatility (0.0–5.0 range)
--   underlying_price NUMERIC(12,4) -- spot price of underlying at trade time
--   occ_symbol       TEXT          -- OCC-formatted symbol (e.g. SPY   260117C00560000)
--
-- All columns are nullable / have safe defaults so that historical rows
-- (written before this migration) are backfilled cleanly without errors.
--
-- Indexes are added on the most query-critical columns.

-- ──────────────────────────────────────────────────────
-- Columns
-- ──────────────────────────────────────────────────────

ALTER TABLE public.flow_events
  ADD COLUMN IF NOT EXISTS dte              INTEGER,
  ADD COLUMN IF NOT EXISTS fill_price       NUMERIC(10,4),
  ADD COLUMN IF NOT EXISTS bid              NUMERIC(10,4),
  ADD COLUMN IF NOT EXISTS ask              NUMERIC(10,4),
  ADD COLUMN IF NOT EXISTS size             INTEGER,
  ADD COLUMN IF NOT EXISTS bid_ask_class    TEXT,
  ADD COLUMN IF NOT EXISTS is_aggressive    BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS exchange_count   INTEGER NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS fill_count       INTEGER NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS open_interest    INTEGER,
  ADD COLUMN IF NOT EXISTS iv               NUMERIC(8,5),
  ADD COLUMN IF NOT EXISTS underlying_price NUMERIC(12,4),
  ADD COLUMN IF NOT EXISTS occ_symbol       TEXT;

-- ──────────────────────────────────────────────────────
-- Indexes
-- ──────────────────────────────────────────────────────

-- Fast aggressive-flow lookups (exclude synthetic rows + filter aggressive)
CREATE INDEX IF NOT EXISTS idx_flow_events_aggressive
  ON public.flow_events (is_aggressive)
  WHERE is_aggressive = true;

-- OCC symbol lookups (used by C-003 sweep upgrade PATCH in flow_store.py)
CREATE INDEX IF NOT EXISTS idx_flow_events_occ_symbol
  ON public.flow_events (occ_symbol)
  WHERE occ_symbol IS NOT NULL;

-- Premium + trade-type composite (common filter for frontend flow table)
CREATE INDEX IF NOT EXISTS idx_flow_events_trade_type
  ON public.flow_events (trade_type);

-- ──────────────────────────────────────────────────────
-- Verify (non-destructive, returns 0 rows on success)
-- ──────────────────────────────────────────────────────
SELECT column_name, data_type, column_default, is_nullable
FROM information_schema.columns
WHERE table_name = 'flow_events'
  AND column_name IN (
    'dte', 'fill_price', 'bid', 'ask', 'size',
    'bid_ask_class', 'is_aggressive', 'exchange_count',
    'fill_count', 'open_interest', 'iv',
    'underlying_price', 'occ_symbol'
  )
ORDER BY ordinal_position;
