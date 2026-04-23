# Cipher — Architecture & Data Flow

> Last updated: 2026-04-23 (Phase 3)

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
│    │              ├── "signals"   → ws.py → WebSocket clients   │
│    │              └── "db_writer" → flow_store.py → Supabase    │
│    │                                                            │
│    ├── start_flow_writer()        services/flow_store.py        │
│    │     ├── bus.subscribe("db_writer")                         │
│    │     ├── persist_flow_episode() → flow_episodes             │
│    │     └── _flush_flow_events()  every 5s → flow_events       │
│    │                                                            │
│    └── _universe_refresh_loop()   every 24h                     │
│                                                                 │
│  FastAPI Routers                                                │
│    ├── /api/auth        auth.py                                 │
│    ├── /api/flow/scan   flow.py   (currently mocked)            │
│    ├── /api/simulate    simulation.py                           │
│    ├── /ws/signals      ws.py     WebSocket + heartbeat         │
│    ├── /api/signals/composite  smart_signals.py                 │
│    └── /api/signals/list       smart_signals.py  [Phase 3]     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Supabase (PostgreSQL)                        │
│   flow_episodes · flow_events · options_universe_snapshots      │
│   options_universe_symbols · auth.users                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## Backend Signal Pipeline — Phase 3

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
  → bus.publish_all() → WebSocket clients + Supabase
```

### Composite Score Weights (Phase 3)

| Component | Weight | Source |
|-----------|--------|--------|
| `flow_score` | 0.55 | Premium size, acceleration, trade count |
| `backtest_score` | 0.35 | Historical win-rate (ticker/type/DTE/tier) |
| `volume_premium_factor` | 0.10 | Premium relative to open interest |

> **Phase 2 weights were** `flow × 0.60 + backtest × 0.40`. Phase 3 adds the OI-relative conviction filter.

---

## WebSocket Heartbeat (Phase 3)

Railway terminates idle TCP connections. The WS router now runs a full ping/pong loop:

| Event | Details |
|-------|---------|
| Server → client ping | `{"type":"ping"}` every **25 seconds** |
| Client → server pong | `{"type":"pong"}` expected within **10 seconds** |
| Pong timeout | Server closes with code `1001`, logs warning |

Frontend must handle `{"type":"ping"}` messages and respond with `{"type":"pong"}`.

---

## Smart Signals Endpoint — Phase 3

### `GET /api/signals/list`

New paginated, filterable endpoint:

| Query Param | Type | Default | Description |
|-------------|------|---------|-------------|
| `page` | int | 1 | Page number (1-indexed) |
| `page_size` | int | 20 | Results per page (max 100) |
| `direction` | string | — | `bullish` / `bearish` / `neutral` |
| `tier` | string | — | `whale` / `institutional` / `large` / `retail` |
| `min_conviction` | float | 0.0 | Minimum `composite_score` (0.0–1.0) |

Response includes `signals[]`, `page`, `page_size`, `total`.

### `GET /api/signals/composite/{ticker}`

Unchanged single-ticker endpoint. Response now includes `volume_premium_factor` field.

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

**Every raw tick is logged.** Every signal episode is also logged. Both visible in Railway logs immediately.

---

### Stage 3 — Event Bus Fan-out

The `AsyncEventBus` (in-memory) delivers each signal to all registered channel subscribers:

| Channel | Subscriber | What it does |
|---------|------------|-------------|
| `signals` | `ws.py` | Forwards to all connected WebSocket clients (frontend) |
| `db_writer` | `flow_store.py` | Persists to Supabase |

---

### Stage 4 — DB Persistence (`flow_store.py`)

#### Supabase Key — Critical Rule

> `flow_store.py` uses **only** `SUPABASE_SERVICE_ROLE_KEY`. This key bypasses Row Level Security (RLS).
> The anon key (`SUPABASE_KEY`) respects RLS and will cause **every insert** to fail with `42501`.
> There is **no fallback** — if the service role key is missing the writer logs a warning and exits.
> See `docs/FIXES.md` fix C-010 for background.

#### `flow_episodes` table
Written **immediately** on every signal episode that crosses the repetition threshold.

| Column | Source |
|--------|--------|
| `ticker` | episode.ticker |
| `direction` | REPEAT_BUY / REPEAT_SELL |
| `contract_type` | CALL / PUT |
| `strike` | episode.strike |
| `expiry` | episode.expiry |
| `total_premium` | sum of all episode premiums |
| `trade_count` | number of trades in window |
| `alert_level` | WATCH / ALERT / STRONG_SIGNAL / CONVICTION |
| `is_accelerating` | true if last 3 trades within 60s |
| `seed_episode` | human-readable summary string |
| `signal_ts` | timestamp of triggering tick |
| `created_at` | UTC insert time (Postgres default) |

> `id` is **never sent** — Postgres generates it as `bigserial`.

#### `flow_events` table
Written in **batches every 5 seconds** (buffered to avoid per-tick DB hammering).

| Column | Source |
|--------|--------|
| `ticker` | ev.ticker |
| `contract_type` | CALL / PUT |
| `strike` | ev.strike |
| `expiry` | ev.expiry |
| `premium` | ev.premium |
| `trade_type` | SWEEP / BLOCK / SPLIT / SINGLE |
| `sentiment` | BULLISH / BEARISH / NEUTRAL |
| `influence_tier` | WHALE / INSTITUTIONAL / LARGE / RETAIL |
| `conviction_score` | 0.0–1.0 |
| `is_golden_sweep` | bool |
| `created_at` | UTC insert time (Postgres default) |

> `id` is **never sent** — Postgres generates it as `uuid` via `DEFAULT gen_random_uuid()`.

---

## Alert Level Logic

| Level | Criteria |
|-------|----------|
| `CONVICTION` | premium >= $5M, OR (accelerating AND premium >= $1M) |
| `STRONG_SIGNAL` | premium >= $1M |
| `ALERT` | premium >= $250K |
| `WATCH` | premium >= $50K (minimum threshold) |

---

## After Simulation Run — Tables Populated

Simulation is triggered via `POST /api/simulate`.

| Table | Populated by | Contains |
|-------|-------------|----------|
| `simulation_results` | `simulation.py` router | Per-run output: PnL, win rate, trades |
| `backtest_results` | backtest runner | Per-symbol historical stats |
| `paper_trades` | paper trading module | Simulated entry/exit records |

---

## Where to Look for Signals

| What you want | Where to look |
|--------------|---------------|
| Raw flow ticks (live) | Railway logs → filter `[flow]` |
| Signal episodes (live) | Railway logs → filter `[signal]` |
| Persisted signal episodes | Supabase `flow_episodes` table |
| All raw ticks (persisted) | Supabase `flow_events` table |
| Simulation results | Supabase `simulation_results` table |
| WebSocket delivery | Browser devtools → WS frames on `/ws/signals` |
| Paginated signals list | `GET /api/signals/list?page=1&min_conviction=0.65` |

---

## ID Generation Contract

> **Rule:** Neither `flow_events` nor `flow_episodes` rows are sent with an `id` field.
> Postgres generates IDs server-side (`uuid` for `flow_events`, `bigserial` for `flow_episodes`).
> Sending a client-generated `id` causes a 400 / schema mismatch error.
> This applies to all future tables unless explicitly noted otherwise.

---

## Environment Variables Required

| Variable | Used by | Required |
|----------|---------|----------|
| `TRADIER_API_KEY` | tradier_stream.py | Yes (live mode) |
| `TRADIER_BASE_URL` | tradier_stream.py | Yes |
| `TRADIER_STREAM_URL` | tradier_stream.py | Yes |
| `SUPABASE_URL` | flow_store.py, universe_store.py | Yes |
| `SUPABASE_SERVICE_ROLE_KEY` | flow_store.py | **Yes — service role key, not anon key** |
| `SUPABASE_KEY` | universe_store.py | Yes (anon key for reads) |
| `SECRET_KEY` | auth.py | Yes |
| `ALGORITHM` | auth.py | Yes (default: HS256) |

---

## Known Issues / TODO

- `routers/flow.py` (`GET /api/flow/scan`) returns **mock data** — needs to be wired to `flow_events` table query
- `flow_events` and `flow_episodes` tables must exist in Supabase with columns matching schemas above
- RLS policies on both tables must permit `service_role` inserts (or be disabled for the service role)
- `/api/signals/list` tier filter is pass-through (mock data) — wire to live accumulator query in Phase 4
