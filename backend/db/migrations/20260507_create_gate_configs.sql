-- =============================================================================
-- Migration: 20260507_create_gate_configs
-- ING-010 (2026-05-07)
--
-- Creates the gate_configs table that backs gate_config_store.load().
-- This table is the single source of truth for all runtime-configurable
-- gate thresholds consumed by:
--   - tradier_stream._resolve_min_premium()         (ingestion floor)
--   - threshold_reconciliation._get_tier_thresholds()  (APEX-S2 breach)
--   - repetition_accumulator._get_episode_min_premium() (Gate 2 DTE floor)
--
-- Schema design notes:
--   PK is (config_key, tier_int) — one row per key/tier combination.
--   tier_int: 1 = T1 (strictest), 2 = T2, 3 = T3 (most permissive).
--   config_value is NUMERIC(20,6) to handle both fractional pct values
--   (e.g. 0.10 for 10%) and large dollar amounts (e.g. 2000000.00).
--   updated_at is auto-set by trigger on every UPDATE for audit trail.
--
-- Idempotent: wrapped in DO $$ block; safe to re-run.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gate_configs (
    config_key    TEXT        NOT NULL,
    tier_int      INT         NOT NULL,
    config_value  NUMERIC(20,6) NOT NULL,
    description   TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT gate_configs_pkey PRIMARY KEY (config_key, tier_int),
    CONSTRAINT gate_configs_tier_range CHECK (tier_int BETWEEN 1 AND 3),
    CONSTRAINT gate_configs_value_positive CHECK (config_value >= 0)
);

COMMENT ON TABLE gate_configs IS
    'Runtime-configurable gate thresholds for the Cipher ingestion + APEX-S2 pipeline. '
    'Consumed by gate_config_store.load() on service start and hot-reload. '
    'ING-010 (2026-05-07).';

COMMENT ON COLUMN gate_configs.config_key IS
    'Threshold key: oi_spike_pct | oi_collapse_pct | premium_usd | volume_ratio | min_premium';
COMMENT ON COLUMN gate_configs.tier_int IS
    '1=T1 (strictest / highest-premium symbols), 2=T2, 3=T3 (most permissive)';
COMMENT ON COLUMN gate_configs.config_value IS
    'Threshold value. Fractional keys (oi_*_pct, volume_ratio) are stored as '
    'decimals (e.g. 0.10 = 10%). Dollar keys (premium_usd, min_premium) are '
    'stored as full USD amounts (e.g. 250000.00).';

-- ---------------------------------------------------------------------------
-- Index for gate_config_store.get(key, tier) point lookups
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_gate_configs_key
    ON gate_configs (config_key);

-- ---------------------------------------------------------------------------
-- Auto-update trigger for updated_at
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_gate_configs_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS gate_configs_updated_at ON gate_configs;
CREATE TRIGGER gate_configs_updated_at
    BEFORE UPDATE ON gate_configs
    FOR EACH ROW EXECUTE FUNCTION trg_gate_configs_updated_at();

COMMIT;
