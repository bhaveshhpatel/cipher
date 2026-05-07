-- =============================================================================
-- Migration 019: gate_configs + gate_config_audit tables  [ING-010]
-- =============================================================================
-- gate_configs holds the live per-gate per-tier threshold values.
-- gate_config_audit is an append-only log of every mutation.
--
-- After applying this migration run the seed INSERT below to populate
-- default values for all 6 gates × 3 tiers (18 rows).
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
COMMENT ON COLUMN gate_configs.gate_name      IS 'One of: min_premium, dte_floor_multiplier, dedup_window_ms, debounce_ms, require_oi, signal_debounce_ms';
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
-- 5. Seed data — default values for all 6 gates × 3 tiers (18 rows)
--
--  Gate                   | T1 default   | T2 default   | T3 default
--  -----------------------+--------------+--------------+--------------
--  min_premium            | 25 000       | 15 000       | 5 000
--  dte_floor_multiplier   | 0.5          | 0.75         | 1.0
--  dedup_window_ms        | 5 000        | 5 000        | 5 000
--  debounce_ms            | 2 000        | 2 000        | 2 000
--  require_oi             | 1.0 (true)   | 1.0          | 0.0 (false)
--  signal_debounce_ms     | 30 000       | 30 000       | 60 000
-- ---------------------------------------------------------------------------
INSERT INTO gate_configs
    (gate_name,              tier, value,      min_value,   max_value,  updated_by)
VALUES
    -- min_premium  ($/contract × 100, i.e. premium_paid * 100)
    ('min_premium',             1,  25000.0,    1000.0,   500000.0, 'migration'),
    ('min_premium',             2,  15000.0,    1000.0,   500000.0, 'migration'),
    ('min_premium',             3,   5000.0,    1000.0,   500000.0, 'migration'),

    -- dte_floor_multiplier  (multiplier applied to per-tier DTE floor)
    ('dte_floor_multiplier',    1,     0.5,       0.1,        5.0, 'migration'),
    ('dte_floor_multiplier',    2,    0.75,       0.1,        5.0, 'migration'),
    ('dte_floor_multiplier',    3,     1.0,       0.1,        5.0, 'migration'),

    -- dedup_window_ms  (duplicate-suppression window in milliseconds)
    ('dedup_window_ms',         1,  5000.0,     500.0,    60000.0, 'migration'),
    ('dedup_window_ms',         2,  5000.0,     500.0,    60000.0, 'migration'),
    ('dedup_window_ms',         3,  5000.0,     500.0,    60000.0, 'migration'),

    -- debounce_ms  (per-symbol fire-rate limiter in milliseconds)
    ('debounce_ms',             1,  2000.0,     500.0,    60000.0, 'migration'),
    ('debounce_ms',             2,  2000.0,     500.0,    60000.0, 'migration'),
    ('debounce_ms',             3,  2000.0,     500.0,    60000.0, 'migration'),

    -- require_oi  (0.0 = disabled, 1.0 = enabled — treated as boolean)
    ('require_oi',              1,     1.0,       0.0,        1.0, 'migration'),
    ('require_oi',              2,     1.0,       0.0,        1.0, 'migration'),
    ('require_oi',              3,     0.0,       0.0,        1.0, 'migration'),

    -- signal_debounce_ms  (cooldown between signals on the same symbol)
    ('signal_debounce_ms',      1,  30000.0,   1000.0,   600000.0, 'migration'),
    ('signal_debounce_ms',      2,  30000.0,   1000.0,   600000.0, 'migration'),
    ('signal_debounce_ms',      3,  60000.0,   1000.0,   600000.0, 'migration')
ON CONFLICT (gate_name, tier) DO NOTHING;
