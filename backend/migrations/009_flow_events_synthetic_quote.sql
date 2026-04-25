-- Migration 009 — C-018: Synthetic Quote Tagging
-- Date: 2026-04-24
--
-- Adds is_synthetic_quote boolean column to flow_events.
-- Rows where Tradier omitted bid/ask (both = 0) and the parser
-- synthesised a ±0.5% NBBO from fill price are tagged TRUE.
--
-- These rows have unreliable bid_ask_class and is_aggressive values.
-- Exclude them from backtesting aggression and net-premium calculations:
--
--   SELECT * FROM flow_events
--   WHERE is_synthetic_quote = false
--     AND is_aggressive = true;
--
-- DEFAULT false safely backfills all historical rows as clean.
-- The partial index keeps query overhead minimal since synthetic rows
-- are expected to be a minority of total flow_events.

ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS is_synthetic_quote boolean NOT NULL DEFAULT false;

-- Partial index: only indexes synthetic rows for fast exclusion queries
CREATE INDEX IF NOT EXISTS idx_flow_events_synthetic_quote
  ON flow_events (is_synthetic_quote)
  WHERE is_synthetic_quote = true;

-- Verify
SELECT column_name, data_type, column_default, is_nullable
FROM information_schema.columns
WHERE table_name = 'flow_events'
  AND column_name = 'is_synthetic_quote';
