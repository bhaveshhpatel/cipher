-- REARCH-010 | Stage 4: Drop all retired columns
-- PG14+: ALTER TABLE DROP COLUMN is catalog-only for columns without
-- stored defaults requiring heap rewrite. No table lock beyond brief
-- AccessExclusiveLock on catalog entry (milliseconds).
--
-- flow_events: drop is_golden_sweep, influence_tier, conviction_score
-- flow_episodes: drop seed_episode
-- signal_history: drop swarm_* (6 cols), volume_premium_factor, influence_tier

BEGIN;

-- flow_events (3 columns)
ALTER TABLE flow_events
  DROP COLUMN IF EXISTS is_golden_sweep,
  DROP COLUMN IF EXISTS influence_tier,
  DROP COLUMN IF EXISTS conviction_score;

-- flow_episodes (1 column)
ALTER TABLE flow_episodes
  DROP COLUMN IF EXISTS seed_episode;

-- signal_history (8 columns)
ALTER TABLE signal_history
  DROP COLUMN IF EXISTS swarm_direction,
  DROP COLUMN IF EXISTS swarm_confidence,
  DROP COLUMN IF EXISTS swarm_agents,
  DROP COLUMN IF EXISTS swarm_bull_votes,
  DROP COLUMN IF EXISTS swarm_bear_votes,
  DROP COLUMN IF EXISTS swarm_hold_votes,
  DROP COLUMN IF EXISTS volume_premium_factor,
  DROP COLUMN IF EXISTS influence_tier;

COMMIT;

-- POST-STAGE VERIFICATION:
-- All three queries must return 0 rows.

SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'flow_events'
  AND column_name IN ('is_golden_sweep', 'influence_tier', 'conviction_score');

SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'flow_episodes'
  AND column_name = 'seed_episode';

SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'signal_history'
  AND column_name IN (
    'swarm_direction', 'swarm_confidence', 'swarm_agents',
    'swarm_bull_votes', 'swarm_bear_votes', 'swarm_hold_votes',
    'volume_premium_factor', 'influence_tier'
  );
