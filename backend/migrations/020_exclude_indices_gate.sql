-- =============================================================================
-- 020_exclude_indices_gate.sql
-- ING-010 Gate 6: exclude_indices
-- =============================================================================
-- Adds seed rows for the `exclude_indices` boolean gate in gate_configs.
--
-- Gate semantics:
--   value = 1.0  → index tickers (SPY, QQQ, IWM, …) are EXCLUDED from flow
--   value = 0.0  → index tickers pass through (included in flow)
--
-- This is a tier-independent gate; only tier=1 is read by the stream at runtime
-- (services/gate_config_store.get("exclude_indices", 1)).  Tier 2 and 3 rows
-- are seeded for completeness so the admin matrix renders correctly.
--
-- Default: 1.0 (filter ON) — index noise is suppressed at cold start.
-- Safe default: the stream will never accidentally let SPY/QQQ flood through
-- on a first deploy or cold-start before an operator toggles the gate.
-- =============================================================================

INSERT INTO gate_configs
    (gate_name, tier, value, min_value, max_value, updated_by, updated_at, previous_value)
VALUES
    ('exclude_indices', 1, 1.0, 0.0, 1.0, 'migration', NOW(), NULL),
    ('exclude_indices', 2, 1.0, 0.0, 1.0, 'migration', NOW(), NULL),
    ('exclude_indices', 3, 1.0, 0.0, 1.0, 'migration', NOW(), NULL)
ON CONFLICT (gate_name, tier) DO UPDATE
    SET value      = EXCLUDED.value,
        min_value  = EXCLUDED.min_value,
        max_value  = EXCLUDED.max_value,
        updated_by = EXCLUDED.updated_by,
        updated_at = EXCLUDED.updated_at;
