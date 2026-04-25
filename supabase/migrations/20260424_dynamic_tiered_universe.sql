-- C-020: Dynamic Tiered Universe
-- Adds:
--   1. `tier` column to options_universe_symbols (1=Elite, 2=Active, 3=Eligible)
--   2. `avg_oi` column: average open interest across all monitored contracts for a symbol
--   3. `ingestion_config` rows for the 6 new tier knobs
--   4. `ingestion_config` rows for OI enrichment knobs
--   5. Removes UNIVERSE_PRIORITY_SYMBOLS concept (replaced by dynamic T1)

-- ---------------------------------------------------------------------------
-- options_universe_symbols: add tier + avg_oi
-- ---------------------------------------------------------------------------
ALTER TABLE options_universe_symbols
  ADD COLUMN IF NOT EXISTS tier    SMALLINT NOT NULL DEFAULT 3,
  ADD COLUMN IF NOT EXISTS avg_oi  INTEGER  DEFAULT NULL;

COMMENT ON COLUMN options_universe_symbols.tier IS
  '1=Elite (avg_volume>=20M), 2=Active (avg_volume>=2M, price>=10), 3=Eligible (>=500K, >=1)';
COMMENT ON COLUMN options_universe_symbols.avg_oi IS
  'Average open interest across all ATM contracts fetched during registry build. NULL until first registry build completes.';

CREATE INDEX IF NOT EXISTS idx_universe_symbols_tier
  ON options_universe_symbols(snapshot_id, tier);

CREATE INDEX IF NOT EXISTS idx_universe_symbols_stream_tier
  ON options_universe_symbols(snapshot_id, stream_eligible, tier);

-- ---------------------------------------------------------------------------
-- ingestion_config: add tier knobs
-- ---------------------------------------------------------------------------
INSERT INTO ingestion_config (key, value, value_type, description)
VALUES
  -- Tier 1 (Elite) filter knobs
  ('REGISTRY_T1_ATM_PCT',  '0.20', 'float', 'ATM range ±% for Tier 1 (Elite) symbols'),
  ('REGISTRY_T1_MAX_DTE',  '90',   'int',   'Max DTE for Tier 1 (Elite) symbols'),
  -- Tier 2 (Active) filter knobs
  ('REGISTRY_T2_ATM_PCT',  '0.15', 'float', 'ATM range ±% for Tier 2 (Active) symbols'),
  ('REGISTRY_T2_MAX_DTE',  '60',   'int',   'Max DTE for Tier 2 (Active) symbols'),
  -- Tier 3 (Eligible) filter knobs
  ('REGISTRY_T3_ATM_PCT',  '0.10', 'float', 'ATM range ±% for Tier 3 (Eligible) symbols'),
  ('REGISTRY_T3_MAX_DTE',  '30',   'int',   'Max DTE for Tier 3 (Eligible) symbols'),
  -- Volume thresholds for tier assignment
  ('TIER1_MIN_VOLUME',     '20000000', 'int', 'Minimum avg_volume for Tier 1 (Elite) assignment'),
  ('TIER2_MIN_VOLUME',     '2000000',  'int', 'Minimum avg_volume for Tier 2 (Active) assignment'),
  ('TIER2_MIN_PRICE',      '10.0',     'float', 'Minimum last_price for Tier 2 (Active) assignment'),
  -- OI enrichment knobs
  ('OI_REFRESH_MINS',      '30',   'int', 'How often to refresh OI data per symbol (minutes)'),
  ('OI_MIN_THRESHOLD',     '0',    'int', 'Minimum OI to include a contract in streaming (0 = all)')
ON CONFLICT (key) DO NOTHING;

-- Stream-aware worker startup stagger (Fix 1)
INSERT INTO ingestion_config (key, value, value_type, description)
VALUES
  ('STREAM_WORKER_STAGGER_MS', '200', 'int', 'Milliseconds to stagger between stream worker startups (0 = no stagger)')
ON CONFLICT (key) DO NOTHING;

-- Session token semaphore (Fix 2)
INSERT INTO ingestion_config (key, value, value_type, description)
VALUES
  ('STREAM_SESSION_SEM', '3', 'int', 'Max concurrent Tradier session token fetches')
ON CONFLICT (key) DO NOTHING;
