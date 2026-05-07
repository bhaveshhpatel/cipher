-- =============================================================================
-- Migration 021: seed exclude_indices Gate 6 rows in gate_configs
-- ING-011 / PR: ing/s10-tiered-gate-control-plane
-- =============================================================================
-- Inserts the three tier rows for the new exclude_indices gate.
-- Default value 1.0 = filter ON (index ETF options suppressed).
-- Tier-independent gate: only tier=1 is read at runtime by tradier_stream;
-- tiers 2 and 3 are seeded for schema completeness.
-- ON CONFLICT DO NOTHING makes this idempotent.
-- =============================================================================

INSERT INTO gate_configs (gate_name, tier, value, min_value, max_value, description)
VALUES
  (
    'exclude_indices', 1, 1.0, 0.0, 1.0,
    'Gate 6 (ING-011): filter index ETF options (SPY/QQQ/IWM/DIA/VXX/GLD/TLT/HYG/EEM/SLV). '
    '1.0 = filter ON (default). 0.0 = pass-through. Tier-1 is the canonical row read at runtime.'
  ),
  (
    'exclude_indices', 2, 1.0, 0.0, 1.0,
    'Gate 6 (ING-011): tier-2 row seeded for schema completeness — not read at runtime.'
  ),
  (
    'exclude_indices', 3, 1.0, 0.0, 1.0,
    'Gate 6 (ING-011): tier-3 row seeded for schema completeness — not read at runtime.'
  )
ON CONFLICT (gate_name, tier) DO NOTHING;
