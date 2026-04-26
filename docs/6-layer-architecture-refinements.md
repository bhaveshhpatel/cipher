# Options Trade Flow Architecture Analysis & Strategy
**Date:** April 24, 2026 (updated April 25, 2026 — Feature 4A, B-021/B-022/B-023)
**Subject:** Real-time Options Trade Flow Processing via Tradier and Charles Schwab APIs

---

## 1. Original 6-Layer Architecture Overview

The system is designed to ingest, process, and broadcast real-time options trade flow with minimal latency and high data integrity.

### Layer 1: Symbol Registry (`services/symbol_registry.py`)
- **Function:** Pre-loads ~16,000 OCC contract metadata into an O(1) in-memory dictionary.
- **Refresh Logic:** Every 30 minutes (15 minutes on expiry days).
- **Optimization:** Avoids regex and API calls during the stream tick.

### Layer 2: Stream Manager (`services/stream_manager.py`)
- **Original Strategy:** Parallelized streaming across 32 connections to circumvent symbol caps (~500 per connection).
- **Fix (2026-04-24):** Corrected the registry refresh loop to notify the manager to restart affected workers when symbols change.
- **B-021 (2026-04-25) — Staggered Worker Startup:** Workers no longer all call `get_session_token()` simultaneously at startup. Each worker sleeps `worker_index × 200ms` before its first token fetch. With ~32 workers this spreads token requests across ~6.4s, eliminating the thundering-herd burst that triggered Tradier 429s on every cold start and post-refresh restart.
- **B-022 (2026-04-25) — Global Session Token Semaphore:** A module-level `asyncio.Semaphore(3)` (stored in `tradier_stream._token_semaphore`) gates all concurrent calls to `get_session_token()` across every worker. At most 3 token fetches run in parallel at any time. With 32 workers in 200ms stagger batches this means the worst-case burst is 3 concurrent requests regardless of worker count. The 11-batch math: ceil(32 / 3) = 11 sequential semaphore slots × ~300ms avg fetch = ~3.3s total token-fetch window, well within Tradier rate limits.
- **B-023 (2026-04-25) — Explicit 429 + Retry-After Handler:** `get_session_token()` now parses HTTP 429 responses and reads the `Retry-After` header (defaulting to 5s if absent). On a 429 the worker sleeps the exact server-specified backoff before retrying, instead of immediately retrying and compounding the rate-limit violation.

#### B-021 + B-022 Combined Startup Timing (32 workers, nominal)

| Phase | Duration | Detail |
|-------|----------|--------|
| Stagger window (B-021) | ~6.4s | 32 × 200ms delays, fully parallelised |
| Token fetch window (B-022) | ~3.3s | 11 semaphore batches × ~300ms each |
| **Total one-time cold-start overhead** | **~10–11s** | One-time on deploy or full restart |
| Per-reconnect overhead | ~300–600ms | Single worker: 1 semaphore slot + fetch |

### Layer 3: Parser (`parsers/options_flow_parser.py`)
- **Critical Fix (C-015):** Corrected logic to use `last` as the fill price (not `price`).
- **Safety:** Implemented `size == 0` guards and synthetic bid/ask spread generation when data is missing.
- **C-018 (2026-04-24):** Added `is_synthetic_quote` flag to `OptionsFlowEvent`. When Tradier omits both `bid` and `ask` (both arrive as `0`) the parser synthesises a ±0.5% NBBO from the fill price so `classify_bid_ask()` can still run. These rows are now tagged `is_synthetic_quote = True`. Their `bid_ask_class` and `is_aggressive` values are **derived from the fill, not real market data** — exclude them from backtesting aggression and net-premium calculations.

### Layer 4: Deduplication (`utils/dedup.py`)
- **Logic:** 5-second TTL cache keyed on `(occ_symbol, size, round(fill_price, 1))`.
- **Sweep Detection:** 3+ exchanges reporting the same trade within 8 seconds upgrades the event to a `SWEEP`.
- **Fix (2026-04-24 C-019):** TTL extended 2s→5s, sweep window 5s→8s, eliminated `int(ts//2)` bucket boundary bug, fill key precision 2dp→1dp, exchange field now correctly passed via `"exch"/"exchange"` fallback.

### Layer 5: Batched DB Writes (`services/flow_store.py`)
- **Strategy:** Buffer events and flush to Supabase using `SUPABASE_SERVICE_ROLE_KEY`.
- **Performance Fix (2026-04-24):** Reduced `_FLUSH_INTERVAL` from 5s to 500ms and added a 100-row immediate flush trigger.
- **C-018 (2026-04-24):** `persist_flow_event()` now writes `is_synthetic_quote` to `flow_events.is_synthetic_quote`. Requires DB migration `009_flow_events_synthetic_quote.sql`.

### Layer 6: Frontend Broadcast (Supabase Realtime)
- **Function:** Automatic broadcast of `INSERT` events to frontend clients via `flow_episodes` and `signal_history` channels.

---

## 2. Feature 4A — Tier Engine (`services/tier_engine.py`)

**Added:** 2026-04-25

All options universe symbols are now classified into Tier 1/2/3 at startup and after every 24h universe refresh. Tier data flows into the composite signal score (`volume_premium_factor` OI lookup) and into `signal_history.influence_tier`.

### Tier Classification Logic

| Tier | Criteria | Admin Override |
|------|----------|--------------------|
| **1** | `average_volume ≥ 20M` OR in whitelist | SPY, QQQ, AAPL, TSLA, NVDA, MSFT, AMZN, META, GOOGL, AMD, PLTR, COIN |
| **2** | `average_volume ≥ 2M` | — |
| **3** | Everything else (default) | — |

Thresholds are stored in the `tier_thresholds` table (admin-editable via `PATCH /admin/tier-thresholds`). Only the row with `is_active = true` is read.

### Key Components

- **`TierEngine.load_thresholds()`** — reads active `tier_thresholds` row into `_TierParams` dataclass; cached in memory.
- **`TierEngine.assign_tiers(symbols)`** — classifies each symbol dict; applies admin whitelist first.
- **`TierEngine.upsert_tiers(symbols)`** — batch-writes `tier`, `open_interest`, `average_volume` back to `options_universe_symbols`.
- **`set_tier_map(symbols)`** — module-level helper; called by `main.py` after universe load. Builds `{ticker: int}` in-memory lookup.
- **`get_tier(ticker)`** — O(1) lookup; returns `3` if ticker not in map.
- **`universe_store.load_tier_map()`** — reads `(ticker, tier)` from the latest snapshot; used for cold-start tier recovery.

### DB Schema (Migrations 010 + 011, applied 2026-04-25)

```sql
-- 010: adds tier/OI columns to universe symbols
ALTER TABLE options_universe_symbols
  ADD COLUMN IF NOT EXISTS tier          SMALLINT NOT NULL DEFAULT 3,
  ADD COLUMN IF NOT EXISTS open_interest INT,
  ADD COLUMN IF NOT EXISTS average_volume INT;

-- 011: admin-configurable tier thresholds
CREATE TABLE IF NOT EXISTS tier_thresholds (
  id             BIGSERIAL PRIMARY KEY,
  t1_min_volume  BIGINT  NOT NULL DEFAULT 20000000,
  t2_min_volume  BIGINT  NOT NULL DEFAULT 2000000,
  t3_min_volume  BIGINT  NOT NULL DEFAULT 500000,
  is_active      BOOLEAN NOT NULL DEFAULT false,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Impact on Signal Scoring

Before Feature 4A, `volume_premium_factor` fell back to `0.5` for all symbols because `open_interest` was never written. After 4A, real OI is populated on each universe refresh, so the ×0.10 composite weight uses accurate data for Tier 1 and Tier 2 symbols.

---

## 3. Data Quality Flags in `flow_events`

The `flow_events` table carries boolean flags that describe the quality and nature of each persisted tick. These must be respected in any backtest or analytics query.

| Column | Type | Meaning | Backtest guidance |
|--------|------|---------|-------------------|
| `is_aggressive` | bool | Fill was at or above ask (real NBBO) | Exclude when `is_synthetic_quote = true` |
| `is_golden_sweep` | bool | Multi-exchange sweep above $1M premium | Always reliable |
| `is_synthetic_quote` | bool | bid=ask=0 — NBBO was synthesised from fill ±0.5% | **Exclude from aggression and net-premium calcs** |

### Recommended Backtest Filter

```sql
-- Aggression analysis: real NBBO rows only
SELECT ticker, count(*) as aggressive_trades, sum(premium) as total_premium
FROM flow_events
WHERE is_synthetic_quote = false
  AND is_aggressive = true
  AND timestamp > now() - interval '1 day'
GROUP BY ticker
ORDER BY total_premium DESC;
```

---

## 4. Tradier API: Analysis and Limitations

### The "Fatal Flaw" (Layer 2) — Mitigations Now Implemented

Tradier's 2026 specifications explicitly prohibit multiple concurrent market data sessions. Opening 32 parallel connections with the same account token will result in:
- **Connection Collisions:** Newer sessions killing older ones in a loop.
- **Account Throttling:** Potential API key suspension for "egregious" usage patterns.
- **Symbol Limits:** Tradier monitors for users attempting to reconstruct the full OPRA firehose for free.

**Status (2026-04-25):** B-021, B-022, and B-023 directly address these risks:
- **B-021** staggers worker startup at 200ms intervals to prevent simultaneous token bursts.
- **B-022** caps concurrent `get_session_token()` calls at 3 via a global semaphore, regardless of worker count.
- **B-023** respects Tradier's `Retry-After` header on 429 responses, eliminating retry storms.

These three fixes together reduce the token-fetch burst profile from 32 simultaneous requests to at most 3 concurrent, spread across ~10s — which is within normal Tradier usage patterns for a single account.

### Remaining Proposed Fixes for Tradier (Longer Term)

1. **WebSocket Transition:** Move from HTTP streaming to WebSockets (`wss://ws.tradier.com/v1/markets/events`). This allows for `add` and `remove` commands on a single connection.
2. **Sliding Window Logic:** Monitor top 500–1,000 underlying tickers. Dynamically subscribe to OTM/ITM option contracts based on underlying price movement. Unsubscribe from inactive or deep-out-of-the-money contracts to stay under the single-connection cap.
3. **Enhanced Trade Detection:** In the 2026 payload, strictly validate `last_volume > 0` and check the `trade_date` timestamp to distinguish between "Trade" ticks and "Quote-only" ticks.

---

## 5. Charles Schwab API: Evaluation & Suggestions

Transitioning to the Schwab API (legacy TDA stack) provides a superior path for this 6-layer architecture.

### Simplified Layer 2 (The Primary Benefit)
- **Single Session Power:** Schwab allows for a single, high-capacity WebSocket connection that can handle thousands of symbols via the `OPTION` service.
- **StreamerInfo Integration:** Requires calling the `/accounts/v1/accounts/{id}/streamer` endpoint to get session credentials, but the resulting stream is far more stable than Tradier's retail endpoints.

### Improved Data Integrity (Layer 3)
- **Explicit Trade Fields:** Schwab provides clear `Last Price` and `Last Size` fields specifically for trades, removing the "guessing game" required in Tradier's combined quote stream.
- **Sequence Tracking:** Includes sequence numbers that make deduplication (Layer 4) even more precise.
- **Note:** Schwab always sends real NBBO — `is_synthetic_quote` would be `false` for all rows from a Schwab feed, eliminating this data quality issue entirely.

### Architecture Comparison Table

| Feature | Tradier (B-021/B-022/B-023 applied) | Charles Schwab |
| :--- | :--- | :--- |
| **Connection Count** | 1 (Required by Spec) | 1 (Standard) |
| **Symbol Capacity** | ~500 (Strictly Monitored) | ~5,000+ (High Performance) |
| **Auth Complexity** | Low (Bearer Token) | High (OAuth 2.0 + Refresh Tokens) |
| **Trade Logic** | Heuristic-based (Size/Price) | Explicit (Trade Service Fields) |
| **Synthetic Quotes** | ~10-30% of ticks (bid/ask=0) | Never — real NBBO always present |
| **Token Burst Risk** | Mitigated (semaphore + stagger) | N/A (single connection) |
| **429 Handling** | Implemented (Retry-After) | N/A |
| **Suitability** | Best for Targeted/Dynamic Flow | Best for Broad Market Firehose |

---

## 6. Final Recommendations

1. **Authentication Management:** If moving to Schwab, Layer 2 must include a "Silent Refresh" worker to update OAuth tokens every 30 minutes without interrupting the WebSocket.
2. **Deduplication Retainment:** Keep Layer 4 (Deduplication) regardless of the provider. Even high-end feeds encounter multi-exchange reporting lag.
3. **Data Flagging:** In Layer 3, always flag "Synthetic Quotes" or "Late Prints" to ensure downstream signals (Layer 6) are not skewed by anomalous data points. **C-018 implements this for the Tradier feed.**
4. **Backtest Hygiene:** Always filter `is_synthetic_quote = false` before computing aggression ratios, net-premium tallies, or conviction scores from historical `flow_events` data.
5. **Tier Engine (Feature 4A):** Run `tier_engine.load_thresholds()` + `set_tier_map()` immediately after every universe refresh so the composite score always uses current tier and OI data. Admin whitelist symbols (SPY, QQQ, etc.) are always Tier 1 regardless of volume.
6. **Rate-Limit Hygiene (B-021/B-022/B-023):** The stagger + semaphore pattern must be preserved on any Layer 2 refactor. Do not remove `startup_delay_s`, `_token_semaphore`, or the 429/Retry-After handler from `stream_worker.py` and `tradier_stream.py` — these are the primary defence against account throttling under the current Tradier multi-worker architecture.
