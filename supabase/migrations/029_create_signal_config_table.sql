-- 029_create_signal_config_table.sql  [REARCH-005]
-- Runtime signal configuration store for the Signal Engine (REARCH-006).
--
-- Mirrors the ingestion_config table schema exactly (bigint PK, key/value/
-- value_type/description/updated_at/updated_by) so the admin UI (REARCH-008)
-- and signal_config_store.py can follow the same PostgREST contract as
-- ingestion_config.
--
-- All 11 Steamroom knob rows are seeded in 030_seed_signal_config_steamroom_defaults.sql.
-- Apply both migrations together before deploying signal_config_store.py.
--
-- REARCH-005 dependency note:
--   Reads by:  backend/services/signal_config_store.py (get_param hot path)
--   Written by: PATCH /admin/signal-config (REARCH-008)
--   Backtested by: GET /admin/signal-config/backtest (REARCH-014, config_override deep-merge)
--
-- Streaming boundary: this table has no interaction with the streaming worker,
-- Tradier client, OCC parser, chain cache, or registry sync.

CREATE TABLE IF NOT EXISTS signal_config (
    id          bigint      PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    key         text        NOT NULL,
    value       text        NOT NULL,
    value_type  text        NOT NULL DEFAULT 'float',
    description text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);

-- Unique constraint: one row per key.  Enforces that signal_config_store.py's
-- PATCH ?key=eq.<key> always targets exactly one row.
CREATE UNIQUE INDEX IF NOT EXISTS signal_config_key_unique
    ON signal_config (key);

-- Admin UI (REARCH-008) lists rows ordered by id; this index also supports
-- the startup validate_signal_config() SELECT which reads all keys.
CREATE INDEX IF NOT EXISTS signal_config_id_idx
    ON signal_config (id ASC);

-- Block anon / authenticated roles; service_role bypasses RLS automatically.
-- Mirrors the same RLS stance as ingestion_config and admin_activity_log.
ALTER TABLE signal_config ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE signal_config IS
    'Runtime signal engine configuration (REARCH-005). Stores the 11 WSJ Steamroom '
    'conviction knobs read by signal_config_store.get_param() on every episode '
    'evaluation. Written via PATCH /admin/signal-config (REARCH-008). '
    'Backtested via GET /admin/signal-config/backtest (REARCH-014).';

COMMENT ON COLUMN signal_config.key IS
    'Dotted knob name matching SIGNAL_CONFIG_TYPES registry in signal_config_store.py '
    '(e.g. sig.min_dte, sig.ask_side_pct_floor).';

COMMENT ON COLUMN signal_config.value IS
    'Raw text value; coerced to Python type declared in SIGNAL_CONFIG_TYPES on read.';

COMMENT ON COLUMN signal_config.value_type IS
    'Type hint stored alongside value for UI display and fallback casting. '
    'Canonical type source is SIGNAL_CONFIG_TYPES in signal_config_store.py, '
    'not this column.';

COMMENT ON COLUMN signal_config.updated_by IS
    'Audit trail: set to the admin email or system identifier on every write.';
