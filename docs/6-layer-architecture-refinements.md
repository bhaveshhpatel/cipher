# Options Trade Flow Architecture Analysis & Strategy
**Date:** April 24, 2026
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

### Layer 3: Parser (`parsers/options_flow_parser.py`)
- **Critical Fix (C-015):** Corrected logic to use `last` as the fill price (not `price`).
- **Safety:** Implemented `size == 0` guards and synthetic bid/ask spread generation when data is missing.
- **C-018 (2026-04-24):** Added `is_synthetic_quote` flag to `OptionsFlowEvent`. When Tradier omits both `bid` and `ask` (both arrive as `0`) the parser synthesises a ±0.5% NBBO from the fill price so `classify_bid_ask()` can still run. These rows are now tagged `is_synthetic_quote = True`. Their `bid_ask_class` and `is_aggressive` values are **derived from the fill, not real market data** — exclude them from backtesting aggression and net-premium calculations.

### Layer 4: Deduplication (`utils/dedup.py`)
- **Logic:** 2-second TTL cache keyed on `(occ_symbol, size, fill_price, time_bucket)`.
- **Sweep Detection:** 3+ exchanges reporting the same trade within 5 seconds upgrades the event to a `SWEEP`.
- **Fix (2026-04-24):** Integrated `DedupCache` into `_process_trade()` to prevent redundant database writes.

### Layer 5: Batched DB Writes (`services/flow_store.py`)
- **Strategy:** Buffer events and flush to Supabase using `SUPABASE_SERVICE_ROLE_KEY`.
- **Performance Fix (2026-04-24):** Reduced `_FLUSH_INTERVAL` from 5s to 500ms and added a 100-row immediate flush trigger.
- **C-018 (2026-04-24):** `persist_flow_event()` now writes `is_synthetic_quote` to `flow_events.is_synthetic_quote`. Requires DB migration `009_flow_events_synthetic_quote.sql`.

### Layer 6: Frontend Broadcast (Supabase Realtime)
- **Function:** Automatic broadcast of `INSERT` events to frontend clients via `flow_episodes` and `signal_history` channels.

---

## 2. Data Quality Flags in `flow_events`

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

## 3. Tradier API: Analysis and Limitations

### The "Fatal Flaw" (Layer 2)
Tradier's 2026 specifications explicitly prohibit multiple concurrent market data sessions. Opening 32 parallel connections with the same account token will result in:
- **Connection Collisions:** Newer sessions killing older ones in a loop.
- **Account Throttling:** Potential API key suspension for "egregious" usage patterns.
- **Symbol Limits:** Tradier monitors for users attempting to reconstruct the full OPRA firehose for free.

### Proposed Fixes for Tradier
To maintain a "free" tier while staying compliant, the architecture must pivot from a "Static Firehose" to a **"Dynamic Sniper"** approach:

1.  **WebSocket Transition:** Move from HTTP streaming to WebSockets (`wss://ws.tradier.com/v1/markets/events`). This allows for `add` and `remove` commands on a single connection.
2.  **Sliding Window Logic:** - Monitor top 500-1,000 underlying tickers.
    - Dynamically subscribe to OTM/ITM option contracts based on underlying price movement.
    - Unsubscribe from inactive or deep-out-of-the-money contracts to stay under the single-connection cap.
3.  **Enhanced Trade Detection:** In the 2026 payload, strictly validate `last_volume > 0` and check the `trade_date` timestamp to distinguish between "Trade" ticks and "Quote-only" ticks.

---

## 4. Charles Schwab API: Evaluation & Suggestions

Transitioning to the Schwab API (legacy TDA stack) provides a superior path for this 6-layer architecture.

### Simplified Layer 2 (The Primary Benefit)
- **Single Session Power:** Schwab allows for a single, high-capacity WebSocket connection that can handle thousands of symbols via the `OPTION` service.
- **StreamerInfo Integration:** Requires calling the `/accounts/v1/accounts/{id}/streamer` endpoint to get session credentials, but the resulting stream is far more stable than Tradier's retail endpoints.

### Improved Data Integrity (Layer 3)
- **Explicit Trade Fields:** Schwab provides clear `Last Price` and `Last Size` fields specifically for trades, removing the "guessing game" required in Tradier's combined quote stream.
- **Sequence Tracking:** Includes sequence numbers that make deduplication (Layer 4) even more precise.
- **Note:** Schwab always sends real NBBO — `is_synthetic_quote` would be `false` for all rows from a Schwab feed, eliminating this data quality issue entirely.

### Architecture Comparison Table

| Feature | Tradier (Modified) | Charles Schwab |
| :--- | :--- | :--- |
| **Connection Count** | 1 (Required by Spec) | 1 (Standard) |
| **Symbol Capacity** | ~500 (Strictly Monitored) | ~5,000+ (High Performance) |
| **Auth Complexity** | Low (Bearer Token) | High (OAuth 2.0 + Refresh Tokens) |
| **Trade Logic** | Heuristic-based (Size/Price) | Explicit (Trade Service Fields) |
| **Synthetic Quotes** | ~10-30% of ticks (bid/ask=0) | Never — real NBBO always present |
| **Suitability** | Best for Targeted/Dynamic Flow | Best for Broad Market Firehose |

---

## 5. Final Recommendations

1.  **Authentication Management:** If moving to Schwab, Layer 2 must include a "Silent Refresh" worker to update OAuth tokens every 30 minutes without interrupting the WebSocket.
2.  **Deduplication Retainment:** Keep Layer 4 (Deduplication) regardless of the provider. Even high-end feeds encounter multi-exchange reporting lag.
3.  **Data Flagging:** In Layer 3, always flag "Synthetic Quotes" or "Late Prints" to ensure downstream signals (Layer 6) are not skewed by anomalous data points. **C-018 implements this for the Tradier feed.**
4.  **Backtest Hygiene:** Always filter `is_synthetic_quote = false` before computing aggression ratios, net-premium tallies, or conviction scores from historical `flow_events` data.
