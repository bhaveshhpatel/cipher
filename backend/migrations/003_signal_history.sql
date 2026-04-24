-- Migration 003: signal_history
-- Persists every CompositeSignal emitted by composite_signal_engine.py
-- to a queryable table for history, analytics, and the /api/signals/history
-- and /api/signals/list endpoints.
--
-- Columns mirror CompositeSignal dataclass (Phase 3 weights):
--   flow × 0.55 + backtest × 0.35 + volume_premium × 0.10
--
-- RLS: table is protected. Only service role key may INSERT.
-- The anon key (SUPABASE_KEY) must NOT be used for writes here.

CREATE TABLE IF NOT EXISTS signal_history (
    id                    BIGSERIAL PRIMARY KEY,
    ticker                TEXT        NOT NULL,
    recommendation        TEXT        NOT NULL,   -- BUY | SELL | HOLD
    composite_score       NUMERIC(5,3) NOT NULL,
    flow_score            NUMERIC(5,3) NOT NULL,
    backtest_score        NUMERIC(5,3) NOT NULL,
    volume_premium_factor NUMERIC(5,3) NOT NULL DEFAULT 0.5,
    reasoning             TEXT,
    -- Raw episode fields (denormalized for fast filter queries)
    contract_type         TEXT,                  -- CALL | PUT
    direction             TEXT,                  -- BULLISH | BEARISH | NEUTRAL
    influence_tier        TEXT,                  -- WHALE | INSTITUTIONAL | LARGE | RETAIL
    total_premium         NUMERIC(14,2),
    trade_count           INTEGER,
    is_accelerating       BOOLEAN     NOT NULL DEFAULT false,
    signal_ts             TIMESTAMPTZ,           -- original episode timestamp from Tradier
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for the most common query patterns
CREATE INDEX IF NOT EXISTS idx_signal_history_ticker
    ON signal_history (ticker);

CREATE INDEX IF NOT EXISTS idx_signal_history_recommendation
    ON signal_history (recommendation);

CREATE INDEX IF NOT EXISTS idx_signal_history_composite_score
    ON signal_history (composite_score DESC);

CREATE INDEX IF NOT EXISTS idx_signal_history_created_at
    ON signal_history (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_signal_history_direction
    ON signal_history (direction);

CREATE INDEX IF NOT EXISTS idx_signal_history_influence_tier
    ON signal_history (influence_tier);

-- Composite index for the /list endpoint common filter pattern
CREATE INDEX IF NOT EXISTS idx_signal_history_rec_score
    ON signal_history (recommendation, composite_score DESC, created_at DESC);

COMMENT ON TABLE signal_history IS
    'Persisted CompositeSignal records from Phase 3 composite_signal_engine. '
    'Written exclusively by signal_store.py using SUPABASE_SERVICE_ROLE_KEY. '
    'Never insert from anon key — RLS will reject with 42501.';
