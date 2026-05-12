-- 031_seed_signal_config_tier_multipliers.sql  [REARCH-005 / PBE multiplier extension]
-- Adds 6 tier-sensitivity multiplier knobs to signal_config.
--
-- RATIONALE (PBE deliberation outcome):
--   A flat $50K NOTEWORTHY threshold treats a $50K Tier-3 small-cap print
--   identically to a $50K Tier-1 large-cap print.  The former is a significant
--   institutional-relative print; the latter is noise.  Without ADV data in
--   the pipeline, a multiplier-per-tier pattern gives operators a tunable
--   tier-sensitive gate using only data already present in flow_episodes
--   (notional_tier column added in REARCH-004).
--
-- HOW IT WORKS (Signal Engine, REARCH-006):
--   effective_threshold = base_threshold * multiplier
--
--   Tier-1 (large-cap):   multiplier = 1.0  (base threshold unchanged)
--   Tier-2 (mid-cap):     multiplier = 0.5  (50% of Tier-1 base)
--   Tier-3 (small-cap):   multiplier = 0.2  (20% of Tier-1 base)
--
--   Defaults produce these effective thresholds:
--
--   Alert Level   Tier-1      Tier-2      Tier-3
--   GOLDEN        $1,000,000  $500,000    $200,000
--   BLOCK         $500,000    $250,000    $100,000
--   NOTEWORTHY    $50,000     $25,000     $10,000
--
-- Multipliers are tunable at runtime via admin PATCH /admin/signal-config
-- without a restart (signal_config_store 30s TTL).
--
-- Uses INSERT ... ON CONFLICT DO NOTHING for idempotency.
-- Must run AFTER 030_seed_signal_config_steamroom_defaults.sql.

INSERT INTO signal_config (key, value, value_type, description) VALUES

    -- GOLDEN sweep tier multipliers --------------------------------------------
    (
        'sig.golden_sweep_premium_t2_mult',
        '0.5',
        'float',
        'Tier-2 multiplier for sig.golden_sweep_premium. '
        'Effective Tier-2 GOLDEN threshold = sig.golden_sweep_premium * this value. '
        'Default 0.5 -> $500,000 effective for mid-cap names. '
        'Range: 0.01-1.0. Set to 1.0 to disable tier differentiation for Tier-2.'
    ),
    (
        'sig.golden_sweep_premium_t3_mult',
        '0.2',
        'float',
        'Tier-3 multiplier for sig.golden_sweep_premium. '
        'Effective Tier-3 GOLDEN threshold = sig.golden_sweep_premium * this value. '
        'Default 0.2 -> $200,000 effective for small-cap names. '
        'Range: 0.01-1.0. Set to 1.0 to disable tier differentiation for Tier-3.'
    ),

    -- BLOCK tier multipliers ---------------------------------------------------
    (
        'sig.block_premium_t2_mult',
        '0.5',
        'float',
        'Tier-2 multiplier for sig.block_premium. '
        'Effective Tier-2 BLOCK threshold = sig.block_premium * this value. '
        'Default 0.5 -> $250,000 effective for mid-cap names. '
        'Range: 0.01-1.0.'
    ),
    (
        'sig.block_premium_t3_mult',
        '0.2',
        'float',
        'Tier-3 multiplier for sig.block_premium. '
        'Effective Tier-3 BLOCK threshold = sig.block_premium * this value. '
        'Default 0.2 -> $100,000 effective for small-cap names. '
        'Range: 0.01-1.0.'
    ),

    -- NOTEWORTHY tier multipliers ----------------------------------------------
    (
        'sig.noteworthy_premium_t2_mult',
        '0.5',
        'float',
        'Tier-2 multiplier for sig.noteworthy_premium. '
        'Effective Tier-2 NOTEWORTHY threshold = sig.noteworthy_premium * this value. '
        'Default 0.5 -> $25,000 effective for mid-cap names. '
        'Range: 0.01-1.0.'
    ),
    (
        'sig.noteworthy_premium_t3_mult',
        '0.2',
        'float',
        'Tier-3 multiplier for sig.noteworthy_premium. '
        'Effective Tier-3 NOTEWORTHY threshold = sig.noteworthy_premium * this value. '
        'Default 0.2 -> $10,000 effective for small-cap names. '
        'Range: 0.01-1.0.'
    )

ON CONFLICT (key) DO NOTHING;

-- Verify: confirm all 16 rows (10 base + 6 multipliers) are present.
DO $$
DECLARE
    expected_count INT := 16;
    actual_count   INT;
BEGIN
    SELECT COUNT(*) INTO actual_count FROM signal_config;
    IF actual_count < expected_count THEN
        RAISE WARNING
            '[REARCH-005] signal_config has % rows after seed; expected %. '
            'Check for ON CONFLICT skips on duplicate keys.',
            actual_count, expected_count;
    ELSE
        RAISE NOTICE
            '[REARCH-005] signal_config seed OK: % rows present (10 base + 6 tier multipliers).',
            actual_count;
    END IF;
END;
$$;
