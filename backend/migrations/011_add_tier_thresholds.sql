-- migration 011: tier_thresholds table for admin-driven T1/T2/T3 classification rules
-- Part of feature 4A: dynamic tiering
-- Updated 2026-05-17: column DEFAULTs synced to live DB row (id=1, updated 2026-05-04)

CREATE TABLE IF NOT EXISTS tier_thresholds (
  id          SERIAL PRIMARY KEY,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by  TEXT        NOT NULL DEFAULT 'system',
  is_active   BOOLEAN     NOT NULL DEFAULT true,

  -- Tier 1 — liquid large-caps (SPY, AAPL, TSLA, NVDA, etc.)
  t1_min_volume     INT     NOT NULL DEFAULT 20000000,   -- 20M avg daily volume
  t1_min_last_price DECIMAL NOT NULL DEFAULT 10.0,
  t1_min_oi         INT     NOT NULL DEFAULT 1000,       -- open interest at ATM strike
  t1_atm_pct        DECIMAL NOT NULL DEFAULT 0.15,       -- ±15% strike range
  t1_max_dte        INT     NOT NULL DEFAULT 60,

  -- Tier 2 — mid-cap optionable (HOOD, SOFI, RIVN, etc.)
  t2_min_volume     INT     NOT NULL DEFAULT 1000000,
  t2_min_last_price DECIMAL NOT NULL DEFAULT 10.0,
  t2_min_oi         INT     NOT NULL DEFAULT 500,
  t2_atm_pct        DECIMAL NOT NULL DEFAULT 0.20,       -- ±20% strike range
  t2_max_dte        INT     NOT NULL DEFAULT 90,

  -- Tier 3 — standard (everything else that passes universe filter)
  t3_min_volume     INT     NOT NULL DEFAULT 500000,
  t3_min_last_price DECIMAL NOT NULL DEFAULT 1.0,
  t3_min_oi         INT     NOT NULL DEFAULT 100,
  t3_atm_pct        DECIMAL NOT NULL DEFAULT 0.20,       -- ±20% strike range
  t3_max_dte        INT     NOT NULL DEFAULT 90
);

COMMENT ON TABLE tier_thresholds IS
  'Admin-editable thresholds for T1/T2/T3 symbol tier classification. Only the row with is_active=true is used.';

CREATE INDEX IF NOT EXISTS idx_tier_thresholds_active
  ON tier_thresholds(is_active);

-- Trigger: keep updated_at current
CREATE OR REPLACE FUNCTION update_tier_thresholds_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_tier_thresholds_updated_at ON tier_thresholds;
CREATE TRIGGER trg_tier_thresholds_updated_at
  BEFORE UPDATE ON tier_thresholds
  FOR EACH ROW EXECUTE FUNCTION update_tier_thresholds_updated_at();

-- Seed the default active row with explicit values matching the live DB
INSERT INTO tier_thresholds (
  is_active, updated_by,
  t1_min_volume, t1_min_last_price, t1_min_oi, t1_atm_pct, t1_max_dte,
  t2_min_volume, t2_min_last_price, t2_min_oi, t2_atm_pct, t2_max_dte,
  t3_min_volume, t3_min_last_price, t3_min_oi, t3_atm_pct, t3_max_dte
) VALUES (
  true, 'system',
  20000000, 10.0, 1000, 0.15, 60,
  1000000,  10.0, 500,  0.20, 90,
  500000,   1.0,  100,  0.20, 90
)
ON CONFLICT DO NOTHING;
