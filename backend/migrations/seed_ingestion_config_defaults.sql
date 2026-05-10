-- REARCH-002 (2026-05-09)
-- Seed default ingestion floor values into ingestion_config.
-- Uses INSERT ... ON CONFLICT DO NOTHING so re-running is safe.

INSERT INTO ingestion_config (key, value, value_type, description) VALUES
    ('ing.min_dte',         '1',     'int',  '0DTE hard floor — events with DTE < 1 are always dropped'),
    ('ing.max_dte',         '90',    'int',  'Maximum DTE ceiling — events beyond 90 DTE are dropped'),
    ('ing.min_premium.t1',  '25000', 'int',  'T1 (INSTITUTIONAL) tier minimum premium in dollars'),
    ('ing.min_premium.t2',  '15000', 'int',  'T2 (LARGE) tier minimum premium in dollars'),
    ('ing.min_premium.t3',  '5000',  'int',  'T3 (RETAIL) tier minimum premium in dollars'),
    ('ing.min_oi',          '50',    'int',  'Minimum open interest per contract (sourced from options_chain_cache)'),
    ('ing.require_ask_tag', 'true',  'bool', 'Tag is_aggressive on events — does NOT gate; consumed by signal engines (REARCH-006)')
ON CONFLICT (key) DO NOTHING;
