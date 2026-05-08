-- =============================================================================
-- Migration 019: gate_configs + gate_config_audit tables  [ING-010]
-- =============================================================================
-- gate_configs holds the live per-gate per-tier threshold values.
-- gate_config_audit is an append-only log of every mutation.
--
-- Seed defaults mirror _DEFAULTS in services/gate_config_store.py exactly.
-- Any deviation between this file and that dict is a bug — keep them in sync.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. gate_configs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gate_configs (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    gate_name    text        NOT NULL,
    tier         smallint    NOT NULL CHECK (tier IN (1, 2, 3)),
    value        double precision NOT NULL,
    min_value    double precision NOT NULL DEFAULT 0,
    max_value    double precision NOT NULL,
    updated_by   text        NOT NULL DEFAULT 'system',
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT gate_configs_unique_gate_tier UNIQUE (gate_name, tier)
);

COMMENT ON TABLE  gate_configs                IS 'Live per-gate per-tier threshold values (ING-010)';
COMMENT ON COLUMN gate_configs.gate_name      IS 'One of: min_premium, dte_floor_multiplier, dedup_window_ms, require_oi, signal_debounce_ms, signal_min_premium, exclude_indices';
COMMENT ON COLUMN gate_configs.tier           IS '1=Tier-1 (large-cap), 2=Tier-2 (mid-cap), 3=Tier-3 (small-cap)';
COMMENT ON COLUMN gate_configs.value          IS 'Current live value for this gate × tier combo';
COMMENT ON COLUMN gate_configs.min_value      IS 'Inclusive lower bound used by admin UI slider and PATCH validator';
COMMENT ON COLUMN gate_configs.max_value      IS 'Inclusive upper bound used by admin UI slider and PATCH validator';
COMMENT ON COLUMN gate_configs.updated_by     IS 'Email of the admin who last changed this row';
COMMENT ON COLUMN gate_configs.updated_at     IS 'Wall-clock time of the last update (auto-maintained)';

-- Speed up GateConfigStore.load() — bulk SELECT by gate_name
CREATE INDEX IF NOT EXISTS idx_gate_configs_gate_name
    ON gate_configs (gate_name);

-- Speed up PATCH endpoint — lookup by (gate_name, tier)
CREATE INDEX IF NOT EXISTS idx_gate_configs_gate_tier
    ON gate_configs (gate_name, tier);

-- ---------------------------------------------------------------------------
-- 2. gate_config_audit  (append-only audit log)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gate_config_audit (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    gate_name    text        NOT NULL,
    tier         smallint    NOT NULL CHECK (tier IN (1, 2, 3)),
    old_value    double precision,
    new_value    double precision NOT NULL,
    changed_by   text        NOT NULL,
    reason       text,
    changed_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  gate_config_audit             IS 'Append-only audit trail of gate_configs mutations (ING-010)';
COMMENT ON COLUMN gate_config_audit.old_value   IS 'NULL on first insert (no prior value)';
COMMENT ON COLUMN gate_config_audit.reason      IS 'Optional free-text rationale supplied by the admin';

-- Speed up history endpoint  GET /api/admin/gate-config/history
CREATE INDEX IF NOT EXISTS idx_gate_config_audit_changed_at
    ON gate_config_audit (changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_gate_config_audit_gate_name
    ON gate_config_audit (gate_name);

CREATE INDEX IF NOT EXISTS idx_gate_config_audit_tier
    ON gate_config_audit (tier);

-- ---------------------------------------------------------------------------
-- 3. Row-Level Security
-- ---------------------------------------------------------------------------
ALTER TABLE gate_configs       ENABLE ROW LEVEL SECURITY;
ALTER TABLE gate_config_audit  ENABLE ROW LEVEL SECURITY;

-- anon / authenticated roles have no access — service_role only
CREATE POLICY gate_configs_service_only
    ON gate_configs
    USING (auth.role() = 'service_role');

CREATE POLICY gate_config_audit_service_only
    ON gate_config_audit
    USING (auth.role() = 'service_role');

-- ---------------------------------------------------------------------------
-- 4. Auto-stamp updated_at on gate_configs
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION _set_gate_configs_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_gate_configs_updated_at ON gate_configs;
CREATE TRIGGER trg_gate_configs_updated_at
    BEFORE UPDATE ON gate_configs
    FOR EACH ROW EXECUTE FUNCTION _set_gate_configs_updated_at();

-- ---------------------------------------------------------------------------
-- 5. Seed data — default values for all 7 gates × 3 tiers (21 rows)
--
--    Values MUST match _DEFAULTS in services/gate_config_store.py exactly.
--    Any deviation is a bug.
--
--  Gate                   | T1 default   | T2 default   | T3 default
--  -----------------------+--------------+--------------+--------------
--  min_premium ($)        | 25 000       | 15 000       | 10 000
--  dte_floor_multiplier   | 1.5          | 1.0          | 0.75
--  dedup_window_ms        | 5 000        | 5 000        | 5 000
--  require_oi             | 0.0 (off)    | 0.0 (off)    | 0.0 (off)
--  signal_debounce_ms     | 30 000       | 60 000       | 120 000
--  signal_min_premium ($) | 50 000       | 35 000       | 20 000   [SA-3]
--  exclude_indices        | 1.0 (on)     | 1.0 (on)     | 1.0 (on) [SA-3]
--
--  Note: 'debounce_ms' is a code-layer alias for 'signal_debounce_ms'
--  resolved by GateConfigStore._resolve_alias(). It is NOT a DB gate
--  and must NOT have seed rows here.
-- ---------------------------------------------------------------------------
INSERT INTO gate_configs
    (gate_name,              tier, value,      min_value,   max_value,  updated_by)
VALUES
    -- min_premium  (option premium in $, i.e. fill_price * size * 100)
    ('min_premium',             1,  25000.0,    1000.0,   500000.0, 'migration'),
    ('min_premium',             2,  15000.0,    1000.0,   500000.0, 'migration'),
    ('min_premium',             3,  10000.0,    1000.0,   500000.0, 'migration'),

    -- dte_floor_multiplier  (multiplier on per-tier DTE floor curve)
    -- T1 is TIGHTEST (1.5×) — liquid mega-cap names face a stricter accumulator gate.
    -- T3 is most RELAXED (0.75×) — illiquid names need a lower bar to capture flow.
    ('dte_floor_multiplier',    1,     1.5,       0.1,        5.0, 'migration'),
    ('dte_floor_multiplier',    2,     1.0,       0.1,        5.0, 'migration'),
    ('dte_floor_multiplier',    3,    0.75,       0.1,        5.0, 'migration'),

    -- dedup_window_ms  (duplicate-suppression window in milliseconds)
    ('dedup_window_ms',         1,  5000.0,     500.0,    60000.0, 'migration'),
    ('dedup_window_ms',         2,  5000.0,     500.0,    60000.0, 'migration'),
    ('dedup_window_ms',         3,  5000.0,     500.0,    60000.0, 'migration'),

    -- require_oi  (0.0 = gate OFF, 1.0 = gate ON — all tiers off by default)
    -- Gate is toggled via admin API; default-off preserves S2 stream behaviour.
    ('require_oi',              1,     0.0,       0.0,        1.0, 'migration'),
    ('require_oi',              2,     0.0,       0.0,        1.0, 'migration'),
    ('require_oi',              3,     0.0,       0.0,        1.0, 'migration'),

    -- signal_debounce_ms  (cooldown between signals on the same symbol)
    -- T1 tightest (30s), T3 widest (120s) to reduce noise on illiquid names.
    ('signal_debounce_ms',      1,  30000.0,   1000.0,   600000.0, 'migration'),
    ('signal_debounce_ms',      2,  60000.0,   1000.0,   600000.0, 'migration'),
    ('signal_debounce_ms',      3, 120000.0,   1000.0,   600000.0, 'migration'),

    -- signal_min_premium  (minimum cumulative episode premium before signal fires)
    -- T1 strictest (50k) — only large institutional flow signals on mega-caps.
    -- T3 most relaxed (20k) — captures meaningful but smaller flow on small-caps.
    -- SA-3: rows were missing from original migration; added here.
    ('signal_min_premium',      1,  50000.0,   1000.0,   500000.0, 'migration'),
    ('signal_min_premium',      2,  35000.0,   1000.0,   500000.0, 'migration'),
    ('signal_min_premium',      3,  20000.0,   1000.0,   500000.0, 'migration'),

    -- exclude_indices  (1.0 = filter ON — exclude SPY/QQQ/IWM/etc. from flow)
    -- Default ON for all tiers: index options are high-volume noise, not signals.
    -- Configurable via admin API to allow pass-through mode (0.0) per tier.
    -- SA-3: rows were missing from original migration; added here.
    ('exclude_indices',         1,     1.0,       0.0,        1.0, 'migration'),
    ('exclude_indices',         2,     1.0,       0.0,        1.0, 'migration'),
    ('exclude_indices',         3,     1.0,       0.0,        1.0, 'migration')

ON CONFLICT (gate_name, tier) DO NOTHING;
