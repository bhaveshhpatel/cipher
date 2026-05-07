-- =============================================================================
-- Migration: add_gate_configs
-- Issue:     #84 — Tiered Gate Control Plane (ING-010)
-- =============================================================================
-- Creates two tables:
--   gate_configs       — hot-reloadable per-tier gate thresholds
--   gate_config_audit  — append-only audit log of every threshold change
--
-- Default seed values mirror _DEFAULTS in services/gate_config_store.py.
-- Any deviation between this file and that dict is a bug — keep them in sync.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. gate_configs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gate_configs (
    id           BIGSERIAL     PRIMARY KEY,

    -- Identifies which gate+tier combination this row controls.
    gate_name    TEXT          NOT NULL,
    tier         SMALLINT      NOT NULL CHECK (tier IN (1, 2, 3)),

    -- Live threshold value read by GateConfigStore.get().
    value        DOUBLE PRECISION NOT NULL,

    -- Bounds enforced by GateConfigStore.update() and the admin PATCH endpoint.
    min_value    DOUBLE PRECISION NOT NULL,
    max_value    DOUBLE PRECISION NOT NULL,

    -- Audit columns written by GateConfigStore.update().
    previous_value DOUBLE PRECISION,
    updated_by   TEXT          NOT NULL DEFAULT 'system',
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT gate_configs_unique_gate_tier UNIQUE (gate_name, tier)
);

COMMENT ON TABLE  gate_configs IS 'Hot-reloadable per-tier ingestion gate thresholds (ING-010 / issue #84).';
COMMENT ON COLUMN gate_configs.gate_name        IS 'Canonical gate name. Allowed: min_premium, dte_floor_multiplier, dedup_window_ms, require_oi, signal_debounce_ms.';
COMMENT ON COLUMN gate_configs.tier             IS 'Symbol tier: 1=high-cap, 2=mid-cap, 3=small-cap.';
COMMENT ON COLUMN gate_configs.value            IS 'Current threshold value read by the ingestion pipeline.';
COMMENT ON COLUMN gate_configs.min_value        IS 'Hard lower bound enforced on admin PATCH.';
COMMENT ON COLUMN gate_configs.max_value        IS 'Hard upper bound enforced on admin PATCH.';
COMMENT ON COLUMN gate_configs.previous_value   IS 'Value before the last update — written by GateConfigStore.update().';
COMMENT ON COLUMN gate_configs.updated_by       IS 'User/system identifier that last modified this row.';
COMMENT ON COLUMN gate_configs.updated_at       IS 'UTC timestamp of the last modification.';

-- ---------------------------------------------------------------------------
-- 2. gate_config_audit
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gate_config_audit (
    id           BIGSERIAL     PRIMARY KEY,
    gate_name    TEXT          NOT NULL,
    tier         SMALLINT      NOT NULL,
    old_value    DOUBLE PRECISION NOT NULL,
    new_value    DOUBLE PRECISION NOT NULL,
    changed_by   TEXT          NOT NULL DEFAULT 'system',
    reason       TEXT,
    changed_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS gate_config_audit_gate_tier_idx
    ON gate_config_audit (gate_name, tier, changed_at DESC);

COMMENT ON TABLE gate_config_audit IS 'Append-only audit log of every gate_configs threshold change (ING-010).';

-- ---------------------------------------------------------------------------
-- 3. RLS — service-role bypass, anon role blocked
-- ---------------------------------------------------------------------------

ALTER TABLE gate_configs       ENABLE ROW LEVEL SECURITY;
ALTER TABLE gate_config_audit  ENABLE ROW LEVEL SECURITY;

-- Allow the Supabase service role (backend) full access.
CREATE POLICY gate_configs_service_all
    ON gate_configs
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY gate_config_audit_service_all
    ON gate_config_audit
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Explicitly deny anon / authenticated roles (no accidental public exposure).
CREATE POLICY gate_configs_deny_anon
    ON gate_configs
    FOR ALL
    TO anon, authenticated
    USING (false);

CREATE POLICY gate_config_audit_deny_anon
    ON gate_config_audit
    FOR ALL
    TO anon, authenticated
    USING (false);

-- ---------------------------------------------------------------------------
-- 4. Seed defaults
--    Mirrors _DEFAULTS + _BOUNDS in services/gate_config_store.py exactly.
--    ON CONFLICT DO NOTHING so re-running this migration is idempotent.
-- ---------------------------------------------------------------------------

INSERT INTO gate_configs (gate_name, tier, value, min_value, max_value, updated_by) VALUES
    -- min_premium  (currency, $)
    ('min_premium', 1, 25000.0,  1000.0, 500000.0, 'migration'),
    ('min_premium', 2, 15000.0,  1000.0, 500000.0, 'migration'),
    ('min_premium', 3, 10000.0,  1000.0, 500000.0, 'migration'),

    -- dte_floor_multiplier  (multiplier, ×)
    ('dte_floor_multiplier', 1, 1.5,  0.1, 5.0, 'migration'),
    ('dte_floor_multiplier', 2, 1.0,  0.1, 5.0, 'migration'),
    ('dte_floor_multiplier', 3, 0.75, 0.1, 5.0, 'migration'),

    -- dedup_window_ms  (milliseconds)
    ('dedup_window_ms', 1, 5000.0, 500.0, 60000.0, 'migration'),
    ('dedup_window_ms', 2, 5000.0, 500.0, 60000.0, 'migration'),
    ('dedup_window_ms', 3, 5000.0, 500.0, 60000.0, 'migration'),

    -- require_oi  (boolean as 0/1)
    ('require_oi', 1, 0.0, 0.0, 1.0, 'migration'),
    ('require_oi', 2, 0.0, 0.0, 1.0, 'migration'),
    ('require_oi', 3, 0.0, 0.0, 1.0, 'migration'),

    -- signal_debounce_ms  (milliseconds)
    ('signal_debounce_ms', 1,  30000.0, 1000.0, 600000.0, 'migration'),
    ('signal_debounce_ms', 2,  60000.0, 1000.0, 600000.0, 'migration'),
    ('signal_debounce_ms', 3, 120000.0, 1000.0, 600000.0, 'migration')

ON CONFLICT (gate_name, tier) DO NOTHING;
