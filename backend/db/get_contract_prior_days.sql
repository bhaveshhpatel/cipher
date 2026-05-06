-- get_contract_prior_days.sql
-- Paste into Supabase SQL editor and run ONCE to register/update the function.
--
-- ING-007: Multi-day repeat contract lookback.
--
-- Returns two distinct prior-day counts for the given contract — NOT counting
-- today (America/New_York midnight boundary).
--
-- Parameters:
--   p_ticker        TEXT    e.g. 'AAPL'
--   p_contract_type TEXT    'CALL' or 'PUT'
--   p_strike        FLOAT8  e.g. 150.0
--   p_expiry        TEXT    'YYYY-MM-DD'
--   p_min_premium   FLOAT8  DTE-tier premium floor from accumulator context
--
-- Returns (via JSON object):
--   prior_days_active     INTEGER  — distinct prior days with any qualifying flow
--   prior_days_aggressive INTEGER  — same, filtered WHERE is_aggressive = TRUE
--
-- A return of prior_days_active >= 1 means the contract is a multi-day repeat.
--
-- Usage (from Python via Supabase RPC):
--   client.rpc('get_contract_prior_days', {
--       'p_ticker': 'AAPL', 'p_contract_type': 'CALL',
--       'p_strike': 150.0,  'p_expiry': '2026-06-20',
--       'p_min_premium': 10000.0
--   }).execute()

CREATE OR REPLACE FUNCTION get_contract_prior_days(
    p_ticker        TEXT,
    p_contract_type TEXT,
    p_strike        FLOAT8,
    p_expiry        TEXT,
    p_min_premium   FLOAT8
)
RETURNS JSON
LANGUAGE sql
STABLE
AS $$
    SELECT json_build_object(
        'prior_days_active',
        COUNT(DISTINCT DATE(signal_ts AT TIME ZONE 'America/New_York'))::INTEGER,

        'prior_days_aggressive',
        COUNT(DISTINCT DATE(signal_ts AT TIME ZONE 'America/New_York'))
            FILTER (WHERE is_aggressive = TRUE)::INTEGER
    )
    FROM   flow_episodes
    WHERE  ticker        = p_ticker
      AND  contract_type = p_contract_type
      AND  strike        = p_strike
      AND  expiry        = p_expiry
      AND  total_premium >= p_min_premium
      -- Exclude today (ET) — only count PRIOR trading days
      AND  DATE(signal_ts AT TIME ZONE 'America/New_York')
           < (CURRENT_TIMESTAMP AT TIME ZONE 'America/New_York')::DATE
      -- 5-day rolling window (PBE deliberation, 2026-05-04)
      AND  signal_ts >= NOW() - INTERVAL '5 days';
$$;
