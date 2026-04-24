# Cipher — Claude Context File

> Last updated: 2026-04-24 (Phase 5A)
> This file is the authoritative AI-assistant context document for the Cipher codebase.
> Keep it updated after every phase so future sessions have full project context.

---

## What Is Cipher?

**Cipher** is an institutional options flow intelligence platform with the tagline *"Decode the Market."* It detects real-time whale/institutional options flow, scores signals using a composite engine, runs multi-agent AI swarm simulations to generate BUY/SELL/HOLD verdicts, and persists all signals to Supabase for historical querying.

Built with:
- **Backend:** FastAPI (Python 3.11) on Railway
- **Frontend:** Next.js 14, TypeScript, Tailwind CSS on Vercel
- **Database:** Supabase (PostgreSQL)
- **Data source:** Tradier WebSocket SSE stream (~2,600+ symbols)

---

## Repository

- **GitHub**: `https://github.com/bhaveshhpatel/cipher`
- **Owner**: Dhruv Patel

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Backend | FastAPI (Python 3.11 pinned), async WebSockets |
| Auth | JWT (`python-jose` + `passlib` bcrypt) |
| Streaming | Tradier WebSocket → async in-process event bus |
| AI Engine | Groq `llama-3.3-70b-versatile` (multi-agent swarm, 3/6/9/12 agents) |
| Database | Supabase (PostgreSQL) |
| Deploy (BE) | Railway |
| Deploy (FE) | Vercel |
| CI/CD | GitHub Actions |

---

## Phase History

### Phase 1 — Foundation
- FastAPI backend scaffolded on Railway
- Tradier SSE stream integration (`services/tradier_stream.py`)
- `RepetitionAccumulator` — groups flow by (ticker, strike, expiry, type), emits episodes at ≥3 trades / ≥$50K premium
- `AsyncEventBus` in-memory fan-out (`core/async_bus.py`)
- Supabase persistence: `flow_episodes` + `flow_events` tables
- Auth: JWT-based (`/api/auth/register`, `/api/auth/login`, `/api/auth/me`)
- WebSocket delivery: `/ws/signals`
- Market-hours guard (no streaming outside 09:30–16:00 ET Mon–Fri)
- Railway deployment with nixpacks, environment variable management

### Phase 2 — Signal Engine + Hardening
- `composite_signal_engine.py` — combined flow score + backtest score
  - Weights: `flow × 0.60 + backtest × 0.40`
  - Recommendation: BUY / SELL / HOLD at ≥0.65 composite threshold
- `backtest_validator.py` — historical win-rate lookup by ticker/type/DTE/tier
- `smart_signals.py` router — `/api/signals/composite/{ticker}` endpoint
- Multiple stream failure mode fixes (F1–F9): token refresh, 401 handling, watchdog, backoff with jitter
- Flow store fixes (REG-FS-1 through REG-FS-3)
- Comprehensive test suite

### Phase 3 — Volume-Weighted Scoring, Filters, Heartbeat
- `options_flow_parser.py`: `size == 0` guard
- `composite_signal_engine.py`: 3-component scoring
  - New weights: `flow × 0.55 + backtest × 0.35 + volume_premium × 0.10`
  - `volume_weighted_premium_factor()` = `total_premium / (open_interest × 100)`, capped 0–1, falls back to `0.5` neutral when OI unavailable
- `smart_signals.py`: `GET /api/signals/list` with pagination + filters
- `ws.py`: Full ping/pong heartbeat — server pings every 25s, expects pong within 10s, closes 1001 on timeout

### Phase 4 — Live DB Wiring, Signal History, Flow Fix
- **`services/signal_store.py`** (NEW): subscribes to `signal_writer` bus channel, persists every `CompositeSignal` to `signal_history` table using `SUPABASE_SERVICE_KEY`
- **`routers/history.py`** (NEW): `GET /api/signals/history` — queries `signal_history` with full pagination + filters (ticker, direction, tier, min_conviction, limit, offset)
- **`routers/flow.py`** (FIXED): was querying empty `flow_events` table — now correctly queries `flow_episodes` (82k+ live rows). Maps `direction→sentiment`, `alert_level→influence_tier`
- **`routers/smart_signals.py`** (UPDATED): `/list` and `/composite/{ticker}` now query live `signal_history` DB first, fall back to deterministic mock if DB empty
- **`main.py`** (UPDATED): registers `history.router`, starts `signal_write_task` (`start_signal_writer()`) alongside stream and flow writer tasks
- **Migration 003**: `signal_history` table created
- **Migration 005**: `signal_history` schema repair (NOT NULL columns, check constraints)
- **Migration 006**: RLS policies for flow tables
- **Migration 007**: Seed data
- **Migration 008**: `flow_events.expiry` made nullable

### Phase 5A — AI Swarm Expansion + Dedup + Trade Executor (current)
- **`simulation/swarm_engine.py`** (UPDATED): agent roster expanded to 12 (configurable via `SWARM_N_AGENTS` env var, snaps to nearest of 3/6/9/12). Primary provider: Groq `llama-3.3-70b-versatile`. Graceful HOLD fallback when no API key. `run()` accepts flow_events list OR pre-built summary string.
- **`simulation/ensemble_runner.py`** (UPDATED): `run_ensemble()` correctly passes `flow_events` list to `SwarmEngine.run()`. `EnsembleResult` includes per-agent `name` field.
- **`services/signal_store.py`** (UPDATED): `_build_row()` now persists swarm fields: `swarm_direction`, `swarm_confidence`, `swarm_agents` (JSONB), `swarm_bull_votes`, `swarm_bear_votes`, `swarm_hold_votes`. Three bug fixes applied (Postgres 23502, 23514 errors).
- **`utils/dedup.py`** (NEW): 2-second TTL deduplication cache (`DedupCache`). Keys events on `(occ_symbol, size, fill_price_2dp, time_bucket_2s)`. Prevents same trade printing 4× across exchanges. Also detects sweeps (3+ exchanges within 5s window). Module-level singleton: `flow_dedup`.
- **`utils/tradier_client.py`** (NEW): Tradier REST API client utility.
- **`execution/trade_executor.py`** (NEW): `TradeExecutor` class — places option orders via Tradier REST API (`place_option_order`, `get_positions`). Used for paper trading or live execution.
- **`services/stream_manager.py`** (NEW): Stream pool manager service.
- **`services/stream_worker.py`** (NEW): Stream worker service.
- **`services/symbol_registry.py`** (NEW): OCC Symbol Registry — Layer 1 of options flow architecture. Builds and refreshes OCC contract map. Configurable via `REGISTRY_*` env vars.
- **`signals/midcap_screener.py`** (NEW): Mid-cap screener for signal filtering.
- **Migration 004**: swarm fields added to `signal_history`

---

## Universe Pipeline (Steps 1–5)

```
Step 1: CBOE CSV → ~5,500 raw symbols
        ↓  _fetch_cboe_symbols()  in services/symbols_loader.py
Step 2: Tradier /expirations validation → ~5,500 confirmed optionable symbols
        ↓  _validate_symbols()  in services/symbols_loader.py
Step 3: Tradier batch quotes → /v1/markets/quotes
        - Batch into groups of 200 (~28 parallel requests)
        - Fetch: last_price, volume per symbol
        - Compute stream_eligible flag:
            last_price >= UNIVERSE_MIN_PRICE (default 1.0)
            AND volume >= UNIVERSE_MIN_VOLUME (default 500,000)
        - Priority symbols always forced eligible
        - Upsert all symbols into options_universe_symbols table
Step 4: Extract stream_eligible=true symbols → StreamPoolManager
        (~1,000–2,000 after price/volume filter)
Step 5: Save snapshot to options_universe_snapshots
```

### Startup Universe Resolution (Priority Order)
1. Fresh DB snapshot (< 24h old) → stream starts instantly
2. Tradier fetch + validate + screen → saves to DB, then starts
3. Any DB snapshot (stale) → fallback if Tradier is down
4. `SEED_SYMBOLS` → last resort

Background refresh loop runs every 24h.

---

## Signal Pipeline (Phase 5A)

```
Tradier SSE tick
  → parse_tradier_trade()
       └── size == 0 / missing → return None (skip)
  → DedupCache.is_duplicate()  [utils/dedup.py]
       └── duplicate → drop
       └── canonical → check is_sweep()
  → RepetitionAccumulator.ingest()
       threshold: ≥3 trades, ≥$50K premium, 30-min rolling window
       → RepetitionEpisode
  → build_composite(ep, accumulator)
       flow_score            × 0.55
       backtest_score        × 0.35
       volume_premium_factor × 0.10
       → CompositeSignal { BUY | SELL | HOLD, 0–1 score }
  → bus.publish_all()
       → ws.py            → connected WebSocket clients
       → flow_store.py    → Supabase flow_episodes + flow_events
       → signal_store.py  → Supabase signal_history (+ swarm fields)
```

---

## Composite Score Weights

| Phase | Formula |
|-------|---------|
| Phase 2 | `flow × 0.60 + backtest × 0.40` |
| Phase 3+ | `flow × 0.55 + backtest × 0.35 + volume_premium × 0.10` |

**Recommendation threshold:** composite ≥ 0.65 → BUY (bullish) or SELL (bearish)

`volume_weighted_premium_factor` = `total_premium / (open_interest × 100)`, capped 0–1.
Falls back to `0.5` neutral when OI is unavailable. Do not treat 0.5 as a signal.

---

## AI Swarm (Phase 5A)

- **Provider:** Groq `llama-3.3-70b-versatile` via OpenAI-compatible client
- **Agent counts:** 3, 6, 9, or 12 — configured via `SWARM_N_AGENTS` env var, snaps to nearest valid
- **Agent roles (12 total):**
  - Tier 1 (1–6): Momentum Trader, Contrarian Analyst, Fundamental Analyst, Technical Analyst, Macro Strategist, Risk Manager
  - Tier 2 (7–9): Options Flow Specialist, Quant/Statistical Arb, Sentiment Analyst
  - Tier 3 (10–12): Sector Rotation Strategist, Volatility Trader, Dark Pool/Tape Reader
- **Verdict format:** each agent returns `VERDICT: BUY|SELL|HOLD`, `REASONING: ...`, `CONFIDENCE: 0.0–1.0`
- **Ensemble:** majority vote → `EnsembleResult` with `bull_votes`, `bear_votes`, `hold_votes`, `confidence`
- **Swarm fields persisted to `signal_history`:** `swarm_direction`, `swarm_confidence`, `swarm_bull_votes`, `swarm_bear_votes`, `swarm_hold_votes`, `swarm_agents` (JSONB)
- **Fallback:** all agents return HOLD when `GROQ_API_KEY` not set

---

## WebSocket Protocol

| Message | Direction | Meaning |
|---------|-----------|---------|
| Signal JSON | Server → Client | Live signal episode |
| `{"type":"ping"}` | Server → Client | Heartbeat probe (every 25s) |
| `{"type":"pong"}` | Client → Server | Heartbeat reply (within 10s) |

Connection close codes:
- `4001` — invalid/expired JWT on connect
- `1001` — pong timeout (Railway idle disconnect prevention)

**Frontend must implement pong response** — not yet confirmed implemented.

---

## Repository Structure

```
cipher/
├── .github/
│   └── workflows/
│       ├── backend.yml        # CI only — syntax check; NO deploy steps
│       └── frontend.yml       # Vercel deploy via CLI
├── backend/
│   ├── main.py                # FastAPI app — startup, router registration, lifespan tasks
│   ├── config.py              # pydantic-settings v2 — all env vars incl. SWARM_N_AGENTS, REGISTRY_*
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── nixpacks.toml
│   ├── runtime.txt            # python-3.11.9
│   ├── .python-version        # 3.11.9
│   ├── migrations/
│   │   ├── 001_options_universe.sql
│   │   ├── 002_universe_symbols_quotes.sql
│   │   ├── 003_signal_history.sql
│   │   ├── 004_swarm_fields.sql
│   │   ├── 005_signal_history_repair.sql
│   │   ├── 006_flow_tables_rls.sql
│   │   ├── 007_seed_data.sql
│   │   └── 008_flow_events_expiry_nullable.sql
│   ├── core/
│   │   ├── auth.py
│   │   └── async_bus.py
│   ├── parsers/
│   │   ├── options_flow_parser.py     # size==0 guard
│   │   ├── bid_ask_classifier.py
│   │   └── trade_type_detector.py
│   ├── services/
│   │   ├── flow_store.py          # DB writer: flow_events + flow_episodes — SERVICE ROLE KEY only
│   │   ├── signal_store.py        # [Phase 4/5A] DB writer: signal_history — SERVICE ROLE KEY only
│   │   ├── symbols_loader.py      # Steps 1–3: CBOE fetch, validation, batch quotes
│   │   ├── universe_store.py      # Steps 4–5: DB read/write + upsert_symbol_quotes
│   │   ├── universe_screener.py   # DEPRECATED — OI-based screener, no longer called
│   │   ├── tradier_stream.py      # Resilient WebSocket stream processor
│   │   ├── stream_manager.py      # [Phase 5A] Stream pool manager
│   │   ├── stream_worker.py       # [Phase 5A] Stream worker
│   │   └── symbol_registry.py     # [Phase 5A] OCC contract map registry
│   ├── signals/
│   │   ├── repetition_accumulator.py
│   │   ├── composite_signal_engine.py   # 3-component scoring
│   │   ├── backtest_validator.py
│   │   └── midcap_screener.py           # [Phase 5A]
│   ├── simulation/
│   │   ├── swarm_engine.py        # [Phase 5A] 12-agent Groq LLM swarm
│   │   └── ensemble_runner.py     # [Phase 5A] Majority-vote aggregator
│   ├── execution/
│   │   └── trade_executor.py      # [Phase 5A] Tradier order placement
│   ├── utils/
│   │   ├── dedup.py               # [Phase 5A] 2s TTL dedup + sweep detection
│   │   └── tradier_client.py      # [Phase 5A] Tradier REST client
│   ├── routers/
│   │   ├── ws.py              # WebSocket + ping/pong heartbeat
│   │   ├── smart_signals.py   # /composite/{ticker} + /list — live DB + mock fallback
│   │   ├── history.py         # [Phase 4] /api/signals/history — signal_history table
│   │   ├── flow.py            # /api/flow/scan — queries flow_episodes (FIXED Phase 4)
│   │   ├── auth.py
│   │   └── simulation.py
│   └── tests/
│       ├── test_symbols_loader.py
│       ├── test_tradier_stream.py
│       ├── test_flow_store.py
│       └── test_universe_store.py
├── frontend/
│   └── (Next.js 14 app — src/, __tests__/, vercel.json)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── BACKLOG.md
│   ├── FIXES.md
│   ├── SIGNAL_ENGINE.md
│   ├── features.md
│   ├── regression-test-plan.md
│   └── specs.md
└── claude.md                  # This file
```

---

## Key File Map

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI app, lifespan startup, all router registration |
| `backend/config.py` | Pydantic settings — all env vars |
| `backend/services/tradier_stream.py` | SSE stream loop, market-hours guard, demo mode, stats |
| `backend/parsers/options_flow_parser.py` | Tradier tick → `OptionsFlowEvent`, size==0 guard |
| `backend/parsers/bid_ask_classifier.py` | ABOVE_ASK / AT_ASK / MID / AT_BID / BELOW_BID |
| `backend/parsers/trade_type_detector.py` | SWEEP / BLOCK / SPLIT / SINGLE |
| `backend/signals/repetition_accumulator.py` | Groups events into `RepetitionEpisode` |
| `backend/signals/composite_signal_engine.py` | `build_composite()` — 3-component scoring |
| `backend/signals/backtest_validator.py` | Historical win-rate lookup |
| `backend/simulation/swarm_engine.py` | 12-agent Groq LLM swarm |
| `backend/simulation/ensemble_runner.py` | Majority-vote aggregator → `EnsembleResult` |
| `backend/execution/trade_executor.py` | Tradier order placement (paper + live) |
| `backend/utils/dedup.py` | 2s TTL dedup cache + sweep detection. Singleton: `flow_dedup` |
| `backend/utils/tradier_client.py` | Tradier REST client utility |
| `backend/services/signal_store.py` | Supabase writer for `signal_history` — SERVICE KEY only |
| `backend/services/flow_store.py` | Supabase writer for `flow_episodes`/`flow_events` — SERVICE KEY only |
| `backend/services/symbol_registry.py` | OCC contract map registry |
| `backend/routers/ws.py` | WebSocket `/ws/signals` with ping/pong heartbeat |
| `backend/routers/smart_signals.py` | `/composite/{ticker}` + `/list` — live DB + mock fallback |
| `backend/routers/history.py` | `/api/signals/history` — paginated signal_history queries |
| `backend/routers/flow.py` | `/api/flow/scan` — queries live `flow_episodes` table |
| `backend/routers/auth.py` | JWT auth endpoints |
| `backend/routers/simulation.py` | Paper trading simulation |
| `backend/core/async_bus.py` | In-memory async event bus |
| `backend/core/auth.py` | JWT decode, `get_current_user` dependency |

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/auth/register` | No | Register user |
| POST | `/api/auth/login` | No | Login, returns JWT |
| GET | `/api/auth/me` | JWT | Current user info |
| GET | `/api/signals/composite/{ticker}` | JWT | Single-ticker composite signal (DB first, mock fallback) |
| GET | `/api/signals/list` | JWT | Paginated signal list (DB first, mock fallback) |
| GET | `/api/signals/history` | JWT | Paginated signal_history with full filters |
| GET | `/api/signals/stream/stats` | JWT | Stream stats (ticks, signals, mode) |
| GET | `/api/stream/stats` | JWT | Alias for stream stats |
| GET | `/api/flow/scan` | JWT | Live flow scan from flow_episodes table |
| POST | `/api/simulate` | JWT | Run paper trading simulation |
| WS | `/ws/signals?token=<jwt>` | JWT (query) | Live signal stream |
| GET | `/health` | No | Health check |
| GET | `/` | No | Root — version info |

### `/api/signals/history` Query Params

| Param | Type | Default | Constraints |
|-------|------|---------|-------------|
| `ticker` | string | — | 1–10 chars |
| `direction` | string | — | `bullish` / `bearish` / `neutral` |
| `tier` | string | — | `whale` / `institutional` / `large` / `retail` |
| `min_conviction` | float | 0.0 | 0.0–1.0 |
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | ≥0 |

### `/api/signals/list` Query Params

| Param | Type | Default | Constraints |
|-------|------|---------|-------------|
| `page` | int | 1 | ≥1 |
| `page_size` | int | 20 | 1–100 |
| `direction` | string | — | `bullish` / `bearish` / `neutral` |
| `tier` | string | — | `whale` / `institutional` / `large` / `retail` |
| `min_conviction` | float | 0.0 | 0.0–1.0 |

---

## Supabase Tables

| Table | Writer | Key Used | Notes |
|-------|--------|----------|-------|
| `flow_episodes` | `flow_store.py` | SERVICE_KEY | 82k+ rows, primary flow data |
| `flow_events` | `flow_store.py` | SERVICE_KEY | Currently 0 rows — not the live table |
| `signal_history` | `signal_store.py` | SERVICE_KEY | Composite signals + swarm fields |
| `options_universe_symbols` | `universe_store.py` | ANON_KEY | Symbol quotes, stream_eligible |
| `options_universe_snapshots` | `universe_store.py` | ANON_KEY | Universe snapshots |

---

## Supabase Critical Rules

1. **Always use `SUPABASE_SERVICE_KEY`** for all writes to `flow_episodes`, `flow_events`, `signal_history` — the anon key fails with `42501` due to RLS
2. **Never send `id` fields** for `flow_events` (uuid) or `flow_episodes` (bigserial) — Postgres generates them
3. **No `.select()` chained after `.insert()`** in supabase-py v2
4. **`flow_events` is empty** — live data is in `flow_episodes` (82k+ rows)
5. **Env var is `SUPABASE_SERVICE_KEY`** (NOT `SUPABASE_SERVICE_ROLE_KEY`) — config.py uses `SUPABASE_SERVICE_KEY`

### Supabase Key Reference

| Key | Env var | Used by | Bypasses RLS? |
|-----|---------|---------|---------------|
| Anon / Public | `SUPABASE_KEY` | `universe_store.py`, `smart_signals.py`, `history.py` (reads) | No |
| Service Role | `SUPABASE_SERVICE_KEY` | `flow_store.py`, `signal_store.py`, `flow.py` (writes + RLS bypass reads) | Yes |

---

## Known Fixes Applied

| ID | Description |
|---|---|
| C-005 | supabase-py v2 `.select()` after `.insert()` breaks — generate `snapshot_id` via `uuid4()` in Python |
| C-006 | `options_universe_snapshots.provider` NOT NULL — always pass `"tradier"` explicitly |
| C-007 | `config.py` missing `priority_symbols` property — added `@property` |
| C-008 | `stream_eligible` column missing from DB — added in migration 002 |
| C-009 | `universe_screener.py` deprecated — replaced by `_fetch_batch_quotes()` |
| C-010 | `flow_store.py` was falling back to anon key — fixed to require `SUPABASE_SERVICE_KEY` exclusively |
| C-011 | `flow.py` was querying empty `flow_events` — fixed to query `flow_episodes` |
| C-012 | `signal_store.py` `_build_row()` omitting NOT NULL columns — Postgres 23502. Fixed: `alert_level`, `sentiment`, `premium`, `trade_type`, `is_golden_sweep` now always populated |
| C-013 | `direction` column check constraint — REPEAT_BUY→BUY, REPEAT_SELL→SELL. Postgres 23514 |
| C-014 | `trade_type` NOT NULL — unrecognised values fall back to `SINGLE` |
| C-015 | `influence_tier` NOT NULL — unrecognised values fall back to `RETAIL` |

---

## Environment Variables (Full List)

```
# Auth
SECRET_KEY=
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# Supabase
SUPABASE_URL=
SUPABASE_KEY=                  # anon key — reads only
SUPABASE_SERVICE_KEY=          # service role key — REQUIRED for all writes

# Tradier
TRADIER_API_KEY=
TRADIER_ACCOUNT_ID=
TRADIER_BASE_URL=https://api.tradier.com
TRADIER_STREAM_URL=https://stream.tradier.com

# AI
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GROQ_API_KEY=                  # PRIMARY — used by swarm_engine.py (llama-3.3-70b-versatile)

# Misc
REDIS_URL=redis://localhost:6379
ALLOWED_ORIGINS=http://localhost:3000

# Universe pipeline
UNIVERSE_PRIORITY_SYMBOLS=SPY,QQQ,AAPL,TSLA,NVDA,MSFT,AMZN,META,GOOGL,AMD
UNIVERSE_BATCH_DELAY_MS=0
UNIVERSE_STREAM_ELIGIBLE_DEFAULT=true
UNIVERSE_MIN_PRICE=1.0
UNIVERSE_MIN_VOLUME=500000
UNIVERSE_QUOTES_BATCH_SIZE=200
UNIVERSE_QUOTES_CONCURRENCY=28

# AI Swarm
SWARM_N_AGENTS=6               # snaps to nearest of 3, 6, 9, 12

# OCC Symbol Registry
REGISTRY_MAX_DTE=90
REGISTRY_ATM_RANGE_PCT=0.15
REGISTRY_REFRESH_MINS=30
REGISTRY_MIN_OI=0
REGISTRY_EXPIRY_DAY_REFRESH_MINS=15
```

---

## Important Implementation Notes

### SUPABASE_SERVICE_KEY vs SUPABASE_SERVICE_ROLE_KEY
The env var in `config.py` and throughout the codebase is **`SUPABASE_SERVICE_KEY`** (no `_ROLE_` in the name). Old docs referenced `SUPABASE_SERVICE_ROLE_KEY` — that is wrong. Always use `SUPABASE_SERVICE_KEY`.

### flow_events vs flow_episodes
`flow_events` has 0 rows. All 82k+ live flow records are in `flow_episodes`. Never query `flow_events` for live data.

### DedupCache
`flow_dedup` is the module-level singleton in `utils/dedup.py`. Key: `(occ_symbol, size, fill_2dp, time_bucket_2s)`. Sweep = 3+ exchanges within 5s window.

### Tradier Single-Symbol Dict Edge Case
When only 1 symbol in a `/v1/markets/quotes` batch, Tradier returns a dict not a list:
```python
if isinstance(quotes_raw, dict):
    quotes_raw = [quotes_raw]
```

### Price Field Fallback Order
`last` → `last_price` → `close` → `prevclose`

### universe_screener.py
DEPRECATED. Kept for backward test compatibility only. Do NOT re-add call from `load_universe()`.

### volume_premium_factor OI Fallback
Falls back to `0.5` neutral when OI unavailable. Do not treat 0.5 as a signal — it means OI data was absent.

### Frontend WS Pong
Frontend must send `{"type":"pong"}` within 10s of receiving `{"type":"ping"}` or connection closes with code 1001. **Status: not yet confirmed implemented in frontend.**

---

## Open / Phase 6 TODO

- Frontend: implement WS pong response
- Load test `/api/signals/list` and `/api/signals/history` with 50 concurrent authenticated users
- WebSocket fan-out benchmark with 50+ subscribers
- Investigate OI field availability per symbol (affects `volume_premium_factor` fallback rate)
- Wire `TradeExecutor` into simulation router for live paper trade execution
- `stream_manager.py` + `stream_worker.py` integration — confirm wired into main stream loop
- `symbol_registry.py` — confirm integrated into flow pipeline
- `signals/midcap_screener.py` — confirm integrated into signal pipeline
