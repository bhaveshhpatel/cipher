# Cipher — Architecture & Data Flow

> Last updated: 2026-04-23

---

## Overview

Cipher is an institutional options flow intelligence platform. It monitors live Tradier WebSocket streams across 2,600+ symbols, classifies each trade tick, detects repetition patterns, and surfaces high-conviction signals to the frontend via WebSocket and (now) persists them to Supabase.

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
│    │     ├── [LOG] every tick     Railway logs                  │
│    │     ├── RepetitionAccumulator signals/repetition_accumulator│
│    │     ├── [LOG] every signal   Railway logs                  │
│    │     └── bus.publish_all()    core/async_bus.py             │
│    │              │                                             │
│    │         AsyncEventBus (in-memory fan-out)                  │
│    │              ├── "signals"   → ws.py → WebSocket clients   │
│    │              └── "db_writer" → flow_store.py → Supabase    │
│    │                                                            │
│    ├── start_flow_writer()        services/flow_store.py  [NEW] │
│    │     ├── bus.subscribe("db_writer")                         │
│    │     ├── persist_composite_signal() → composite_signals     │
│    │     └── _flush_flow_events()  every 5s → flow_events       │
│    │                                                            │
│    └── _universe_refresh_loop()   every 24h                     │
│                                                                 │
│  FastAPI Routers                                                │
│    ├── /api/auth        auth.py                                 │
│    ├── /api/flow/scan   flow.py   (currently mocked — TODO)     │
│    ├── /api/simulate    simulation.py                           │
│    ├── /ws/signals      ws.py     WebSocket                     │
│    └── /api/signals     smart_signals.py                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Supabase (PostgreSQL)                        │
└─────────────────────────────────────────────────────────────────┘
```

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
  → [LOG] tradier_stream logger    "[flow] AAPL CALL $180 2024-06-21 | prem=$250,000 ..."
  → RepetitionAccumulator.ingest() group by (ticker, strike, expiry, contract_type)
       └── if trades >= 3 AND premium >= $50,000:
             → RepetitionEpisode produced
             → [LOG] tradier_stream logger  "[signal] AAPL CALL alert=CONVICTION ..."
             → bus.publish_all(signal_dict)
```

**Every raw tick is now logged.** Every signal episode is also logged. Both are visible in Railway logs immediately.

---

### Stage 3 — Event Bus Fan-out

The `AsyncEventBus` (in-memory) delivers each signal to all registered channel subscribers:

| Channel | Subscriber | What it does |
|---------|------------|-------------|
| `signals` | `ws.py` | Forwards to all connected WebSocket clients (frontend) |
| `db_writer` | `flow_store.py` | Persists to Supabase (NEW) |

---

### Stage 4 — DB Persistence (`flow_store.py`) — NEW

#### `composite_signals` table
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
| `created_at` | UTC insert time |

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
| `created_at` | UTC insert time |

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
| Persisted signals | Supabase `composite_signals` table |
| All raw ticks (persisted) | Supabase `flow_events` table |
| Simulation results | Supabase `simulation_results` table |
| WebSocket delivery | Browser devtools → WS frames on `/ws/signals` |

---

## Known Issues / TODO

- `routers/flow.py` (`GET /api/flow/scan`) returns **mock data** — needs to be wired to `flow_events` table query
- `flow_events` table must exist in Supabase with columns matching `flow_store.py` schema above
- `composite_signals` table must exist in Supabase with columns matching schema above
- RLS policies on both tables must allow service role inserts
- `flow_store.py` uses `SUPABASE_SERVICE_ROLE_KEY` (preferred) or `SUPABASE_KEY` — ensure env var is set in Railway

---

## Environment Variables Required

| Variable | Used by | Required |
|----------|---------|----------|
| `TRADIER_API_KEY` | tradier_stream.py | Yes (live mode) |
| `TRADIER_BASE_URL` | tradier_stream.py | Yes |
| `TRADIER_STREAM_URL` | tradier_stream.py | Yes |
| `SUPABASE_URL` | flow_store.py, universe_store.py | Yes |
| `SUPABASE_SERVICE_ROLE_KEY` | flow_store.py | Yes (for DB writes) |
| `SUPABASE_KEY` | universe_store.py | Yes |
| `SECRET_KEY` | auth.py | Yes |
| `ALGORITHM` | auth.py | Yes (default: HS256) |
