-- =============================================================================
-- 032_rearch006_debounce_config.sql
-- REARCH-006 — Signal Engine Rewrite: SIG-DEBOUNCE configuration seed
--
-- Adds two signal_config rows that govern the emit-side deduplication window.
-- These rows are read hot by SignalDebounce on every potential emit — no
-- redeploy required to change the window or toggle the kill-switch.
--
-- Deliberation: PBE-3 (2026-05-12)
--   Mechanism:  in-memory dict keyed on (symbol, contract_key, alert_level)
--   Scope:      per-triplet independently (allows TSLA 200C + TSLA 200P to
--               both emit GOLDEN simultaneously; allows BLOCK→GOLDEN upgrade
--               on same contract within window)
--   Window:     90s — covers fast sweep trains (~60s max on active names)
--               with margin. Under 60 is too tight for illiquid names;
--               over 120 starts suppressing genuinely separate entries.
--   Kill-switch: sig.debounce_enabled = 'false' disables without redeploy
--
-- Safe to re-run: ON CONFLICT (key) DO NOTHING
-- =============================================================================

BEGIN;

INSERT INTO signal_config (key, value, description, updated_at)
VALUES
  (
    'sig.debounce_window_seconds',
    '90',
    'SIG-DEBOUNCE: emit-side deduplication window in seconds per (symbol, contract_key, alert_level) triplet. Fast sweep trains (5 fills in <60s) on the same contract+level are suppressed to a single signal_history write. Does not affect episode accumulation or cross-day signals.',
    NOW()
  ),
  (
    'sig.debounce_enabled',
    'true',
    'SIG-DEBOUNCE master kill-switch. Set to ''false'' to disable emit-side deduplication without a redeploy. Use for debugging signal suppression issues in production.',
    NOW()
  )
ON CONFLICT (key) DO NOTHING;

-- Verify both rows exist after insert
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM signal_config
    WHERE key IN ('sig.debounce_window_seconds', 'sig.debounce_enabled');

    IF v_count < 2 THEN
        RAISE EXCEPTION 'VALIDATION FAILED: expected 2 debounce config rows, found %', v_count;
    END IF;

    RAISE NOTICE 'VALIDATION PASSED: SIG-DEBOUNCE config rows present (count=%).',  v_count;
END
$$;

COMMIT;

-- =============================================================================
-- POST-COMMIT verification (run manually or in CI):
-- SELECT key, value, description FROM signal_config
-- WHERE key IN ('sig.debounce_window_seconds', 'sig.debounce_enabled')
-- ORDER BY key;
-- =============================================================================
