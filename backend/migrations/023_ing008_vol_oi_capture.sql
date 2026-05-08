-- Migration 023: ING-008 — Vol/OI capture at flow event and episode level
--
-- Changes:
--   flow_events        : +contract_volume_snapshot (INT nullable)
--                        +contract_oi              (INT nullable)
--   flow_episodes      : +contract_oi_at_open      (INT nullable)
--                        +contract_volume_at_close  (INT nullable)
--                        +volume_oi_ratio           (NUMERIC(8,4) nullable)
--   options_chain_cache: +volume                   (INT NOT NULL DEFAULT 0)
--
-- Vol vs OI is NOT a gate at ingestion time.  These columns are enrichment
-- only — the signal engines (S8/S12) and APEX layer consume them for scoring.
-- A cache miss (chain_store returned NULL for the contract at event time)
-- results in NULL stored in the columns; NULL is never a drop condition.
--
-- Safe to run multiple times (all ADD COLUMN IF NOT EXISTS).
-- Indexes are plain unconditional — no WHERE clause / no function calls.

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. flow_events — snapshot at the moment a flow event is logged
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE public.flow_events
    ADD COLUMN IF NOT EXISTS contract_volume_snapshot INTEGER,
    ADD COLUMN IF NOT EXISTS contract_oi              INTEGER;

COMMENT ON COLUMN public.flow_events.contract_volume_snapshot IS
    'Intraday volume for this specific contract at the time the flow event was logged. '
    'Sourced from the chain_store 5-min background cache. NULL if cache miss at event time.';

COMMENT ON COLUMN public.flow_events.contract_oi IS
    'Open interest for this specific contract at the time the flow event was logged. '
    'Sourced from the chain_store cache. NULL if cache miss at event time.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. flow_episodes — OI captured at open, volume captured at close/persist
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE public.flow_episodes
    ADD COLUMN IF NOT EXISTS contract_oi_at_open       INTEGER,
    ADD COLUMN IF NOT EXISTS contract_volume_at_close  INTEGER,
    ADD COLUMN IF NOT EXISTS volume_oi_ratio           NUMERIC(8,4);

COMMENT ON COLUMN public.flow_episodes.contract_oi_at_open IS
    'Open interest for this contract at the time the episode was first created (INSERT). '
    'NULL if chain_store cache miss at episode open time.';

COMMENT ON COLUMN public.flow_episodes.contract_volume_at_close IS
    'Intraday volume for this contract at the time of the most recent episode PATCH '
    '(i.e. last qualifying print within the merge window). NULL if cache miss.';

COMMENT ON COLUMN public.flow_episodes.volume_oi_ratio IS
    'contract_volume_at_close / contract_oi_at_open, pre-computed at persist time. '
    'NULL when either component is NULL or contract_oi_at_open = 0 (avoid div-by-zero). '
    'Available as a ready-made input to S8/S12 signal engines and APEX scoring.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. options_chain_cache — add intraday volume alongside open_interest
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE public.options_chain_cache
    ADD COLUMN IF NOT EXISTS volume INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN public.options_chain_cache.volume IS
    'Intraday cumulative volume for this contract, populated on each 5-min '
    'chain_store background refresh. Resets with the cache at market open. '
    'Sourced from Tradier GET /markets/options/chains.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Indexes — plain (no WHERE, no function calls)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_flow_episodes_volume_oi_ratio
    ON public.flow_episodes (volume_oi_ratio);

CREATE INDEX IF NOT EXISTS idx_flow_episodes_contract_oi_at_open
    ON public.flow_episodes (contract_oi_at_open);

CREATE INDEX IF NOT EXISTS idx_flow_events_contract_oi
    ON public.flow_events (contract_oi);
