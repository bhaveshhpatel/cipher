-- ING-007: Supabase RPC function for per-contract multi-day lookback.
-- Deploy this function to cipher-database before running ING-007 Python.
--
-- Called by contract_day_cache._fetch_from_db() via PostgREST RPC.
-- Returns a single JSON object: {prior_days_active, prior_days_aggressive}
--
-- DATE_TRUNC('day', NOW()) ceiling: excludes today's flow from prior_days_active.
-- Today's episode is what is being evaluated — it must not count itself.
--
-- Index used: idx_flow_events_contract_day (ticker, contract_type, strike, expiry, created_at DESC)
-- Confirmed via EXPLAIN ANALYZE on 2026-05-04: 0.225ms execution time.

CREATE OR REPLACE FUNCTION get_contract_prior_days(
    p_ticker        TEXT,
    p_contract_type TEXT,
    p_strike        NUMERIC,
    p_expiry        TEXT,
    p_min_premium   NUMERIC
)
RETURNS JSON
LANGUAGE sql
STABLE
AS $$
    SELECT json_build_object(
        'prior_days_active',     COALESCE(COUNT(DISTINCT DATE(created_at)), 0),
        'prior_days_aggressive', COALESCE(COUNT(DISTINCT DATE(created_at)) FILTER (WHERE is_aggressive = TRUE), 0)
    )
    FROM flow_events
    WHERE ticker        = p_ticker
      AND contract_type = p_contract_type
      AND strike        = p_strike
      AND expiry        = p_expiry
      AND created_at   >= NOW() - INTERVAL '5 days'
      AND created_at   <  DATE_TRUNC('day', NOW())
      AND premium      >= p_min_premium;
$$;
