# Cipher — Architecture & Data Flow

> Last updated: 2026-04-23 (Phase 4)

---

## Overview

Cipher is an institutional options flow intelligence platform. It monitors live Tradier WebSocket streams across 2,600+ symbols, classifies each trade tick, detects repetition patterns, and surfaces high-conviction signals to the frontend via WebSocket and persists them to Supabase.

---

## System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        Railway (Backend)                        │
│                                                                 │
│  main.py (FastAPI lifespan)                                     │
│    ├── stream_options_flow()      tradier_stream.py             │
│    │     ├── _get_session_token() Tradier REST                  │
│    │     ├── httpx SSE stream     Tradier Stream API            │
│    │     ├── parse_tradier_trade() parsers/options_flow_parser  │
│    │     │     └── [Phase 3] size==0 guard → returns None       │
│    │     ├── [LOG] every tick     Railway logs                  │
│    │     ├── RepetitionAccumulator signals/repetition_accumulator│
│    │     ├── [LOG] every signal   Railway logs                  │
│    │     └── bus.publish_all()    core/async_bus.py             │
│    │              │                                             │
│    │         AsyncEventBus (in-memory fan-out)                  │
│    │              ├── "signals"      → ws.py → WebSocket clients│
│    │              ├── "db_writer"    → flow_store.py → Supabase │
│    │              └── "signal_store" → signal_store.py → Supabase [Phase 4]
│    │                                                            │
│    ├── start_flow_writer()        services/flow_store.py        │
│    │     ├── bus.subscribe("db_writer")                         │
│    │     ├── persist_flow_episode() → flow_episodes             │
│    │     └── _flush_flow_events()  every 5s → flow_events       │
│    │                                                            │
│    ├── start_signal_store()       services/signal_store.py [Phase 4]
│    │     ├── bus.subscribe("signal_store")                      │
│    │     └── persist_signal_history() → signal_history          │
│    │                                                            │
│    └── _universe_refresh_loop()   every 24h                     │
│                                                                 │
│  FastAPI Routers                                                │
│    ├── /api/auth             auth.py                            │
│    ├── /api/flow/scan        flow.py   (currently mocked)       │
│    ├── /api/simulate         simulation.py                      │
│    ├── /ws/signals           ws.py     WebSocket + ping/pong    │
│    ├── /api/signals/composite     smart_signals.py              │
│    ├── /api/signals/list          smart_signals.py  [Phase 3]  │
│    └── /api/signals/history       smart_signals.py  [Phase 4]  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Supabase (PostgreSQL)                        │
│   flow_episodes · flow_events · options_universe_snapshots      │
│   options_universe_symbols · signal_history · auth.users        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Backend Signal Pipeline — Phase 4

```
Tradier SSE tick
  → parse_tradier_trade()
       └── [Phase 3] size guard: if size == 0 → return None (skip event)
  → RepetitionAccumulator.ingest()
       └── RepetitionEpisode produced when trades ≥ 3 AND premium ≥ $50K
  → build_composite(ep, accumulator)
       ├── compute_flow_score()              × 0.55 weight
       │     premium (capped $10M) + acceleration bonus + trade count
       ├── get_backtest_score()              × 0.35 weight
       │     historical win-rate by ticker/type/DTE/tier
       └── volume_weighted_premium_factor()  × 0.10 weight  [Phase 3]
             total_premium / (open_interest × 100), capped 0–1
             falls back to 0.5 neutral when OI unavailable
  → CompositeSignal { recommendation, composite_score, flow_score,
                      backtest_score, volume_premium_factor, reasoning }
  → bus.publish_all()
       ├── "signals"      → WebSocket clients
       ├── "db_writer"    → flow_store.py → flow_episodes + flow_events
       └── "signal_store" → signal_store.py → signal_history  [Phase 4]
```

### Composite Score Weights (Phase 3+)

| Component | Weight | Source |
|-----------|--------|--------|
| `flow_score` | 0.55 | Premium size, acceleration, trade count |
| `backtest_score` | 0.35 | Historical win-rate (ticker/type/DTE/tier) |
| `volume_premium_factor` | 0.10 | Premium relative to open interest |

---

## WebSocket Heartbeat (Phase 3 — fully resolved Phase 4)

Railway terminates idle TCP connections. The WS router runs a full ping/pong loop:

| Event | Details |
|-------|---------|
| Server → client ping | `{"type":"ping"}` every **25 seconds** |
| Client → server pong | `{"type":"pong"}` expected within **10 seconds** |
| Pong timeout | Server closes with code `1001`, logs warning |

**Phase 4:** `useSignalStream.ts` now fully handles `{"type":"ping"}` messages and responds with `{"type":"pong"}` — the TODO from Phase 3 is resolved.

---

## Signal History — Phase 4

### `signal_history` Table

Persists every composite signal emitted by the engine for historical replay and dashboard display.

```sql
CREATE TABLE signal_history (
  id              BIGSERIAL PRIMARY KEY,
  ticker          TEXT NOT NULL,
  recommendation  TEXT NOT NULL,           -- BUY / SELL / HOLD
  composite_score NUMERIC NOT NULL,
  flow_score      NUMERIC NOT NULL,
  backtest_score  NUMERIC NOT NULL,
  volume_premium_factor NUMERIC,
  reasoning       TEXT,
  contract_type   TEXT,                    -- CALL / PUT
  alert_level     TEXT,                    -- WATCH / ALERT / STRONG_SIGNAL / CONVICTION
  total_premium   NUMERIC,
  trade_count     INT,
  signal_ts       TIMESTAMPTZ DEFAULT now(),
  created_at      TIMESTAMPTZ DEFAULT now()
);
```

**Migration file:** `backend/migrations/003_signal_history.sql`

### `GET /api/signals/history`

New paginated endpoint for frontend signal history tab:

| Query Param | Type | Default | Description |
|-------------|------|---------|-------------|
| `page` | int | 1 | Page number (1-indexed) |
| `page_size` | int | 20 | Results per page (max 100) |
| `ticker` | string | — | Filter by ticker symbol |
| `recommendation` | string | — | `BUY` / `SELL` / `HOLD` |
| `min_score` | float | 0.0 | Minimum `composite_score` |

Response: `{ signals[], page, page_size, total }`

---

## Smart Signals Endpoints

### `GET /api/signals/list` (Phase 3)

| Query Param | Type | Default | Description |
|-------------|------|---------|-------------|
| `page` | int | 1 | Page number (1-indexed) |
| `page_size` | int | 20 | Results per page (max 100) |
| `direction` | string | — | `bullish` / `bearish` / `neutral` |
| `tier` | string | — | `whale` / `institutional` / `large` / `retail` |
| `min_conviction` | float | 0.0 | Minimum `composite_score` (0.0–1.0) |

### `GET /api/signals/composite/{ticker}` (Phase 2+)

Single-ticker composite endpoint. Response includes `volume_premium_factor` field.

---

## Live Data Pipeline — Step by Step

### Stage 1 — Symbol Universe (Startup)

| Step | What happens | DB table written |
|------|-------------|------------------|
| 1 | Load fresh snapshot from DB (< 24h) | read `options_universe_snapshots` |
| 2 | If stale: fetch from CBOE + Tradier validate + screen | — |
| 3 | Save validated snapshot | `options_universe_snapshots` |
| 4 | Upsert per-symbol quotes | `options_universe_symbols` |
| 5 | Stream starts with stream-eligible symbols (2,600+) | — |

Refreshes every **24 hours** in background.

---

### Stage 2 — Tradier Stream (Live, always-on)

```
Tradier SSE tick
  → parse_tradier_trade()          parse raw JSON into OptionsFlowEvent
       └── size == 0 → return None   [Phase 3 guard]
  → [LOG] tradier_stream logger    "[flow] AAPL CALL $180 2024-06-21 | prem=$250,000 ..."
  → RepetitionAccumulator.ingest() group by (ticker, strike, expiry, contract_type)
       └── if trades >= 3 AND premium >= $50,000:
             → RepetitionEpisode produced
             → [LOG] tradier_stream logger  "[signal] AAPL CALL alert=CONVICTION ..."
             → bus.publish_all(signal_dict)
```

---

### Stage 3 — Event Bus Fan-out

| Channel | Subscriber | What it does |
|---------|------------|-------------|
| `signals` | `ws.py` | Forwards to all connected WebSocket clients |
| `db_writer` | `flow_store.py` | Persists to `flow_episodes` + `flow_events` |
| `signal_store` | `signal_store.py` | Persists to `signal_history` [Phase 4] |

---

### Stage 4 — DB Persistence

#### `flow_store.py` — flow_episodes + flow_events

See specs.md § Flow Store for full schema and batching details.

#### `signal_store.py` — signal_history (Phase 4)

Subscribes to `signal_store` bus channel. Persists every composite signal row immediately (no batching — signals are low-frequency relative to ticks).

---

## Alert Level Logic

| Level | Criteria |
|-------|----------|
| `CONVICTION` | premium >= $5M, OR (accelerating AND premium >= $1M) |
| `STRONG_SIGNAL` | premium >= $1M |
| `ALERT` | premium >= $250K |
| `WATCH` | premium >= $50K (minimum threshold) |

---

## Where to Look for Signals

| What you want | Where to look |
|--------------|---------------|
| Raw flow ticks (live) | Railway logs → filter `[flow]` |
| Signal episodes (live) | Railway logs → filter `[signal]` |
| Persisted signal episodes | Supabase `flow_episodes` table |
| All raw ticks (persisted) | Supabase `flow_events` table |
| Signal history (paginated) | `GET /api/signals/history?page=1&min_score=0.65` |
| Simulation results | Supabase `simulation_results` table |
| WebSocket delivery | Browser devtools → WS frames on `/ws/signals` |
| Paginated signals list | `GET /api/signals/list?page=1&min_conviction=0.65` |

---

## ID Generation Contract

> **Rule:** Neither `flow_events` nor `flow_episodes` nor `signal_history` rows are sent with an `id` field.
> Postgres generates IDs server-side. Sending a client-generated `id` causes a 400 / schema mismatch error.

---

## Environment Variables Required

| Variable | Used by | Required |
|----------|---------|----------|
| `TRADIER_API_KEY` | tradier_stream.py | Yes (live mode) |
| `TRADIER_BASE_URL` | tradier_stream.py | Yes |
| `TRADIER_STREAM_URL` | tradier_stream.py | Yes |
| `SUPABASE_URL` | flow_store.py, signal_store.py, universe_store.py | Yes |
| `SUPABASE_SERVICE_ROLE_KEY` | flow_store.py, signal_store.py | **Yes — service role key, not anon key** |
| `SUPABASE_KEY` | universe_store.py | Yes (anon key for reads) |
| `SECRET_KEY` | auth.py | Yes |
| `ALGORITHM` | auth.py | Yes (default: HS256) |

---

## Known Issues / TODO

- `routers/flow.py` (`GET /api/flow/scan`) returns **mock data** — needs to be wired to `flow_events` table query
- `/api/signals/list` tier filter is pass-through (mock data) — wire to live accumulator query in Phase 5
