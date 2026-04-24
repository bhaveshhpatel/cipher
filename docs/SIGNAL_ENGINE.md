# Cipher Signal Engine — Reference Document

> **Living document.** Updated automatically as the signal pipeline evolves.
> Last updated: 2026-04-23 (Phase 4)

---

## Overview

The Cipher signal engine is a real-time options flow pipeline that:
1. **Ingests** raw trade ticks from the Tradier streaming API
2. **Parses & classifies** each tick (contract type, sentiment, influence tier, conviction)
3. **Accumulates** repeated flow on the same contract within a rolling window
4. **Scores** each qualifying episode using flow metrics + historical backtest win-rate
5. **Emits** a composite BUY / SELL / HOLD recommendation with structured reasoning
6. **Persists** raw events and signal episodes to Supabase (service role key required)
7. **Persists** every composite signal to `signal_history` for historical replay [Phase 4]
8. **Broadcasts** signals in real-time to all connected WebSocket clients via the async bus

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
│   ├── flow_store.py               # Supabase writer: flow_episodes + flow_events
│   ├── signal_store.py             # Supabase writer: signal_history [Phase 4]
│   ├── universe_screener.py        # Builds the active symbol universe
│   └── universe_store.py           # Persists universe data to Supabase
├── core/
│   ├── async_bus.py                # In-memory pub/sub fan-out for WebSocket delivery
│   └── auth.py                     # JWT auth middleware
├── migrations/
│   ├── 001_options_universe.sql    # Universe tables
│   ├── 002_flow_tables.sql         # flow_episodes + flow_events
│   └── 003_signal_history.sql      # signal_history [Phase 4]
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

### 3. Repetition Accumulation (`signals/repetition_accumulator.py`)

Groups events by `ticker:contract_type:strike:expiry` within a **rolling 30-minute window**.

Episode emitted only when **both** thresholds met:
- `trade_count >= 3`
- `total_premium >= $50,000`

**Alert Levels:**

| Level | Condition |
|---|---|
| `CONVICTION` | Premium ≥ $5M, OR accelerating + premium ≥ $1M |
| `STRONG_SIGNAL` | Premium ≥ $1M |
| `ALERT` | Premium ≥ $250K |
| `WATCH` | Below $250K but above min threshold |

### 4. Flow Scoring (`signals/composite_signal_engine.py`)

```
flow_score = min(1.0,
    (total_premium / $10M) × 0.65
  + 0.15 if is_accelerating
  + min(trade_count / 20, 0.20)
)
```

### 5. Backtest Validation (`signals/backtest_validator.py`)

Looks up historical win-rate for `(ticker, contract_type, DTE bucket, tier)` from last 90 days.

**DTE Buckets**: `0-7`, `8-30`, `31-90`, `90+`

**Tier Baseline Win-Rates:**

| Tier | Base Win-Rate |
|---|---|
| `WHALE` | 72% |
| `INSTITUTIONAL` | 63% |
| `LARGE` | 55% |
| `RETAIL` | 44% |

### 6. Composite Score & Recommendation

```
composite_score = (flow_score × 0.55) + (backtest_score × 0.35) + (volume_premium_factor × 0.10)
BUY/SELL  → composite_score ≥ 0.65
HOLD      → composite_score < 0.65
```

`CompositeSignal` fields: `ticker`, `recommendation`, `composite_score`, `flow_score`, `backtest_score`, `volume_premium_factor`, `reasoning`.

### 7. Persistence (`services/flow_store.py` + `services/signal_store.py`)

| Table | Writer | Write Timing |
|---|---|---|
| `flow_events` | `flow_store.py` | Batched every 5 seconds |
| `flow_episodes` | `flow_store.py` | Immediately on qualifying signal |
| `signal_history` | `signal_store.py` | Immediately on every composite signal [Phase 4] |

#### ⚠️ Critical: Service Role Key Required

Both `flow_store.py` and `signal_store.py` **must** use `SUPABASE_SERVICE_ROLE_KEY`. The anon key respects RLS and will cause every insert to fail with `42501`.

### 8. Real-Time Broadcast (`core/async_bus.py`)

Every qualifying signal is published to the in-memory async bus on three channels:
- `signals` → WebSocket clients
- `db_writer` → `flow_store.py`
- `signal_store` → `signal_store.py` [Phase 4]

---

## Key Scoring Formula Reference

```
flow_score      = min(1.0, (premium/$10M)×0.65 + accel×0.15 + min(trades/20, 0.20))
composite_score = flow_score×0.55 + backtest_score×0.35 + volume_premium_factor×0.10
BUY/SELL        → composite_score ≥ 0.65
HOLD            → composite_score < 0.65
```

---

## WebSocket Ping/Pong (Phase 4 — TODO resolved)

The frontend `useSignalStream.ts` hook now fully implements the ping/pong contract:
- Receives `{"type":"ping"}` from server
- Responds immediately with `{"type":"pong"}`
- Prevents Railway idle-connection kills

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
| 2026-04-23 | Phase 4: signal_history table + signal_store.py + GET /api/signals/history + useSignalHistory hook + SignalHistory component + History tab in dashboard. WS ping/pong TODO resolved in useSignalStream.ts. | Multiple |
| 2026-04-23 | Phase 3: volume_premium_factor (×0.10), composite weights updated (0.55/0.35/0.10), /api/signals/list endpoint, size==0 guard | Multiple |
| 2026-04-23 | Fix RLS 42501 error — removed anon key fallback in `flow_store.py` | `backend/services/flow_store.py` |
| 2026-04-23 | Initial signal engine doc created | `docs/SIGNAL_ENGINE.md` |
