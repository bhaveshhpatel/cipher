-- Migration 012: options_chain_cache
-- Persists the in-memory OCC symbol registry built by symbol_registry.py
-- so that on a process restart the chain data can be loaded from DB in
-- seconds rather than re-fetching all chains from Tradier (which takes
-- 3-8 minutes for 1,000+ tickers).
--
-- Design:
--   One row per OCC contract per snapshot.  Keyed on (snapshot_id, occ_symbol).
--   ON DELETE CASCADE means pruning old snapshots automatically prunes chains.
--   RLS: service_role INSERT/UPDATE, anon SELECT.
--
-- Written by: chain_store.save_chain()  (services/chain_store.py)
-- Read by:    chain_store.load_chain()  (services/chain_store.py)
--             called from symbol_registry.SymbolRegistry.load_from_db()

CREATE TABLE IF NOT EXISTS public.options_chain_cache (
    id             BIGSERIAL    PRIMARY KEY,
    snapshot_id    UUID         NOT NULL
        REFERENCES public.options_universe_snapshots(id) ON DELETE CASCADE,
    occ_symbol     TEXT         NOT NULL,
    ticker         TEXT         NOT NULL,
    contract_type  TEXT         NOT NULL,   -- CALL | PUT
    strike         NUMERIC(10,2) NOT NULL,
    expiry         TEXT         NOT NULL,   -- YYYY-MM-DD
    dte            INT          NOT NULL,
    open_interest  INT          NOT NULL DEFAULT 0,
    tier           INT          NOT NULL DEFAULT 3,
    built_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_chain_cache_snapshot_symbol
        UNIQUE (snapshot_id, occ_symbol)
);

CREATE INDEX IF NOT EXISTS idx_chain_cache_snapshot
    ON public.options_chain_cache (snapshot_id);

CREATE INDEX IF NOT EXISTS idx_chain_cache_ticker
    ON public.options_chain_cache (ticker);

CREATE INDEX IF NOT EXISTS idx_chain_cache_built_at
    ON public.options_chain_cache (built_at DESC);

-- RLS
ALTER TABLE public.options_chain_cache ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'options_chain_cache' AND policyname = 'anon_select_chain_cache'
  ) THEN
    CREATE POLICY anon_select_chain_cache
        ON public.options_chain_cache FOR SELECT
        TO anon, authenticated USING (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'options_chain_cache' AND policyname = 'service_write_chain_cache'
  ) THEN
    CREATE POLICY service_write_chain_cache
        ON public.options_chain_cache FOR ALL
        TO service_role WITH CHECK (true);
  END IF;
END $$;

COMMENT ON TABLE public.options_chain_cache IS
    'Persisted OCC contract metadata from symbol_registry.SymbolRegistry.build(). '
    'Enables cold-start registry seeding without re-fetching chains from Tradier. '
    'Rows cascade-delete when the parent snapshot is pruned.';
