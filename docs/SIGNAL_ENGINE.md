# Cipher Signal Engine — Reference Document

> **Living document.** Updated automatically as the signal pipeline evolves.
> Last updated: 2026-04-23

---

## Overview

The Cipher signal engine is a real-time options flow pipeline that:
1. **Ingests** raw trade ticks from the Tradier streaming API
2. **Parses & classifies** each tick (contract type, sentiment, influence tier, conviction)
3. **Accumulates** repeated flow on the same contract within a rolling window
4. **Scores** each qualifying episode using flow metrics + historical backtest win-rate
5. **Emits** a composite BUY / SELL / HOLD recommendation with structured reasoning
6. **Persists** raw events and signal episodes to Supabase (service role key required)
7. **Broadcasts** signals in real-time to all connected WebSocket clients via the async bus

---

## Module Map

```
backend/
├── parsers/
│   ├── options_flow_parser.py      # Raw tick → OptionsFlowEvent dataclass
│   ├── bid_ask_classifier.py       # Classifies trade vs mid / above ask / below bid
│   └── trade_type_detector.py      # SWEEP, BLOCK, SPLIT detection
├── signals/
│   ├── repetition_accumulator.py   # Rolling-window episode builder + alert levels
│   ├── composite_signal_engine.py  # Flow score + backtest score → composite signal
│   ├── backtest_validator.py       # Historical win-rate lookup (Supabase / cache)
│   └── midcap_screener.py          # Mid-cap universe filter
├── services/
│   ├── tradier_stream.py           # WebSocket consumer + pipeline orchestration
│   ├── flow_store.py               # Supabase DB writer (service role key only)
│   ├── universe_screener.py        # Builds the active symbol universe
│   └── universe_store.py           # Persists universe data to Supabase
├── core/
│   ├── async_bus.py                # In-memory pub/sub fan-out for WebSocket delivery
│   └── auth.py                     # JWT auth middleware
└── main.py                         # FastAPI app + lifespan startup
```

---

## Pipeline — Step by Step

### 1. Tick Ingestion (`tradier_stream.py`)
- Opens a persistent WebSocket connection to the Tradier streaming API
- Subscribes to all symbols in the active universe
- On every `trade` message, passes the raw payload to the parser

### 2. Parsing & Classification (`parsers/`)

**`options_flow_parser.py`** — produces an `OptionsFlowEvent`:

| Field | Description |
|---|---|
| `ticker` | Underlying symbol |
| `contract_type` | `CALL` or `PUT` |
| `strike` | Strike price |
| `expiry` | Expiration date string |
| `premium` | Total dollar premium (price × size × 100) |
| `trade_type` | `SWEEP`, `BLOCK`, `SPLIT` |
| `sentiment` | `BULLISH` or `BEARISH` (CALL=BULLISH, PUT=BEARISH) |
| `influence_tier` | `WHALE`, `INSTITUTIONAL`, `LARGE`, `RETAIL` |
| `conviction_score` | 0–1 float, immediate per-tick strength |
| `is_golden_sweep` | True if large sweep above ask |
| `timestamp` | UTC datetime of the tick |
| `dte` | Days to expiration |

**`bid_ask_classifier.py`** — determines if fill was at/above ask (aggressive buyer) or at/below bid (aggressive seller).

**`trade_type_detector.py`** — identifies sweep (multi-exchange), block (single large print), or split (same contract, rapid small prints).

### 3. Repetition Accumulation (`signals/repetition_accumulator.py`)

`RepetitionAccumulator` groups events by `ticker:contract_type:strike:expiry` key within a **rolling 30-minute window**.

An episode is emitted only when **both** thresholds are met:
- `trade_count >= 3`
- `total_premium >= $50,000`

**Alert Levels** (set on the episode before scoring):

| Level | Condition |
|---|---|
| `CONVICTION` | Premium ≥ $5M, OR accelerating + premium ≥ $1M |
| `STRONG_SIGNAL` | Premium ≥ $1M |
| `ALERT` | Premium ≥ $250K |
| `WATCH` | Below $250K but above min threshold |

**Acceleration detection**: the last 3 events all occurred within 60 seconds.

### 4. Flow Scoring (`signals/composite_signal_engine.py`)

`compute_flow_score(ep)` → 0–1 float:

```
flow_score = min(1.0,
    (total_premium / $10M) × 0.65   # premium weight (capped at $10M)
  + 0.15 if is_accelerating          # acceleration bonus
  + min(trade_count / 20, 0.20)      # repetition weight (capped at 20 trades)
)
```

### 5. Backtest Validation (`signals/backtest_validator.py`)

`get_backtest_score(ticker, contract_type, dte, influence_tier)` → 0–1 float.

Looks up historical win-rate for the same `(ticker, contract_type, DTE bucket, tier)` combination from the last 90 days of `flow_episodes` in Supabase.

**DTE Buckets**: `0-7`, `8-30`, `31-90`, `90+`

**Tier Baseline Win-Rates** (before Gaussian noise adjustment):

| Tier | Base Win-Rate |
|---|---|
| `WHALE` | 72% |
| `INSTITUTIONAL` | 63% |
| `LARGE` | 55% |
| `RETAIL` | 44% |

> ⚠️ Current implementation uses seeded pseudo-random values for demo consistency. Production should replace `_CACHE` population with a live Supabase aggregation query over `flow_episodes`.

### 6. Composite Score & Recommendation

```
composite_score = (flow_score × 0.60) + (backtest_score × 0.40)
```

**Recommendation logic**:

| Composite Score | Sentiment | Recommendation |
|---|---|---|
| ≥ 0.65 | BULLISH | **BUY** |
| ≥ 0.65 | BEARISH | **SELL** |
| < 0.65 | Any | **HOLD** |

`CompositeSignal` fields: `ticker`, `recommendation`, `composite_score`, `flow_score`, `backtest_score`, `reasoning` (human-readable string).

### 7. Persistence (`services/flow_store.py`)

Two Supabase tables are written:

| Table | Write Timing | Key Fields |
|---|---|---|
| `flow_events` | Batched every 5 seconds | All `OptionsFlowEvent` fields |
| `flow_episodes` | Immediately on qualifying signal | Episode summary + alert level |

#### ⚠️ Critical: Service Role Key Required

`flow_store.py` **must** use `SUPABASE_SERVICE_ROLE_KEY`. The anon/public key respects Row Level Security (RLS) and will cause every insert to fail with:

```
401 — {"code":"42501","message":"new row violates row-level security policy for table \"flow_episodes\""}
```

**Environment variable**: `SUPABASE_SERVICE_ROLE_KEY` (Railway → Variables). Never fall back to `SUPABASE_KEY`.

**Fix applied 2026-04-23**: Removed the `or os.environ.get("SUPABASE_KEY")` fallback from `_SUPABASE_KEY` initialization. The module now fails fast with a clear log warning if the service role key is absent, rather than silently using the anon key.

### 8. Real-Time Broadcast (`core/async_bus.py`)

Every qualifying signal is also published to the in-memory async bus on multiple channels (e.g., `websocket`, `db_writer`). WebSocket clients receive signals within milliseconds of the episode threshold being crossed.

---

## Key Scoring Formula Reference

```
flow_score      = min(1.0, (premium/$10M)×0.65 + accel×0.15 + min(trades/20, 0.20))
composite_score = flow_score×0.60 + backtest_score×0.40
BUY/SELL        → composite_score ≥ 0.65
HOLD            → composite_score < 0.65
```

---

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_URL` | Yes | Supabase project REST endpoint |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Server-side DB writes (bypasses RLS) |
| `TRADIER_API_KEY` | Yes | Tradier streaming API access |
| `SUPABASE_KEY` | No | Anon key — frontend only, never for backend writes |

---

## Known Issues & Backlog

- **Backtest validator** — currently uses seeded pseudo-random. Replace with live Supabase 90-day aggregation query before production launch.
- **Mid-cap screener** — `midcap_screener.py` exists but is not yet wired into the main pipeline accumulator filter.
- **`flow_event_buffer` loss on crash** — buffered events not yet flushed are lost on pod restart. Consider a Redis queue as intermediate buffer.

---

## Changelog

| Date | Change | File(s) |
|---|---|---|
| 2026-04-23 | Initial signal engine doc created | `docs/SIGNAL_ENGINE.md` |
| 2026-04-23 | Fix RLS 42501 error — removed anon key fallback in `flow_store.py` | `backend/services/flow_store.py` |
