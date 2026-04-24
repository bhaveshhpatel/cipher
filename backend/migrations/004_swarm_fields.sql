-- Phase 5A: Add swarm verdict fields to signal_history
-- Run this migration in Supabase SQL editor

ALTER TABLE signal_history
  ADD COLUMN IF NOT EXISTS swarm_direction   TEXT    DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS swarm_confidence  NUMERIC DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS swarm_agents      JSONB   DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS swarm_bull_votes  INTEGER DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS swarm_bear_votes  INTEGER DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS swarm_hold_votes  INTEGER DEFAULT NULL;

COMMENT ON COLUMN signal_history.swarm_direction  IS 'Ensemble swarm verdict: BUY | SELL | HOLD';
COMMENT ON COLUMN signal_history.swarm_confidence IS 'Swarm confidence 0.0-1.0 (winning vote share)';
COMMENT ON COLUMN signal_history.swarm_agents     IS 'JSON array of per-agent verdicts [{role, direction, reasoning, confidence}]';
COMMENT ON COLUMN signal_history.swarm_bull_votes IS 'Number of agents that voted BUY';
COMMENT ON COLUMN signal_history.swarm_bear_votes IS 'Number of agents that voted SELL';
COMMENT ON COLUMN signal_history.swarm_hold_votes IS 'Number of agents that voted HOLD';
