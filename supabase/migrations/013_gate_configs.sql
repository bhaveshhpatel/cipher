-- ING-010: Tier-aware configurable ingestion gate system
-- Migration: 013_gate_configs.sql

CREATE TABLE IF NOT EXISTS gate_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gate_name       TEXT NOT NULL,
    tier            INTEGER NOT NULL,
    value           NUMERIC NOT NULL,
    value_type      TEXT NOT NULL,
    description     TEXT,
    updated_by      TEXT NOT NULL DEFAULT 'migration',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    previous_value  NUMERIC,
    reason          TEXT,
    CONSTRAINT uq_gate_configs_name_tier UNIQUE (gate_name, tier)
);

CREATE INDEX IF NOT EXISTS idx_gate_configs_lookup
    ON gate_configs (gate_name, tier);

-- Gate A: min_premium
INSERT INTO gate_configs (gate_name, tier, value, value_type, description, updated_by) VALUES
    ('min_premium', 1, 25000, 'currency',     'T1 mega-cap minimum event premium ($)', 'migration'),
    ('min_premium', 2, 15000, 'currency',     'T2 mid-cap minimum event premium ($)', 'migration'),
    ('min_premium', 3, 10000, 'currency',     'T3 small-cap minimum event premium ($)', 'migration')
ON CONFLICT (gate_name, tier) DO NOTHING;

-- Gate B: dte_floor_multiplier
INSERT INTO gate_configs (gate_name, tier, value, value_type, description, updated_by) VALUES
    ('dte_floor_multiplier', 1, 1.5,  'multiplier', 'T1 DTE floor scale factor', 'migration'),
    ('dte_floor_multiplier', 2, 1.0,  'multiplier', 'T2 DTE floor scale factor (baseline)', 'migration'),
    ('dte_floor_multiplier', 3, 0.75, 'multiplier', 'T3 DTE floor scale factor (relaxed)', 'migration')
ON CONFLICT (gate_name, tier) DO NOTHING;

-- Gate C: require_oi
INSERT INTO gate_configs (gate_name, tier, value, value_type, description, updated_by) VALUES
    ('require_oi', 1, 0, 'boolean', 'T1 require open interest > 0', 'migration'),
    ('require_oi', 2, 0, 'boolean', 'T2 require open interest > 0', 'migration'),
    ('require_oi', 3, 0, 'boolean', 'T3 require open interest > 0', 'migration')
ON CONFLICT (gate_name, tier) DO NOTHING;

-- Gate D: signal_debounce_ms
INSERT INTO gate_configs (gate_name, tier, value, value_type, description, updated_by) VALUES
    ('signal_debounce_ms', 1, 30000,  'milliseconds', 'T1 signal debounce window (30s)', 'migration'),
    ('signal_debounce_ms', 2, 60000,  'milliseconds', 'T2 signal debounce window (60s)', 'migration'),
    ('signal_debounce_ms', 3, 120000, 'milliseconds', 'T3 signal debounce window (120s)', 'migration')
ON CONFLICT (gate_name, tier) DO NOTHING;

-- Gate E: dedup_window_ms (SEEDED; Python wiring deferred to follow-on story)
INSERT INTO gate_configs (gate_name, tier, value, value_type, description, updated_by) VALUES
    ('dedup_window_ms', 1, 5000, 'milliseconds', 'T1 dedup window ms - wiring deferred', 'migration'),
    ('dedup_window_ms', 2, 5000, 'milliseconds', 'T2 dedup window ms - wiring deferred', 'migration'),
    ('dedup_window_ms', 3, 5000, 'milliseconds', 'T3 dedup window ms - wiring deferred', 'migration')
ON CONFLICT (gate_name, tier) DO NOTHING;
