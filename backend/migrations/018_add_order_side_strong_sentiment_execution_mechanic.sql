-- Migration 018 — S2.5: Add order_side, strong_sentiment, execution_mechanic to flow_events
-- Date: 2026-05-01
--
-- All three columns must land together. execution_mechanic cannot be backfilled
-- retroactively on a high-volume stream table — it must be populated from first write.
-- Per QA Lead note in cipher_apex_engineering_spec.md (S2.5 section).
--
-- order_side       TEXT NOT NULL  — BUY | SELL | UNKNOWN (derived from bid_ask_class + contract_type)
-- strong_sentiment BOOLEAN NOT NULL — TRUE when direction inference is unambiguous (real NBBO, non-MID)
-- execution_mechanic TEXT NOT NULL — one of 6 mechanic classes from order_side_classifier.py
--
-- Idempotent: safe to re-run (IF NOT EXISTS + UPDATE WHERE IS NULL guard)

-- ──────────────────────────────────────────────────────────────────────────────
-- Columns
-- ──────────────────────────────────────────────────────────────────────────────

ALTER TABLE public.flow_events
  ADD COLUMN IF NOT EXISTS order_side TEXT NOT NULL DEFAULT 'UNKNOWN'
    CHECK (order_side IN ('BUY', 'SELL', 'UNKNOWN')),
  ADD COLUMN IF NOT EXISTS strong_sentiment BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS execution_mechanic TEXT NOT NULL DEFAULT 'AMBIGUOUS_LONG'
    CHECK (execution_mechanic IN (
      'DIRECTIONAL_LONG',
      'DIRECTIONAL_SHORT',
      'PASSIVE_BULLISH',
      'PASSIVE_BEARISH',
      'AMBIGUOUS_LONG',
      'AMBIGUOUS_SHORT'
    ));

-- ──────────────────────────────────────────────────────────────────────────────
-- Index
-- ──────────────────────────────────────────────────────────────────────────────

-- order_side index: supports analytics queries filtering by direction
CREATE INDEX IF NOT EXISTS idx_flow_events_order_side
  ON public.flow_events (order_side);

-- ──────────────────────────────────────────────────────────────────────────────
-- Backfill guard
-- ──────────────────────────────────────────────────────────────────────────────
-- NOT NULL DEFAULT covers pre-existing rows at column-add time.
-- This UPDATE is a safety net only — catches any rows that bypassed defaults.
-- Conservative: do not attempt to infer direction from historical data.

UPDATE public.flow_events
  SET order_side         = 'UNKNOWN',
      strong_sentiment   = FALSE,
      execution_mechanic = 'AMBIGUOUS_LONG'
  WHERE order_side IS NULL
     OR strong_sentiment IS NULL
     OR execution_mechanic IS NULL;

-- ──────────────────────────────────────────────────────────────────────────────
-- Verify (non-destructive — returns column metadata, 0 error rows on success)
-- ──────────────────────────────────────────────────────────────────────────────

SELECT column_name, data_type, column_default, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'flow_events'
  AND column_name  IN ('order_side', 'strong_sentiment', 'execution_mechanic')
ORDER BY ordinal_position;
