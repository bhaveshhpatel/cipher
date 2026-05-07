-- =============================================================================
-- ING-010: Gate Configs + Audit Trail
-- Applied: 2026-05-07 via Supabase MCP (migration name: ing010_gate_configs_and_audit)
--
-- Creates:
--   public.gate_configs       -- per-tier (T1/T2/T3) threshold config for all 5 gates
--   public.gate_config_audit  -- immutable audit log of every config change
--
-- Seeds 15 default rows (5 gates x 3 tiers) from the issue #84 config matrix.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. gate_configs
-- ---------------------------------------------------------------------------
CREATE TABLE public.gate_configs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    gate_name       TEXT        NOT NULL,
    tier            INTEGER     NOT NULL CHECK (tier IN (1, 2, 3)),
    value           NUMERIC     NOT NULL,
    value_type      TEXT        NOT NULL CHECK (value_type IN ('currency', 'multiplier', 'milliseconds', 'boolean')),
    description     TEXT,
    -- bounds — enforced in application layer; stored here so the admin UI
    -- can render sliders without a separate API call (per QA deliberation)
    min_value       NUMERIC     NOT NULL,
    max_value       NUMERIC     NOT NULL,
    updated_by      TEXT        NOT NULL DEFAULT 'system',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    previous_value  NUMERIC,
    CONSTRAINT gate_configs_unique_gate_tier UNIQUE (gate_name, tier),
    CONSTRAINT gate_configs_value_in_bounds  CHECK (value >= min_value AND value <= max_value),
    CONSTRAINT gate_configs_bounds_ordering  CHECK (min_value < max_value)
);

COMMENT ON TABLE  public.gate_configs                IS 'Per-tier ingestion gate thresholds — hot-reloadable at runtime via GateConfigStore.';
COMMENT ON COLUMN public.gate_configs.gate_name      IS 'Logical gate identifier: min_premium | dte_floor_multiplier | dedup_window_ms | require_oi | signal_debounce_ms';
COMMENT ON COLUMN public.gate_configs.tier           IS '1=T1 mega-cap, 2=T2 mid-cap, 3=T3 small-cap/illiquid';
COMMENT ON COLUMN public.gate_configs.value          IS 'Current threshold. For boolean gates use 1.0 (on) / 0.0 (off).';
COMMENT ON COLUMN public.gate_configs.value_type     IS 'currency=$, multiplier=ratio, milliseconds=ms, boolean=0/1';
COMMENT ON COLUMN public.gate_configs.min_value      IS 'Hard lower bound enforced by GateConfigStore.update() and returned in GET response.';
COMMENT ON COLUMN public.gate_configs.max_value      IS 'Hard upper bound enforced by GateConfigStore.update() and returned in GET response.';
COMMENT ON COLUMN public.gate_configs.previous_value IS 'Value before the most recent update — denormalised convenience copy.';

-- ---------------------------------------------------------------------------
-- 2. gate_config_audit  (append-only — no UPDATE/DELETE via RLS)
-- ---------------------------------------------------------------------------
CREATE TABLE public.gate_config_audit (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    gate_name     TEXT        NOT NULL,
    tier          INTEGER     NOT NULL CHECK (tier IN (1, 2, 3)),
    old_value     NUMERIC,
    new_value     NUMERIC     NOT NULL,
    changed_by    TEXT        NOT NULL,
    reason        TEXT,
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  public.gate_config_audit             IS 'Immutable audit log of every gate config change (who, what, when, old→new).';
COMMENT ON COLUMN public.gate_config_audit.reason      IS 'Optional human-readable rationale supplied in the PATCH request body.';
COMMENT ON COLUMN public.gate_config_audit.changed_by  IS 'Admin email or system identifier that triggered the change.';

-- ---------------------------------------------------------------------------
-- 3. Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX idx_gate_configs_gate_tier
    ON public.gate_configs (gate_name, tier);

CREATE INDEX idx_gate_config_audit_gate_tier
    ON public.gate_config_audit (gate_name, tier);

CREATE INDEX idx_gate_config_audit_changed_at
    ON public.gate_config_audit (changed_at DESC);

CREATE INDEX idx_gate_config_audit_changed_by
    ON public.gate_config_audit (changed_by);

-- ---------------------------------------------------------------------------
-- 4. RLS
-- ---------------------------------------------------------------------------
ALTER TABLE public.gate_configs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.gate_config_audit ENABLE ROW LEVEL SECURITY;

-- gate_configs: service-role reads+writes; authenticated read-only
CREATE POLICY "gate_configs_service_all"
    ON public.gate_configs
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY "gate_configs_authenticated_read"
    ON public.gate_configs
    FOR SELECT
    TO authenticated
    USING (true);

-- gate_config_audit: service-role insert+select; authenticated read-only; no deletes ever
CREATE POLICY "gate_config_audit_service_insert"
    ON public.gate_config_audit
    FOR INSERT
    TO service_role
    WITH CHECK (true);

CREATE POLICY "gate_config_audit_service_select"
    ON public.gate_config_audit
    FOR SELECT
    TO service_role
    USING (true);

CREATE POLICY "gate_config_audit_authenticated_read"
    ON public.gate_config_audit
    FOR SELECT
    TO authenticated
    USING (true);

-- ---------------------------------------------------------------------------
-- 5. Seed data — default config matrix from issue #84
--
--    Gate                | T1        | T2        | T3
--    min_premium ($)     | 25,000    | 15,000    | 10,000
--    dte_floor_mult      | 1.5x      | 1.0x      | 0.75x
--    dedup_window_ms     | 5,000     | 5,000     | 5,000
--    require_oi          | 0 (off)   | 0 (off)   | 0 (off)
--    signal_debounce_ms  | 30,000    | 60,000    | 120,000
-- ---------------------------------------------------------------------------
INSERT INTO public.gate_configs
    (gate_name, tier, value, value_type, min_value, max_value, description, updated_by)
VALUES
    -- min_premium
    ('min_premium', 1, 25000,  'currency',     1000,  500000,
     'Minimum option premium ($) for T1 mega-cap symbols to pass the belowminpremium gate',    'system'),
    ('min_premium', 2, 15000,  'currency',     1000,  500000,
     'Minimum option premium ($) for T2 mid-cap symbols to pass the belowminpremium gate',     'system'),
    ('min_premium', 3, 10000,  'currency',     1000,  500000,
     'Minimum option premium ($) for T3 small-cap symbols to pass the belowminpremium gate',   'system'),

    -- dte_floor_multiplier
    ('dte_floor_multiplier', 1, 1.5,  'multiplier',  0.1,  5.0,
     'DTE floor curve multiplier for T1 — tightens the accumulator gate on liquid names',    'system'),
    ('dte_floor_multiplier', 2, 1.0,  'multiplier',  0.1,  5.0,
     'DTE floor curve multiplier for T2 — neutral baseline',                                  'system'),
    ('dte_floor_multiplier', 3, 0.75, 'multiplier',  0.1,  5.0,
     'DTE floor curve multiplier for T3 — relaxed to capture illiquid flow',                  'system'),

    -- dedup_window_ms
    ('dedup_window_ms', 1, 5000,  'milliseconds', 500,  60000,
     'Deduplication window (ms) for T1 — 5 s to absorb rapid repeat prints',  'system'),
    ('dedup_window_ms', 2, 5000,  'milliseconds', 500,  60000,
     'Deduplication window (ms) for T2 — 5 s baseline',                       'system'),
    ('dedup_window_ms', 3, 5000,  'milliseconds', 500,  60000,
     'Deduplication window (ms) for T3 — 5 s baseline',                       'system'),

    -- require_oi (boolean: 0.0=off, 1.0=on)
    ('require_oi', 1, 0.0, 'boolean', 0.0, 1.0,
     'Require non-zero open interest for T1 contracts (0=off, 1=on)',  'system'),
    ('require_oi', 2, 0.0, 'boolean', 0.0, 1.0,
     'Require non-zero open interest for T2 contracts (0=off, 1=on)',  'system'),
    ('require_oi', 3, 0.0, 'boolean', 0.0, 1.0,
     'Require non-zero open interest for T3 contracts (0=off, 1=on)',  'system'),

    -- signal_debounce_ms
    ('signal_debounce_ms', 1,  30000, 'milliseconds', 1000, 600000,
     'Signal debounce window (ms) for T1 — 30 s, tighter on liquid names',   'system'),
    ('signal_debounce_ms', 2,  60000, 'milliseconds', 1000, 600000,
     'Signal debounce window (ms) for T2 — 60 s baseline',                   'system'),
    ('signal_debounce_ms', 3, 120000, 'milliseconds', 1000, 600000,
     'Signal debounce window (ms) for T3 — 120 s, wider on illiquid names',  'system');
