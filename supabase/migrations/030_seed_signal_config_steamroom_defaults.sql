-- 030_seed_signal_config_steamroom_defaults.sql  [REARCH-005]
-- Seeds the 11 WSJ Steamroom default knob rows into signal_config.
--
-- Uses INSERT ... ON CONFLICT DO NOTHING so this migration is idempotent:
-- re-running it (e.g. after a branch reset) never overwrites a live-tuned value.
--
-- Default values match _DEFAULTS in signal_config_store.py exactly.
-- If you change a default here, update _DEFAULTS in signal_config_store.py
-- to match, and vice versa.
--
-- Key naming convention: all signal knobs use the "sig." prefix to namespace
-- them clearly away from ingestion_config keys in logs and admin UI.
--
-- WSJ Steamroom 5-Dimension mapping:
--   Dim 1  Premium Threshold  sig.golden_sweep_premium / sig.block_premium / sig.noteworthy_premium
--   Dim 2  Ask-Side           sig.require_ask_side / sig.ask_side_pct_floor
--   Dim 3  Vol > OI           sig.require_vol_gt_oi
--   Dim 4  DTE Quality        sig.min_dte / sig.max_dte
--   Dim 5  Repetition         sig.min_trade_count
--   Score  Emission gate      sig.steamroom_score_floor

INSERT INTO signal_config (key, value, value_type, description) VALUES

    -- Dimension 1: Premium Threshold -------------------------------------------
    (
        'sig.golden_sweep_premium',
        '1000000.0',
        'float',
        'Minimum total notional premium (USD) for a GOLDEN alert. '
        'Episode notional >= this value AND all 5 Steamroom dimensions pass -> GOLDEN. '
        'WSJ Steamroom default: $1,000,000.'
    ),
    (
        'sig.block_premium',
        '500000.0',
        'float',
        'Minimum total notional premium (USD) for a BLOCK alert. '
        'Episode notional in [block_premium, golden_sweep_premium) -> BLOCK. '
        'WSJ Steamroom default: $500,000.'
    ),
    (
        'sig.noteworthy_premium',
        '50000.0',
        'float',
        'Minimum total notional premium (USD) for a NOTEWORTHY alert. '
        'Episode notional in [noteworthy_premium, block_premium) -> NOTEWORTHY. '
        'Below this (but above ingestion floor) -> WATCH. '
        'WSJ Steamroom default: $50,000.'
    ),

    -- Dimension 2: Ask-Side Execution -------------------------------------------
    (
        'sig.require_ask_side',
        'true',
        'bool',
        'Gate: episode must be ask-side dominant to emit a signal. '
        'When true, episodes with ask_side_pct < sig.ask_side_pct_floor are rejected. '
        'WSJ Steamroom default: true (ask-side aggression is a conviction requirement).'
    ),
    (
        'sig.ask_side_pct_floor',
        '0.6',
        'float',
        'Minimum fraction of episode trades that must be ask-side fills '
        'when sig.require_ask_side is true. '
        'Range: 0.0-1.0. WSJ Steamroom default: 0.6 (60%% ask-side).'
    ),

    -- Dimension 3: Vol > OI -----------------------------------------------------
    (
        'sig.require_vol_gt_oi',
        'true',
        'bool',
        'Gate: episode vol_oi_signal must be true OR volume/OI ratio > 1.0. '
        'Filters out low-conviction prints where open interest dwarfs volume. '
        'WSJ Steamroom default: true.'
    ),

    -- Dimension 4: DTE Quality --------------------------------------------------
    (
        'sig.min_dte',
        '5',
        'int',
        'Minimum days-to-expiry at signal-layer evaluation. '
        'Tighter than the ingestion floor (min_dte=1) — filters weekly pin risk. '
        'WSJ Steamroom default: 5 DTE.'
    ),
    (
        'sig.max_dte',
        '60',
        'int',
        'Maximum days-to-expiry at signal-layer evaluation. '
        'Tighter than the ingestion ceiling (max_dte=90) — LEAPS are tagged but not alerted. '
        'WSJ Steamroom default: 60 DTE.'
    ),

    -- Dimension 5: Repetition / Clustering -------------------------------------
    (
        'sig.min_trade_count',
        '2',
        'int',
        'Minimum number of constituent trades in an episode before a signal can emit. '
        'Filters single-print noise; requires at least one follow-through trade. '
        'WSJ Steamroom default: 2 trades.'
    ),

    -- Scoring gate --------------------------------------------------------------
    (
        'sig.steamroom_score_floor',
        '3',
        'int',
        'Minimum Steamroom conviction score (0-5) required to emit a signal. '
        'Score increments by 1 for each of the 5 Steamroom dimensions that passes. '
        'Default 3/5 means at least 3 conviction dimensions must be satisfied. '
        'Set to 5 for maximum conviction filtering; 1 to surface all tagged episodes.'
    )

ON CONFLICT (key) DO NOTHING;

-- Verify: confirm all 11 rows are present after seed.
-- This DO block logs a WARNING to postgres logs if any row is missing,
-- mirroring the startup-validator pattern in signal_config_store.validate_signal_config().
DO $$
DECLARE
    expected_count INT := 11;
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
            '[REARCH-005] signal_config seed OK: % rows present.', actual_count;
    END IF;
 END;
$$;
