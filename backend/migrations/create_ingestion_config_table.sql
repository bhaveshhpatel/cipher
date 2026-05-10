-- REARCH-002 (2026-05-09)
-- ingestion_config: key/value store for runtime-adjustable ingestion floors.
--
-- Design rationale:
--   Key/value typed rows (not JSONB, not individual columns) so new floor
--   parameters can be added in future stories as new rows without a migration.
--   value_type CHECK ensures Python reader can cast without hardcoded field names.

CREATE TABLE IF NOT EXISTS ingestion_config (
    key         TEXT        PRIMARY KEY,
    value       TEXT        NOT NULL,
    value_type  TEXT        NOT NULL
                            CHECK (value_type IN ('int', 'float', 'bool', 'str')),
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trigger: keep updated_at current on every row update
CREATE OR REPLACE FUNCTION ingestion_config_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ingestion_config_updated_at ON ingestion_config;
CREATE TRIGGER ingestion_config_updated_at
    BEFORE UPDATE ON ingestion_config
    FOR EACH ROW
    EXECUTE FUNCTION ingestion_config_set_updated_at();

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------

ALTER TABLE ingestion_config ENABLE ROW LEVEL SECURITY;

-- Anyone authenticated can read (admin UI, dashboards)
CREATE POLICY "ingestion_config_read"
    ON ingestion_config FOR SELECT
    USING (true);

-- Only service_role can write (backend + migrations, never browser clients)
CREATE POLICY "ingestion_config_write"
    ON ingestion_config FOR ALL
    USING  (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
