-- Migration 007: Seed data for flow_events and signal_history
-- Run in Supabase SQL Editor.
-- Safe to run multiple times (uses INSERT ... ON CONFLICT DO NOTHING or
-- plain inserts into tables with no unique constraints).
--
-- flow_events: seed 20 rows so the Flow Scanner tab shows data immediately
-- signal_history: already has 50k+ rows from stream — no seed needed

-- ─────────────────────────────────────────────────────────────────────────
-- Seed flow_events (the table the stream writes individual ticks to)
-- These represent individual classified options ticks.
-- ─────────────────────────────────────────────────────────────────────────
INSERT INTO public.flow_events
  (ticker, contract_type, strike, expiry, premium, trade_type,
   sentiment, influence_tier, conviction_score, is_golden_sweep, created_at)
VALUES
  ('AAPL', 'CALL', 195.00, '2025-05-16', 2850000, 'SWEEP',  'BULLISH', 'WHALE',         0.91, true,  now() - interval '1 minute'),
  ('AAPL', 'CALL', 200.00, '2025-06-20', 1200000, 'BLOCK',  'BULLISH', 'INSTITUTIONAL', 0.78, false, now() - interval '3 minutes'),
  ('TSLA', 'PUT',  175.00, '2025-05-09', 3400000, 'SWEEP',  'BEARISH', 'WHALE',         0.88, true,  now() - interval '5 minutes'),
  ('NVDA', 'CALL', 950.00, '2025-05-16', 4100000, 'SWEEP',  'BULLISH', 'WHALE',         0.95, true,  now() - interval '7 minutes'),
  ('SPY',  'PUT',  520.00, '2025-05-02', 9800000, 'BLOCK',  'BEARISH', 'INSTITUTIONAL', 0.82, false, now() - interval '9 minutes'),
  ('QQQ',  'CALL', 450.00, '2025-05-09', 2200000, 'SPLIT',  'BULLISH', 'LARGE',         0.67, false, now() - interval '11 minutes'),
  ('META', 'CALL', 580.00, '2025-05-16', 1750000, 'SWEEP',  'BULLISH', 'INSTITUTIONAL', 0.79, false, now() - interval '13 minutes'),
  ('AMZN', 'CALL', 205.00, '2025-06-20', 3300000, 'BLOCK',  'BULLISH', 'WHALE',         0.85, false, now() - interval '15 minutes'),
  ('MSFT', 'PUT',  410.00, '2025-05-09', 1100000, 'SINGLE', 'BEARISH', 'LARGE',         0.55, false, now() - interval '17 minutes'),
  ('GOOG', 'CALL', 180.00, '2025-05-16', 2600000, 'SWEEP',  'BULLISH', 'WHALE',         0.90, true,  now() - interval '19 minutes'),
  ('TSLA', 'CALL', 190.00, '2025-06-20', 1900000, 'BLOCK',  'BULLISH', 'INSTITUTIONAL', 0.72, false, now() - interval '21 minutes'),
  ('NVDA', 'PUT',  870.00, '2025-05-02', 2100000, 'SPLIT',  'BEARISH', 'LARGE',         0.60, false, now() - interval '23 minutes'),
  ('AAPL', 'PUT',  185.00, '2025-05-09', 890000,  'SINGLE', 'BEARISH', 'RETAIL',        0.42, false, now() - interval '25 minutes'),
  ('SPY',  'CALL', 530.00, '2025-05-16', 5500000, 'SWEEP',  'BULLISH', 'WHALE',         0.93, true,  now() - interval '27 minutes'),
  ('AMD',  'CALL', 175.00, '2025-05-09', 1450000, 'BLOCK',  'BULLISH', 'INSTITUTIONAL', 0.76, false, now() - interval '29 minutes'),
  ('COIN', 'CALL', 245.00, '2025-05-16', 3200000, 'SWEEP',  'BULLISH', 'WHALE',         0.87, true,  now() - interval '31 minutes'),
  ('PLTR', 'CALL',  28.00, '2025-06-20', 780000,  'BLOCK',  'BULLISH', 'LARGE',         0.65, false, now() - interval '33 minutes'),
  ('MSTR', 'CALL', 420.00, '2025-05-09', 2900000, 'SWEEP',  'BULLISH', 'WHALE',         0.89, true,  now() - interval '35 minutes'),
  ('QQQ',  'PUT',  440.00, '2025-05-02', 4200000, 'BLOCK',  'BEARISH', 'INSTITUTIONAL', 0.80, false, now() - interval '37 minutes'),
  ('AMZN', 'PUT',  195.00, '2025-05-16', 1600000, 'SPLIT',  'BEARISH', 'LARGE',         0.58, false, now() - interval '39 minutes');
