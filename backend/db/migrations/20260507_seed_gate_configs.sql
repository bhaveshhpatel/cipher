-- =============================================================================
-- Migration: 20260507_seed_gate_configs
-- ING-010 (2026-05-07)
--
-- Seeds gate_configs with initial values that EXACTLY match the current
-- hardcoded fallbacks in:
--   - threshold_reconciliation._TIER_THRESHOLDS
--   - repetition_accumulator._DEFAULT_DTE_PREMIUM_TIERS
--   - tradier_stream (min_premium ingestion floor)
--
-- Seeding at the same values means production behaviour is UNCHANGED on
-- first deploy. The control plane is live from day 1 but neutral.
-- Operators can UPDATE individual rows to tune gates without a code deploy.
--
-- Uses INSERT ... ON CONFLICT DO NOTHING so this file is safe to re-run.
-- To reset to defaults: DELETE FROM gate_configs; then re-run this file.
--
-- Key reference:
--   config_key        description
--   ────────────────  ──────────────────────────────────────────────────────
--   oi_spike_pct      OI increase % to trigger OI_SPIKE breach (threshold_reconciliation)
--   oi_collapse_pct   OI decrease % to trigger OI_COLLAPSE breach (threshold_reconciliation)
--   premium_usd       Single-event premium $ to trigger PREMIUM_FLOOD breach (threshold_reconciliation)
--   volume_ratio      Volume / 20d-avg to trigger VOLUME_SURGE breach (threshold_reconciliation)
--   min_premium       Episode weighted_premium floor for Gate 2 (repetition_accumulator + tradier_stream)
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- threshold_reconciliation thresholds
-- Matches _TIER_THRESHOLDS in backend/services/threshold_reconciliation.py
-- ---------------------------------------------------------------------------

-- oi_spike_pct: T1=10%, T2=20%, T3=35%
INSERT INTO gate_configs (config_key, tier_int, config_value, description) VALUES
    ('oi_spike_pct', 1, 0.10, 'T1: OI increase >= 10% triggers OI_SPIKE breach'),
    ('oi_spike_pct', 2, 0.20, 'T2: OI increase >= 20% triggers OI_SPIKE breach'),
    ('oi_spike_pct', 3, 0.35, 'T3: OI increase >= 35% triggers OI_SPIKE breach')
ON CONFLICT (config_key, tier_int) DO NOTHING;

-- oi_collapse_pct: T1=-15%, T2=-25%, T3=-40%
-- Stored as positive values; threshold_reconciliation negates on comparison.
INSERT INTO gate_configs (config_key, tier_int, config_value, description) VALUES
    ('oi_collapse_pct', 1, 0.15, 'T1: OI decrease >= 15% triggers OI_COLLAPSE breach (sign applied in code)'),
    ('oi_collapse_pct', 2, 0.25, 'T2: OI decrease >= 25% triggers OI_COLLAPSE breach'),
    ('oi_collapse_pct', 3, 0.40, 'T3: OI decrease >= 40% triggers OI_COLLAPSE breach')
ON CONFLICT (config_key, tier_int) DO NOTHING;

-- premium_usd: T1=$250k, T2=$100k, T3=$50k
INSERT INTO gate_configs (config_key, tier_int, config_value, description) VALUES
    ('premium_usd', 1, 250000.00, 'T1: single-event premium >= $250k triggers PREMIUM_FLOOD'),
    ('premium_usd', 2, 100000.00, 'T2: single-event premium >= $100k triggers PREMIUM_FLOOD'),
    ('premium_usd', 3,  50000.00, 'T3: single-event premium >= $50k triggers PREMIUM_FLOOD')
ON CONFLICT (config_key, tier_int) DO NOTHING;

-- volume_ratio: T1=3x, T2=4x, T3=6x
INSERT INTO gate_configs (config_key, tier_int, config_value, description) VALUES
    ('volume_ratio', 1, 3.0, 'T1: volume >= 3x 20d-avg triggers VOLUME_SURGE'),
    ('volume_ratio', 2, 4.0, 'T2: volume >= 4x 20d-avg triggers VOLUME_SURGE'),
    ('volume_ratio', 3, 6.0, 'T3: volume >= 6x 20d-avg triggers VOLUME_SURGE')
ON CONFLICT (config_key, tier_int) DO NOTHING;

-- ---------------------------------------------------------------------------
-- repetition_accumulator Gate 2 min_premium floors
-- Matches _DEFAULT_DTE_PREMIUM_TIERS in backend/signals/repetition_accumulator.py
-- The accumulator uses tier_int from its internal _tier_map (1=T1, 2=T2, 3=T3).
-- Gate_config_store key: "min_premium" — same key used by tradier_stream.
-- Value represents the T1 (strictest) baseline; adjust per-tier as needed.
--
-- NOTE: The accumulator's DTE-tiered table cannot be fully expressed as a
-- single scalar per tier — floors vary by DTE bucket (<=7d, <=30d, <=90d, >90d).
-- This row seeds the T1 strictest floor ($2M, >90 DTE) as the live override
-- baseline. The DTE-bucket table in code remains the fine-grained source;
-- gate_config_store is the coarse override ceiling that can tighten any bucket.
-- Tier 2 and 3 floors mirror the >90d DTE bucket from _DEFAULT_DTE_PREMIUM_TIERS.
-- ---------------------------------------------------------------------------
INSERT INTO gate_configs (config_key, tier_int, config_value, description) VALUES
    ('min_premium', 1, 2000000.00, 'T1: Gate 2 min_premium override baseline — tightens dte_floor when > dte_floor'),
    ('min_premium', 2, 1000000.00, 'T2: Gate 2 min_premium override baseline'),
    ('min_premium', 3, 1000000.00, 'T3: Gate 2 min_premium override baseline')
ON CONFLICT (config_key, tier_int) DO NOTHING;

COMMIT;
