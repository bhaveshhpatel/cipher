-- =============================================================================
-- Migration 025: REARCH-003 quality tag columns on flow_events
-- applied: YES  (confirmed 2026-05-11 via Supabase cipher-database / kpajucxqlrteckfuafvq)
-- normalized_oi column verified present: NUMERIC DEFAULT NULL ✓
-- =============================================================================
-- Adds normalized_oi, the one REARCH-003 quality tag column not yet present
-- in the schema. All other columns in this set were added by prior migrations:
--
--   bid_ask_class       TEXT    NOT NULL DEFAULT 'MID'   (migration 018)
--   is_ask_side         BOOLEAN          DEFAULT NULL    (migration 023 / pre-existing)
--   vol_oi_signal       BOOLEAN          DEFAULT NULL    (pre-existing)
--   normalized_premium  NUMERIC          DEFAULT NULL    (pre-existing)
--   normalized_oi       NUMERIC          DEFAULT NULL    <-- THIS MIGRATION
--
-- Blocker resolution summary (all four SA blockers cleared):
--   SA-1: event_cipher_score NUMERIC(6,4) vs SMALLINT
--         → Column dropped in migration 024 (REARCH-010). No longer exists.
--   SA-3: bid_ask_class values ('ask_side', 'above_ask') vs Python enum
--         → classify_bid_ask() in flow_store.py now returns 'ASK'/'BID'/'MID'.
--   SA-4: vol_oi_signal stored as Python bool in TEXT column
--         → Column is BOOLEAN DEFAULT NULL in DB (correct). Python type is
--           Optional[bool] in compute_vol_oi_signal(). Both sides aligned.
--   SA-5: enrich_tags() never called from persist_flow_event()
--         → Abstraction dissolved. All five tag fields computed and written
--           inline in persist_flow_event() as of REARCH-003 (flow_store.py).
--
-- normalized_oi semantics:
--   Numerator  : open_interest field from the Tradier tick-level event dict.
--   Denominator: contract_oi captured from chain_store intraday snapshot at
--                persist time.
--   INTENTIONALLY DIFFERENT SOURCES. The ratio expresses what fraction of
--   the cached intraday OI was represented by the Tradier tick-level OI value
--   at the time the tick was processed. It is NOT expected to be ~1.0.
--   NULL when contract_oi (denominator) is unavailable or zero (cache miss).
--   Rounded to 4dp by _compute_vol_oi_ratio() in flow_store.py.
--   Existing rows remain NULL — enrichment only, not a gate field.
-- =============================================================================

-- Add normalized_oi column (idempotent: IF NOT EXISTS)
ALTER TABLE public.flow_events
  ADD COLUMN IF NOT EXISTS normalized_oi NUMERIC DEFAULT NULL;

COMMENT ON COLUMN public.flow_events.normalized_oi IS
  'REARCH-003: tick-level open_interest (Tradier event dict) divided by '
  'contract_oi (chain_store intraday snapshot at persist time). '
  'Intentionally different sources — ratio shows fraction of intraday OI '
  'represented at tick time. NULL on cache miss. Rounded to 4dp. '
  'Computed by _compute_vol_oi_ratio() in flow_store.py.';
