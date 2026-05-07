-- ING-010: Tier-aware ingestion gate control plane
-- Migration: 013_gate_configs.sql
-- Depends on: 012_catch_up_schema_delta.sql
--
-- Creates:
--   gate_configs       — live per-tier gate config rows
--   gate_config_audit  — immutable audit trail of every change

-- ============================================================
-- Table: gate_configs
-- ============================================================
CREATE TABLE IF NOT EXISTS gate_configs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gate_name    TEXT        NOT NULL,
    tier         INTEGER     NOT NULL CHECK (tier IN (1, 2, 3)),
    value        NUMERIC     NOT NULL,
    value_type   TEXT        NOT NULL DEFAULT 'numeric',
    description  TEXT,
    is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_by   TEXT        NOT NULL DEFAULT 'system',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_gate_tier UNIQUE (gate_name, tier)
);

-- ============================================================
-- Table: gate_config_audit
-- ============================================================
CREATE TABLE IF NOT EXISTS gate_config_audit (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gate_name       TEXT        NOT NULL,
    tier            INTEGER     NOT NULL,
    old_value       TEXT,
    new_value       TEXT        NOT NULL,
    updated_by      TEXT        NOT NULL,
    reason          TEXT,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    was_market_hours BOOLEAN    NOT NULL DEFAULT FALSE
);

-- ============================================================
-- Indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_gate_configs_lookup
    ON gate_configs (gate_name, tier)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_gate_config_audit_gate
    ON gate_config_audit (gate_name, tier, changed_at DESC);

-- ============================================================
-- Seed rows — 5 gates × 3 tiers = 15 rows
-- ============================================================

-- Gate A: min_premium (parser belowminpremium floor)
INSERT INTO gate_configs (gate_name, tier, value, value_type, description)
VALUES
    ('min_premium', 1, 25000, 'int', 'T1 large-cap parser minimum premium floor ($)'),
    ('min_premium', 2, 15000, 'int', 'T2 mid-cap parser minimum premium floor ($)'),
    ('min_premium', 3, 10000, 'int', 'T3 small-cap parser minimum premium floor ($)')
ON CONFLICT (gate_name, tier) DO NOTHING;

-- Gate B: dte_floor_multiplier (accumulator DTE-adjusted floor scale)
INSERT INTO gate_configs (gate_name, tier, value, value_type, description)
VALUES
    ('dte_floor_multiplier', 1, 1.5,  'float', 'T1 DTE floor multiplier — tightens accumulator floor for large-cap'),
    ('dte_floor_multiplier', 2, 1.0,  'float', 'T2 DTE floor multiplier — baseline'),
    ('dte_floor_multiplier', 3, 0.75, 'float', 'T3 DTE floor multiplier — relaxes accumulator floor for small-cap')
ON CONFLICT (gate_name, tier) DO NOTHING;

-- Gate C: require_oi (per-tier OI gate; 0=off, 1=on)
INSERT INTO gate_configs (gate_name, tier, value, value_type, description)
VALUES
    ('require_oi', 1, 0, 'int', 'T1 require OI gate (0=off, 1=on)'),
    ('require_oi', 2, 0, 'int', 'T2 require OI gate (0=off, 1=on)'),
    ('require_oi', 3, 0, 'int', 'T3 require OI gate (0=off, 1=on)')
ON CONFLICT (gate_name, tier) DO NOTHING;

-- Gate D: debounce_ms (signal debounce window in milliseconds)
INSERT INTO gate_configs (gate_name, tier, value, value_type, description)
VALUES
    ('debounce_ms', 1,  30000, 'int', 'T1 signal debounce window (ms)'),
    ('debounce_ms', 2,  60000, 'int', 'T2 signal debounce window (ms)'),
    ('debounce_ms', 3, 120000, 'int', 'T3 signal debounce window (ms)')
ON CONFLICT (gate_name, tier) DO NOTHING;

-- Gate E: dedup_window_ms (dedup cache window in milliseconds)
INSERT INTO gate_configs (gate_name, tier, value, value_type, description)
VALUES
    ('dedup_window_ms', 1, 5000, 'int', 'T1 dedup cache window (ms)'),
    ('dedup_window_ms', 2, 5000, 'int', 'T2 dedup cache window (ms)'),
    ('dedup_window_ms', 3, 5000, 'int', 'T3 dedup cache window (ms)')
ON CONFLICT (gate_name, tier) DO NOTHING;
