-- get_contract_prior_days.sql
-- Paste into Supabase SQL editor and run ONCE to register the function.
--
-- Returns the number of distinct prior trading days (America/New_York midnight)
-- on which the given contract already appeared in flow_episodes — NOT counting
-- today.  A return value >= 1 means the contract is a multi-day repeat.
--
-- Parameters:
--   p_ticker        TEXT   e.g. 'AAPL'
--   p_contract_type TEXT   'CALL' or 'PUT'
--   p_strike        FLOAT8 e.g. 150.0
--   p_expiry        TEXT   'YYYY-MM-DD'
--
-- Usage (from Python via Supabase RPC):
--   client.rpc('get_contract_prior_days', {
--       'p_ticker': 'AAPL', 'p_contract_type': 'CALL',
--       'p_strike': 150.0,  'p_expiry': '2026-06-20'
--   }).execute()

CREATE OR REPLACE FUNCTION get_contract_prior_days(
    p_ticker        TEXT,
    p_contract_type TEXT,
    p_strike        FLOAT8,
    p_expiry        TEXT
)
RETURNS INTEGER
LANGUAGE sql
STABLE
AS $$
    SELECT COUNT(DISTINCT
               DATE(
                   (signal_ts AT TIME ZONE 'America/New_York')
               )
           )::INTEGER
    FROM   flow_episodes
    WHERE  ticker        = p_ticker
      AND  contract_type = p_contract_type
      AND  strike        = p_strike
      AND  expiry        = p_expiry
      -- Exclude today (ET) so we only count PRIOR days
      AND  DATE(signal_ts AT TIME ZONE 'America/New_York')
           < CURRENT_DATE AT TIME ZONE 'America/New_York';
$$;
