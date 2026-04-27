# Cipher Signal Engine — Reference Document

> **Living document.** Updated as the signal pipeline evolves.
> Last updated: 2026-04-26 (Phase 5A swarm, C-019 dedup overhaul, Layer 1 registry, Layer 2 stream manager, Feature 4A tiers, registry prewarm, correct env var names)

---

## Overview

The Cipher signal engine is a real-time options flow pipeline that:

1. **Resolves** a tier-filtered OCC symbol universe at startup via DB snapshot or Tradier validation
2. **Ingests** raw trade ticks from the Tradier WebSocket stream via a pool of parallel workers
3. **Parses & classifies** each tick (contract type, sentiment, influence tier, conviction)
4. **Deduplicates** cross-exchange OPRA prints (C-019) before any downstream processing
5. **Accumulates** repeated flow on the same contract within a rolling 30-minute window
6. **Scores** each qualifying episode using flow metrics + historical backtest win-rate + volume/OI ratio
7. **Runs** a multi-agent AI swarm (3/6/9/12 Groq agents) for BUY / SELL / HOLD majority vote
8. **Emits** a `CompositeSignal` with structured recommendation, scores, and swarm verdict
9. **Persists** flow events and signal episodes to Supabase (service role key required)
10. **Broadcasts** signals in real-time to all connected WebSocket clients via the async event bus

---

## Module Map

```
backend/
├── main.py                         # FastAPI app + lifespan, CORS regex, _registry_prewarm_loop
├── parsers/
│   ├── options_flow_parser.py      # Raw tick → OptionsFlowEvent (C-015, C-018)
│   ├── bid_ask_classifier.py       # ABOVE_ASK / AT_ASK / MID / AT_BID / BELOW_BID
│   └── trade_type_detector.py      # SWEEP / BLOCK / SPLIT / SINGLE
├── signals/
│   ├── repetition_accumulator.py   # Rolling-window episode builder + alert levels
│   ├── composite_signal_engine.py  # 3-component scoring → CompositeSignal
│   ├── backtest_validator.py       # Historical win-rate lookup by ticker/type/DTE/tier
│   └── midcap_screener.py          # Mid-cap universe filter (not yet wired into pipeline)
├── simulation/
│   ├── swarm_engine.py             # 12-agent Groq LLM swarm (Phase 5A)
│   └── ensemble_runner.py          # Majority-vote aggregator → EnsembleResult
├── execution/
│   └── trade_executor.py           # Tradier order placement (paper + live) — not yet in signal path
├── services/
│   ├── symbol_registry.py          # Layer 1 — OCC contract map, get_oi_map(), refresh_loop()
│   ├── stream_manager.py           # Layer 2 — StreamManager, worker pool, cold-start stagger
│   ├── stream_worker.py            # Layer 2 — per-worker SSE session, B-008 stats helpers
│   ├── tradier_stream.py           # Pipeline orchestrator: parse → dedup → accumulate → signal
│   ├── flow_store.py               # Layer 5 — Supabase writer: flow_episodes + flow_events
│   ├── signal_store.py             # Supabase writer: signal_history (+ Phase 5A swarm fields)
│   ├── tier_engine.py              # Feature 4A — dynamic T1/T2/T3 classification
│   ├── symbols_loader.py           # CBOE fetch + Tradier validation + screening
│   └── universe_store.py           # Snapshot persistence + tier_map load/upsert
├── utils/
│   ├── dedup.py                    # Layer 4 — DedupCache TTL=5s, sweep_win=8s (C-019)
│   └── tradier_client.py           # Tradier REST client, Semaphore(3) B-022, 429 handler B-023
├── core/
│   ├── async_bus.py                # In-memory pub/sub fan-out
│   └── auth.py                     # JWT auth middleware
└── migrations/                     # 001–012
    ├── 001_options_universe.sql
    ├── 002_flow_tables.sql
    ├── 003_signal_history.sql
    ├── 004_swarm_fields.sql
    ├── 010_tier_column.sql
    ├── 011_tier_thresholds.sql
    └── 012_tier_admin.sql
```

---

## Pipeline — Step by Step

### 0. Startup: Universe + Registry

Before the stream starts, `main.py` lifespan runs a **blocking** startup sequence:

```
1. _resolve_startup_universe()
     ├── Fresh DB snapshot (< 24h) → stream starts immediately
     ├── Tradier CBOE fetch + validate + screen → saves to DB
     └── Stale DB snapshot or SEED_SYMBOLS fallback
2. init_registry(watchlist, tier_map)   — Layer 1 init
3. registry.build()                     — loads full OCC contract set (blocks until done)
4. _stamp_oi(quotes, oi_map)            — stamps avg chain OI onto SymbolQuote objects
5. assign_tiers(quotes)                 — OI-informed T1/T2/T3 classification
6. registry.set_tier_map(tier_map)      — wires final tier map into registry
7. universe_store.upsert_symbol_quotes()— open_interest + tier written to DB
```

Then 6 background tasks are spawned (see lifespan task table in ARCHITECTURE.md).

### 1. Layer 1 — Symbol Registry (`services/symbol_registry.py`)

- Pre-loads OCC contract metadata into an in-memory dict at startup and after every `registry.build()`
- Per-tick lookup is O(1): `registry["TSLA260424C00375000"]` → `{ticker, strike, expiry, type, DTE, tier}`
- No regex, no API call, no per-tick latency
- Refreshes every `REGISTRY_REFRESH_MINS` (default 30 min); on expiry days refreshes every 15 min
- **Pre-warm loop** (`_registry_prewarm_loop` in `main.py`): fires every weekday at 09:15 ET, rebuilds the full OCC set 15 minutes before market open. Skips weekends. Non-fatal on error.
- Per-tier contract filtering: ATM strike range and max DTE are set by tier
  - Tier 1 (liquid): ATM ±20%, max DTE 90
  - Tier 2 (mid-cap): ATM ±15%, max DTE 60
  - Tier 3 (default): ATM ±10%, max DTE 30
- `get_oi_map()` returns `{symbol: avg_oi}` after each build — used by `_stamp_oi()` in lifespan

### 2. Layer 2 — Stream Manager (`services/stream_manager.py` + `stream_worker.py`)

- Spawns `ceil(registry.size() / 500)` workers (typically 20–40) to cover the full OCC symbol set
- Tradier caps each WebSocket session at ~500 symbols
- **B-021 cold-start stagger**: worker `i` sleeps `i × 0.200s` before its first session token request. Reconnects do NOT re-apply the stagger.
- **B-022 token semaphore**: `asyncio.Semaphore(3)` in `tradier_client.py` caps concurrent `/markets/events/session` calls at 3
- **B-023 429 handler**: if Tradier returns HTTP 429, reads `Retry-After` header (default 10s), sleeps, then retries
- **B-008 stats helpers**: `_inc_global_error()` and `_inc_global_reconnect()` in `stream_worker.py` write into `tradier_stream._stats` so `/health/stream` returns real values instead of zeros
- On registry refresh: diffs old vs new symbol set, restarts only affected workers

### 3. Layer 3 — Parser (`parsers/options_flow_parser.py`)

**`parse_tradier_trade()`** — raw Tradier tick → `OptionsFlowEvent`:

| Field | Description |
|---|---|
| `ticker` | Underlying symbol (from registry enrichment, not raw OCC parse) |
| `contract_type` | `CALL` or `PUT` |
| `strike` | Strike price |
| `expiry` | Expiration date string |
| `dte` | Days to expiration |
| `fill_price` | `tick["last"]` primary → `tick.get("price")` fallback → bid/ask mid (C-015) |
| `size` | Contract count. `size == 0` → skip (return None) |
| `premium` | `fill_price × size × 100` |
| `bid` / `ask` | From tick; synthetic 1% spread applied when both are 0 (C-018) |
| `is_synthetic_quote` | `True` when synthetic spread applied |
| `sentiment` | `BULLISH` (CALL) or `BEARISH` (PUT) |
| `influence_tier` | `WHALE / INSTITUTIONAL / LARGE / RETAIL` |
| `conviction_score` | 0–1 float, per-tick strength |
| `is_golden_sweep` | `True` if large SWEEP above ask |
| `trade_type` | `SWEEP / BLOCK / SPLIT / SINGLE` |
| `timestamp` | UTC datetime of the tick |

OCC symbol regex uses `{1,10}` chars for ticker to handle long symbols. Registry enrichment overrides OCC-parsed fields with pre-validated chain metadata.

### 4. Layer 4 — Deduplication (`utils/dedup.py` — C-019)

A single trade prints on CBOE, MIAX, PHLX, AMEX within an OPRA reporting window. Without dedup, every exchange copy writes a DB row and inflates signal strength.

**Module-level singleton:** `flow_dedup = DedupCache(ttl=5.0, sweep_window=8.0)`

**Dedup key:** `(occ_symbol, size, round(fill_price, 1))`
- TTL=5s covers worst-case PHLX/MIAX lag (2–5s)
- Fill key rounded to 1dp absorbs ±$0.01 feed rounding across exchanges
- No `int(ts//2)` time-bucket — pure first-seen TTL comparison (bucket boundary bug eliminated)

**Sweep detection:**
- Exchange field passed via `trade_payload["exch"]` or `["exchange"]` fallback
- 3+ unique exchanges seen within 8s → `trade_type = SWEEP`, `exchange_count` populated

**C-019 fix summary (2026-04-24):**

| Bug | Before | After |
|-----|--------|-------|
| TTL | 2s | 5s |
| Sweep window | 5s | 8s |
| Bucket boundary | `int(ts//2)` — CBOE@1.99s and MIAX@2.01s landed in different buckets | Pure first-seen TTL |
| Fill key precision | 2dp | 1dp |
| Dedup wired? | `flow_dedup` built but **never imported or called** in `_process_trade()` | Fixed — wired into hot path |
| Exchange passed? | Exchange field never passed to `is_duplicate()` — sweep never fired | Fixed via `"exch"/"exchange"` fallback |

Observability: `dedup_stats()` and `get_exchange_count()` added; merged into `/health/stream` response.

### 5. Repetition Accumulation (`signals/repetition_accumulator.py`)

Groups events by `ticker:contract_type:strike:expiry` within a **rolling 30-minute window**.

Episode emitted only when **both** thresholds met:
- `trade_count >= 3`
- `total_premium >= $50,000`

**Alert Levels:**

| Level | Condition |
|---|---|
| `CONVICTION` | Premium ≥ $5M, OR accelerating + premium ≥ $1M |
| `STRONG_SIGNAL` | Premium ≥ $1M (non-accelerating) |
| `ALERT` | Premium ≥ $250K |
| `WATCH` | Below $250K but above min threshold |

### 6. Composite Scoring (`signals/composite_signal_engine.py`)

```
flow_score = min(1.0,
    (total_premium / $10M) × 0.65
  + 0.15 if is_accelerating
  + min(trade_count / 20, 0.20)
)

volume_premium_factor = min(1.0, total_premium / (open_interest × 100))
  → falls back to 0.5 (neutral) when OI is unavailable
  → do NOT treat 0.5 as a signal

composite_score = (flow_score × 0.55) + (backtest_score × 0.35) + (volume_premium_factor × 0.10)

BUY / SELL  → composite_score ≥ 0.65
HOLD        → composite_score < 0.65
```

**Score weight history:**

| Phase | Formula |
|-------|---------|
| Phase 2 | `flow × 0.60 + backtest × 0.40` |
| Phase 3+ (current) | `flow × 0.55 + backtest × 0.35 + volume_premium × 0.10` |

### 7. Backtest Validation (`signals/backtest_validator.py`)

Looks up historical win-rate for `(ticker, contract_type, DTE bucket, tier)` from last 90 days.

**DTE Buckets:** `0–7`, `8–30`, `31–90`, `90+`

**Tier Baseline Win-Rates (seeded — replace with live Supabase aggregation before production launch):**

| Tier | Base Win-Rate |
|---|---|
| `WHALE` | 72% |
| `INSTITUTIONAL` | 63% |
| `LARGE` | 55% |
| `RETAIL` | 44% |

### 8. AI Swarm (`simulation/swarm_engine.py` + `ensemble_runner.py`) — Phase 5A

- **Provider:** Groq `llama-3.3-70b-versatile` via OpenAI-compatible client
- **Agent counts:** 3, 6, 9, or 12 — set via `SWARM_N_AGENTS` env var, snaps to nearest valid
- **Agent roles:**
  - Tier 1 (1–6): Momentum Trader, Contrarian Analyst, Fundamental Analyst, Technical Analyst, Macro Strategist, Risk Manager
  - Tier 2 (7–9): Options Flow Specialist, Quant/Statistical Arb, Sentiment Analyst
  - Tier 3 (10–12): Sector Rotation Strategist, Volatility Trader, Dark Pool/Tape Reader
- **Verdict format per agent:** `VERDICT: BUY|SELL|HOLD`, `REASONING: ...`, `CONFIDENCE: 0.0–1.0`
- **Ensemble:** majority vote → `EnsembleResult` with `bull_votes`, `bear_votes`, `hold_votes`, `confidence`, per-agent `name` field
- **Fallback:** all agents return HOLD when `GROQ_API_KEY` not set

### 9. Persistence (`services/flow_store.py` + `services/signal_store.py`)

| Table | Writer | Write Timing | Notes |
|---|---|---|---|
| `flow_episodes` | `flow_store.py` | Batched — flush every 500ms OR 100 rows | Live data (82k+ rows). **Not** `flow_events`. |
| `flow_events` | `flow_store.py` | Same batch | `expiry` column is nullable |
| `signal_history` | `signal_store.py` | On every qualifying `CompositeSignal` | Includes all Phase 5A swarm fields |

**Layer 5 fix (C-018):** `_FLUSH_INTERVAL` was 5s (spec: 500ms). Fixed to `0.5s` + `_FLUSH_MAX_ROWS=100` early-flush.

#### ⚠️ Critical: Service Role Key Required

`flow_store.py` and `signal_store.py` **must** use `SUPABASE_SERVICE_KEY` (the env var name in `config.py`). The anon key respects RLS and will fail every insert with `42501`.

### 10. Real-Time Broadcast (`core/async_bus.py`)

Every qualifying signal is published on three channels:

| Channel | Subscriber | Purpose |
|---------|------------|--------|
| `signals` | `ws.py` | Live WebSocket delivery to clients |
| `db_writer` | `flow_store.py` | Batched Supabase write (Layer 5) |
| `signal_writer` | `signal_store.py` | `signal_history` persistence |

---

## `signal_history` Schema (Phase 5A)

```sql
CREATE TABLE signal_history (
  id                    BIGSERIAL PRIMARY KEY,
  ticker                TEXT NOT NULL,
  recommendation        TEXT NOT NULL,          -- BUY / SELL / HOLD
  composite_score       NUMERIC NOT NULL,
  flow_score            NUMERIC NOT NULL,
  backtest_score        NUMERIC NOT NULL,
  volume_premium_factor NUMERIC,
  reasoning             TEXT,
  contract_type         TEXT,
  alert_level           TEXT NOT NULL,
  sentiment             TEXT NOT NULL,
  direction             TEXT NOT NULL,
  influence_tier        TEXT NOT NULL,
  premium               NUMERIC NOT NULL,
  trade_type            TEXT NOT NULL,
  is_golden_sweep       BOOLEAN NOT NULL DEFAULT false,
  total_premium         NUMERIC,
  trade_count           INT,
  swarm_direction       TEXT,                    -- Phase 5A
  swarm_confidence      NUMERIC,                 -- Phase 5A
  swarm_agents          JSONB,                   -- Phase 5A: [{name, verdict, reasoning, confidence}]
  swarm_bull_votes      INT,                     -- Phase 5A
  swarm_bear_votes      INT,                     -- Phase 5A
  swarm_hold_votes      INT,                     -- Phase 5A
  signal_ts             TIMESTAMPTZ DEFAULT now(),
  created_at            TIMESTAMPTZ DEFAULT now()
);
```

---

## Feature 4A — Dynamic Tier Classification

Tier determines which OCC contracts are loaded into the registry (ATM range + max DTE) and feeds into `backtest_score` as a dimension.

| Tier | Min Avg Volume | ATM Strike Range | Max DTE |
|------|---------------|-----------------|--------|
| 1 (Liquid) | ≥ 20M | ±20% | 90 |
| 2 (Mid-cap) | ≥ 2M | ±15% | 60 |
| 3 (Default) | ≥ 500K | ±10% | 30 |

- Thresholds stored in `tier_thresholds` table (single `is_active=true` row), cached for 300s
- Admin whitelist (`TIER_ADMIN_WHITELIST` env var) forces symbols to Tier 1 regardless of metrics
- All 3 conditions (volume + price + OI) required for T1/T2 classification — OI grace path removed
- `assign_tiers()` called twice at startup: preliminary pass (OI=0) + final pass after `registry.build()`

---

## WebSocket Protocol

| Message | Direction | Details |
|---------|-----------|--------|
| Signal JSON | Server → Client | Live `CompositeSignal` episode |
| `{"type":"ping"}` | Server → Client | Every 25 seconds |
| `{"type":"pong"}` | Client → Server | Must respond within 10 seconds |
| Code `4001` | Server closes | Invalid / expired JWT on connect |
| Code `1001` | Server closes | Pong timeout (Railway idle-connection kill prevention) |

---

## Key Scoring Formula Reference

```
flow_score      = min(1.0, (premium/$10M)×0.65 + accel×0.15 + min(trades/20, 0.20))
backtest_score  = historical win-rate for (ticker, type, DTE bucket, tier)
vol_prem_factor = min(1.0, total_premium / (open_interest × 100))  — fallback 0.5
composite_score = flow_score×0.55 + backtest_score×0.35 + vol_prem_factor×0.10
BUY / SELL      → composite ≥ 0.65
HOLD            → composite < 0.65
```

---

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_URL` | Yes | Supabase project REST endpoint |
| `SUPABASE_SERVICE_KEY` | **Yes** | Server-side DB writes — bypasses RLS. Env var name in `config.py`. |
| `SUPABASE_KEY` | Yes | Anon key — read-only / frontend; never for backend writes |
| `TRADIER_API_KEY` | Yes | Tradier streaming API access |
| `TRADIER_BASE_URL` | Yes | Tradier REST base URL |
| `TRADIER_STREAM_URL` | Yes | Tradier WebSocket stream URL |
| `TRADIER_ACCOUNT_ID` | Yes | Required for paper/live trade execution |
| `SECRET_KEY` | Yes | JWT signing key |
| `ALGORITHM` | Yes | JWT algorithm (default: HS256) |
| `GROQ_API_KEY` | Yes | Groq API for swarm. HOLD fallback when absent. |
| `SWARM_N_AGENTS` | No | Swarm agent count (default: 6, valid: 3/6/9/12) |
| `REGISTRY_REFRESH_MINS` | No | OCC registry refresh interval (default: 30) |
| `REGISTRY_MAX_DTE` | No | Max DTE for contract loading (default: 90) |
| `TIER_ADMIN_WHITELIST` | No | Comma-separated tickers forced to Tier 1 |
| `TIER_THRESHOLD_CACHE_TTL_S` | No | Tier threshold cache TTL (default: 300) |
| `STREAM_WORKER_STARTUP_DELAY_S` | No | Per-worker cold-start stagger (default: 0.2) |
| `TRADIER_SESSION_MAX_CONCURRENCY` | No | Token semaphore size (default: 3) |
| `TRADIER_SESSION_429_DEFAULT_SLEEP_S` | No | 429 retry sleep when Retry-After absent (default: 10) |
| `CORS_ALLOWED_ORIGINS` | No | Comma-separated extra origins for CORS regex |

---

## Known Issues & Backlog

- **Backtest validator** — currently uses seeded pseudo-random win-rates. Replace with live Supabase 90-day aggregation query before production launch (B-004 area).
- **Mid-cap screener** — `midcap_screener.py` exists but is not confirmed wired into the main pipeline accumulator filter (B-016).
- **Trade executor** — `execution/trade_executor.py` is built and tested but not connected to the signal output path (B-009).
- **`flow_event_buffer` loss on crash** — events buffered in memory are lost on pod restart. Redis queue (B-011) would mitigate this.
- **Frontend WS pong** — frontend must implement `{"type":"pong"}` response within 10s (B-026).

---

## Changelog

| Date | Change | File(s) |
|---|---|---|
| 2026-04-26 | Full rebuild — added Layer 1 registry, Layer 2 stream manager, C-019 dedup overhaul, Phase 5A swarm, Feature 4A tiers, OI-gated two-pass startup, registry prewarm, correct env var names (`SUPABASE_SERVICE_KEY`), Layer 5 flush fix, B-008/021/022/023 stream hardening. | `docs/SIGNAL_ENGINE.md` |
| 2026-04-23 | Phase 4: `signal_history` table + `signal_store.py` + `GET /api/signals/history` + WS ping/pong resolved. | Multiple |
| 2026-04-23 | Phase 3: `volume_premium_factor` (×0.10), composite weights (0.55/0.35/0.10), `/api/signals/list`, `size==0` guard. | Multiple |
| 2026-04-23 | Fix RLS 42501 — removed anon key fallback in `flow_store.py`. | `backend/services/flow_store.py` |
| 2026-04-23 | Initial signal engine doc created. | `docs/SIGNAL_ENGINE.md` |
