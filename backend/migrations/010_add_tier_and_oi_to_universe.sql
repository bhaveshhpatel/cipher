-- migration 010: add open_interest, average_volume, and tier to options_universe_symbols
-- Part of feature 4A: dynamic tiering

ALTER TABLE options_universe_symbols
  ADD COLUMN IF NOT EXISTS open_interest  INT,
  ADD COLUMN IF NOT EXISTS average_volume INT,
  ADD COLUMN IF NOT EXISTS tier           SMALLINT NOT NULL DEFAULT 3;

COMMENT ON COLUMN options_universe_symbols.open_interest  IS 'OCC-level open interest at time of universe snapshot';
COMMENT ON COLUMN options_universe_symbols.average_volume IS 'Average daily volume for the underlying symbol';
COMMENT ON COLUMN options_universe_symbols.tier           IS 'Dynamic tier assignment: 1=high, 2=mid, 3=standard. Default 3.';

CREATE INDEX IF NOT EXISTS idx_universe_symbols_tier
  ON options_universe_symbols(snapshot_id, tier);
