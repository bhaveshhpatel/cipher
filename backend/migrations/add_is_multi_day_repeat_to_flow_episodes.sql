-- Migration: add is_multi_day_repeat to flow_episodes
-- Run in Supabase SQL editor BEFORE deploying the ING-007 stream update.
--
-- is_multi_day_repeat = TRUE when the same ticker/contract_type/strike/expiry
-- appeared in flow_episodes on at least one prior trading day (ET).
-- Defaults to FALSE so existing rows are backward-compatible.

ALTER TABLE flow_episodes
    ADD COLUMN IF NOT EXISTS is_multi_day_repeat BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN flow_episodes.is_multi_day_repeat IS
    'TRUE when this contract (ticker/type/strike/expiry) appeared in '
    'flow_episodes on at least one prior trading day (ET). '
    'Set by the stream layer via get_contract_prior_days() RPC.';
