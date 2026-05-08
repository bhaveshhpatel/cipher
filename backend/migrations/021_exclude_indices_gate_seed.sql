-- migration 021: exclude_indices gate seed (ING-011)
--
-- Context:
--   019 seeded all 21 gate_configs rows including exclude_indices.
--   020 re-seeded just exclude_indices with ON CONFLICT DO UPDATE
--       (fix SA-3 — rows were missing in some envs after 019).
--   021 (this file) is an idempotent safety net that also seeds
--       signal_min_premium, which was absent from both 019 and 020.
--
-- All INSERTs use ON CONFLICT (gate_name, tier) DO NOTHING so this
-- migration is safe to re-apply in any environment.

-- Ensure exclude_indices rows exist (no-op if 019/020 already applied).
INSERT INTO gate_configs
  (gate_name, tier, value, value_type, min_value, max_value, description, updated_by)
VALUES
  ('exclude_indices', 1, 1.0, 'boolean', 0.0, 1.0,
   'Gate 6 (ING-011): filter index ETF options (SPY/QQQ/IWM/DIA/VXX/GLD/TLT/HYG/EEM/SLV). '
   '1.0 = filter ON (default). 0.0 = pass-through. '
   'Tier-1 is the canonical row read at runtime.',
   'system'),
  ('exclude_indices', 2, 1.0, 'boolean', 0.0, 1.0,
   'Gate 6 (ING-011): tier-2 row seeded for schema completeness - not read at runtime.',
   'system'),
  ('exclude_indices', 3, 1.0, 'boolean', 0.0, 1.0,
   'Gate 6 (ING-011): tier-3 row seeded for schema completeness - not read at runtime.',
   'system')
ON CONFLICT (gate_name, tier) DO NOTHING;

-- Seed signal_min_premium rows (absent from migrations 019 and 020).
-- Values match _DEFAULTS in services/gate_config_store.py:
--   T1 = 75 000 (tight — whale/institutional flow only)
--   T2 = 50 000 (moderate)
--   T3 = 25 000 (permissive — retail-tier catch-all)
INSERT INTO gate_configs
  (gate_name, tier, value, value_type, min_value, max_value, description, updated_by)
VALUES
  ('signal_min_premium', 1, 75000.0, 'currency', 1000.0, 500000.0,
   'Minimum signal premium ($) for T1 — only trades scoring above this threshold emit a signal.',
   'system'),
  ('signal_min_premium', 2, 50000.0, 'currency', 1000.0, 500000.0,
   'Minimum signal premium ($) for T2 — only trades scoring above this threshold emit a signal.',
   'system'),
  ('signal_min_premium', 3, 25000.0, 'currency', 1000.0, 500000.0,
   'Minimum signal premium ($) for T3 — only trades scoring above this threshold emit a signal.',
   'system')
ON CONFLICT (gate_name, tier) DO NOTHING;
