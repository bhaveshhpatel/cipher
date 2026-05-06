-- =============================================================================
-- contract_day_lookback  — Postgres RPC function for ING-007 Gate 5
-- Apply via: Supabase MCP apply_migration OR Supabase SQL editor
-- =============================================================================
--
-- Called by ContractDayCache._fetch_from_db() via:
--   POST /rest/v1/rpc/contract_day_lookback
--
-- Returns one row:
--   prior_days_active      INT  — distinct calendar days with >= p_min_premium flow
--   prior_days_aggressive  INT  — subset of above with is_aggressive = TRUE
--
-- DATE_TRUNC ceiling ensures today is excluded (SA + PBE decision 2026-05-04).
-- =============================================================================

CREATE OR REPLACE FUNCTION contract_day_lookback(
    p_ticker        TEXT,
    p_contract_type TEXT,
    p_strike        NUMERIC,
    p_expiry        TEXT,
    p_lookback_days INT     DEFAULT 5,
    p_min_premium   NUMERIC DEFAULT 50000
)
RETURNS TABLE (
    prior_days_active      INT,
    prior_days_aggressive  INT
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        COUNT(DISTINCT DATE(created_at))::INT                                           AS prior_days_active,
        COUNT(DISTINCT DATE(created_at)) FILTER (WHERE is_aggressive = TRUE)::INT       AS prior_days_aggressive
    FROM flow_events
    WHERE ticker        = p_ticker
      AND contract_type = p_contract_type
      AND strike        = p_strike
      AND expiry        = p_expiry
      AND premium       >= p_min_premium
      AND created_at   >= NOW() - (p_lookback_days || ' days')::INTERVAL
      AND created_at   <  DATE_TRUNC('day', NOW());
$$;
