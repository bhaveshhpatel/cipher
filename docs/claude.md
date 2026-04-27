&#35; Cipher — AI Context File

> Last updated: 2026-04-26 (Feature 4A-OI + Registry Prewarm + Test Suite)
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
| CI/CD | GitHub Actions (regression-gated) |

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

### Phase 3 — Volume-Weighted Scoring, Filters, Heartbeat
- `options_flow_parser.py`: `size == 0` guard
- `composite_signal_engine.py`: 3-component scoring
  - New weights: `flow × 0.55 + backtest × 0.35 + volume_premium × 0.10`
  - `volume_weighted_premium_factor()` = `total_premium / (open_interest × 100)`, capped 0–1, falls back to `0.5` neutral when OI unavailable
- `smart_signals.py`: `GET /api/signals/list` with pagination + filters
- `ws.py`: Full ping/pong heartbeat — server pings every 25s, expects pong within 10s, closes 1001 on timeout

### Phase 4 — Live DB Wiring, Signal History, Flow Fix
- **`services/signal_store.py`** (NEW): subscribes to `signal_writer` bus channel, persists every `CompositeSignal` to `signal_history` table using `SUPABASE_SERVICE_KEY`
- **`routers/history.py`** (NEW): `GET /api/signals/history` — queries `signal_history` with full pagination + filters
- **`routers/flow.py`** (FIXED): was querying empty `flow_events` — now correctly queries `flow_episodes` (82k+ live rows)
- **`routers/smart_signals.py`** (UPDATED): live DB first, mock fallback
- **`main.py`** (UPDATED): registers `history.router`, starts `signal_write_task`
- Migrations 003, 005, 006, 007, 008

### Phase 5A — AI Swarm Expansion + Dedup + Trade Executor
- **`simulation/swarm_engine.py`**: 12-agent Groq `llama-3.3-70b-versatile` swarm. HOLD fallback when no API key.
- **`simulation/ensemble_runner.py`**: majority-vote aggregator. `EnsembleResult` includes per-agent `name` field.
- **`services/signal_store.py`**: persists swarm fields: `swarm_direction`, `swarm_confidence`, `swarm_agents` (JSONB), vote counts.
- **`utils/dedup.py`** (NEW): 2s TTL dedup cache (`DedupCache`). Sweep = 3+ exchanges within 5s. Singleton: `flow_dedup`.
- **`utils/tradier_client.py`** (NEW): Tradier REST API client.
- **`execution/trade_executor.py`** (NEW): `TradeExecutor` — `place_option_order`, `get_positions`.
- **`services/stream_manager.py`**, **`stream_worker.py`** (NEW): stream pool (32 parallel workers).
- **`services/symbol_registry.py`** (NEW): OCC contract map registry.
- **`signals/midcap_screener.py`** (NEW): mid-cap screener.
- Migration 004: swarm fields on `signal_history`

### Phase 5B — Regression Test Suite + CI Gate
- **Full automated regression test suite** covering the entire backend and frontend codebase.
- **~380+ test cases** across 40+ backend test files and frontend hook tests.
- **CI hard gate**: backend ≥90% coverage (`--cov-fail-under=90`), frontend ≥75% lines/functions globally.
- **Nothing deploys** to Railway or Vercel unless regression tests pass.
- **PR coverage bot**: `orgoro/coverage@v3.2` posts coverage diff comment on every PR.
- See `docs/REGRESSION_TESTING.md` for full test inventory.

### Feature 4A — Dynamic Tier Classification
- `services/tier_engine.py`: `assign_tiers()`, `_classify()`, `_fetch_thresholds()`, `invalidate_thresholds_cache()`
- `services/symbol_registry.py`: `_TierParams` dataclass, `ContractMeta.tier` field
- `universe_store.load_tier_map()` / `upsert_symbol_quotes(tier_map=...)`
- `routers/admin.py`: `GET /api/admin/tier-thresholds`, `PATCH /api/admin/tier-thresholds`, `GET /api/admin/tier-distribution`
- Migrations 010, 011, 012

### Feature 4A-OI — OI-Gated Tier Classification (CURRENT)
- `SymbolRegistry.get_oi_map()` — returns `{symbol: avg_oi}` after `build()`
- `main._stamp_oi(quotes, oi_map)` — stamps avg chain OI onto `SymbolQuote` objects in-place
- Two-pass tier assignment in `lifespan()`: preliminary pass (OI=0) → registry build → OI stamp → final re-classification
- Same two-pass logic in `_universe_refresh_loop()` for background 24h refresh
- `tier_engine._classify()`: OI grace path removed — all 3 conditions (vol + price + OI) required for T1/T2
- `universe_store._sync_upsert_symbol_quotes()`: `open_interest` field included in every upsert row

### Registry Prewarm (CURRENT)
- `main._registry_prewarm_loop()` — background async task that pre-builds the OCC symbol registry each trading day before market open
- Behavior: skips weekends, sleeps until 09:15 ET on weekdays, calls `get_registry().build()`, catches all exceptions non-fatally
- Wired into `lifespan()` startup as `prewarm_task = asyncio.create_task(_registry_prewarm_loop())`
- `prewarm_task` is cancelled and awaited in the lifespan cleanup alongside other background tasks
- Tests: `backend/tests/test_registry_prewarm.py` (5 cases) + `test_lifespan_spawns_prewarm_task` in `test_main_app.py`

---

## lifespan() Startup Sequence (main.py)

```
1. _configure_logging()
2. Load universe:
   a. Try fresh DB snapshot (< 24h)
   b. Else: Tradier fetch + validate → save to DB
   c. Else: stale DB snapshot
   d. Else: SEED_SYMBOLS fallback
3. Preliminary tier pass (OI=0 placeholder)
4. Build OCC registry in background → _registry_prewarm_loop waits until 09:15 ET
5. Stamp OI onto SymbolQuotes → final tier re-classification
6. Start background tasks:
   - universe_refresh_task  (24h loop)
   - signal_write_task
   - prewarm_task           ← NEW: rebuilds registry each morning at 09:15 ET
7. Yield (app serves requests)
8. Shutdown: cancel all tasks, await graceful exit
```

---

## Test Suite — How to Run

```bash
# Backend — full suite
cd backend
pip install -r requirements-dev.txt
pytest

# Backend — skip coverage for speed
pytest --no-cov

# Frontend
cd frontend
npx jest --coverage
```

See `docs/REGRESSION_TESTING.md` for full reference.

---

## CI/CD Pipeline

```
Push to main (backend/**)
  └── lint
        └── regression (--cov-fail-under=90)
              └── Railway auto-deploys via native integration

Push to main (frontend/**)
  └── typecheck + lint
        └── regression (jest --ci --coverage, thresholds in jest.config.ts)
              └── build
                    └── deploy (vercel --prod)

Pull Request
  └── Same gates + orgoro/coverage posts PR comment with coverage diff
```

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
Step 5: Save snapshot to options_universe_snapshots
```

### Startup Universe Resolution (Priority Order)
1. Fresh DB snapshot (< 24h old) → stream starts instantly
2. Tradier fetch + validate + screen → saves to DB, then starts
3. Any DB snapshot (stale) → fallback if Tradier is down
4. `SEED_SYMBOLS` → last resort

Background refresh loop runs every 24h.

---

## Signal Pipeline

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

## AI Swarm

- **Provider:** Groq `llama-3.3-70b-versatile` via OpenAI-compatible client
- **Agent counts:** 3, 6, 9, or 12 — configured via `SWARM_N_AGENTS` env var, snaps to nearest valid
- **Agent roles (12 total):**
  - Tier 1 (1–6): Momentum Trader, Contrarian Analyst, Fundamental Analyst, Technical Analyst, Macro Strategist, Risk Manager
  - Tier 2 (7–9): Options Flow Specialist, Quant/Statistical Arb, Sentiment Analyst
  - Tier 3 (10–12): Sector Rotation Strategist, Volatility Trader, Dark Pool/Tape Reader
- **Verdict format:** each agent returns `VERDICT: BUY|SELL|HOLD`, `REASONING: ...`, `CONFIDENCE: 0.0–1.0`
- **Ensemble:** majority vote → `EnsembleResult`
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
│       ├── backend.yml        # lint → regression (≥90%) → Railway
│       └── frontend.yml       # typecheck → regression (≥75%) → build → Vercel
├── backend/
│   ├── main.py                # FastAPI app + lifespan + _registry_prewarm_loop
│   ├── config.py
│   ├── pytest.ini             # asyncio_mode=auto, --cov-fail-under=90
│   ├── .coveragerc            # omit rules, fail_under=90
│   ├── requirements.txt
│   ├── requirements-dev.txt   # pytest-cov, fastapi[all]
│   ├── migrations/            # 001–012
│   ├── core/
│   │   ├── auth.py
│   │   └── async_bus.py
│   ├── parsers/
│   │   ├── options_flow_parser.py
│   │   ├── bid_ask_classifier.py
│   │   └── trade_type_detector.py
│   ├── services/
│   │   ├── flow_store.py
│   │   ├── signal_store.py
│   │   ├── symbols_loader.py
│   │   ├── universe_store.py
│   │   ├── tradier_stream.py
│   │   ├── stream_manager.py
│   │   ├── stream_worker.py
│   │   ├── tier_engine.py
│   │   └── symbol_registry.py
│   ├── signals/
│   │   ├── repetition_accumulator.py
│   │   ├── composite_signal_engine.py
│   │   ├── backtest_validator.py
│   │   └── midcap_screener.py
│   ├── simulation/
│   │   ├── swarm_engine.py
│   │   └── ensemble_runner.py
│   ├── execution/
│   │   └── trade_executor.py
│   ├── utils/
│   │   ├── dedup.py
│   │   └── tradier_client.py
│   ├── routers/
│   │   ├── ws.py
│   │   ├── smart_signals.py
│   │   ├── history.py
│   │   ├── flow.py
│   │   ├── auth.py
│   │   ├── simulation.py
│   │   ├── admin.py
│   │   └── health.py
│   └── tests/                 # 50+ test files, ~600+ cases
│       ├── test_registry_prewarm.py        ← NEW: prewarm loop (5 cases)
│       ├── test_main_app.py                ← UPDATED: + lifespan_spawns_prewarm_task
│       ├── test_4a_oi_pipeline.py
│       ├── test_4a_tier_engine.py
│       └── [see REGRESSION_TESTING.md for full list]
├── frontend/
│   ├── jest.config.ts
│   ├── __mocks__/
│   └── src/
│       ├── app/
│       ├── components/
│       ├── hooks/
│       ├── lib/api.ts
│       └── types/
└── docs/
    ├── claude.md              ← THIS FILE
    ├── CHANGELOG.md
    ├── REGRESSION_TESTING.md
    ├── ARCHITECTURE.md
    ├── BACKLOG.md
    ├── FIXES.md
    └── SIGNAL_ENGINE.md
```

---

## Key File Map

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI app, lifespan startup, all router registration, `_registry_prewarm_loop` |
| `backend/config.py` | Pydantic settings — all env vars |
| `backend/pytest.ini` | pytest config — `asyncio_mode=auto`, `--cov-fail-under=90` |
| `backend/.coveragerc` | coverage.py omit rules, `fail_under=90` |
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
| `backend/services/symbol_registry.py` | OCC contract map registry, `get_oi_map()` |
| `backend/services/tier_engine.py` | Dynamic T1/T2/T3 classification with DB-backed thresholds |
| `backend/routers/ws.py` | WebSocket `/ws/signals` with ping/pong heartbeat |
| `backend/routers/smart_signals.py` | `/composite/{ticker}` + `/list` — live DB + mock fallback |
| `backend/routers/history.py` | `/api/signals/history` — paginated signal_history queries |
| `backend/routers/flow.py` | `/api/flow/scan` — queries live `flow_episodes` table |
| `backend/routers/auth.py` | JWT auth endpoints |
| `backend/routers/simulation.py` | Paper trading simulation |
| `backend/routers/admin.py` | Tier-threshold CRUD, admin role guard |
| `backend/routers/health.py` | `GET /api/health/stream` |
| `backend/core/async_bus.py` | In-memory async event bus |
| `backend/core/auth.py` | JWT decode, `get_current_user` dependency |
| `backend/tests/test_registry_prewarm.py` | 5 tests for `_registry_prewarm_loop` |
| `frontend/jest.config.ts` | Jest config with `coverageThreshold` per-file and global |
| `docs/REGRESSION_TESTING.md` | Full regression test suite reference |

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/auth/register` | No | Register user |
| POST | `/api/auth/login` | No | Login, returns JWT |
| GET | `/api/auth/me` | JWT | Current user info |
| GET | `/api/signals/composite/{ticker}` | JWT | Single-ticker composite signal |
| GET | `/api/signals/list` | JWT | Paginated signal list |
| GET | `/api/signals/history` | JWT | Paginated signal_history |
| GET | `/api/signals/stream/stats` | JWT | Stream stats |
| GET | `/api/flow/scan` | JWT | Live flow scan |
| POST | `/api/simulation/run` | JWT | Run swarm simulation |
| GET | `/api/admin/tier-thresholds` | JWT+Admin | Read tier thresholds + cache age |
| PATCH | `/api/admin/tier-thresholds` | JWT+Admin | Update tier thresholds |
| GET | `/api/admin/tier-distribution` | JWT+Admin | Current tier distribution |
| GET | `/api/health/stream` | No | Stream health |
| WS | `/ws/signals?token=<jwt>` | JWT (query) | Live signal stream |
| GET | `/health` | No | Health check |
| GET | `/` | No | Root — version info |

---

## Supabase Tables

| Table | Writer | Key Used | Notes |
|-------|--------|----------|-------|
| `flow_episodes` | `flow_store.py` | SERVICE_KEY | 82k+ rows, primary flow data |
| `flow_events` | `flow_store.py` | SERVICE_KEY | Currently 0 rows — not the live table |
| `signal_history` | `signal_store.py` | SERVICE_KEY | Composite signals + swarm fields |
| `options_universe_symbols` | `universe_store.py` | ANON_KEY | Symbol quotes, stream_eligible, open_interest |
| `options_universe_snapshots` | `universe_store.py` | ANON_KEY | Universe snapshots |
| `tier_thresholds` | Admin API | SERVICE_KEY | T1/T2/T3 classification thresholds |

---

## Supabase Critical Rules

1. **Always use `SUPABASE_SERVICE_KEY`** for writes to `flow_episodes`, `flow_events`, `signal_history`
2. **Never send `id` fields** for `flow_events` (uuid) or `flow_episodes` (bigserial)
3. **No `.select()` chained after `.insert()`** in supabase-py v2
4. **`flow_events` is empty** — live data is in `flow_episodes` (82k+ rows)
5. **Env var is `SUPABASE_SERVICE_KEY`** (NOT `SUPABASE_SERVICE_ROLE_KEY`)

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
| C-012 | `signal_store.py` `_build_row()` omitting NOT NULL columns — Postgres 23502 |
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
GROQ_API_KEY=                  # PRIMARY — used by swarm_engine.py (llama-3.3-70b-versatile)

# Misc
ALLOWED_ORIGINS=http://localhost:3000

# Universe pipeline
UNIVERSE_PRIORITY_SYMBOLS=SPY,QQQ,AAPL,TSLA,NVDA,MSFT,AMZN,META,GOOGL,AMD
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
The env var in `config.py` is **`SUPABASE_SERVICE_KEY`** (no `_ROLE_`). Old docs used `SUPABASE_SERVICE_ROLE_KEY` — that is wrong.

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
Falls back to `0.5` neutral when OI unavailable. Do not treat 0.5 as a signal.

### OI Gate for Tier Classification
Symbols with zero OI (after `_stamp_oi()` runs) cannot be classified T1 or T2 regardless of volume or price. This is enforced in `tier_engine._classify()`.

### Registry Prewarm Loop
`_registry_prewarm_loop()` is an infinite async loop in `main.py`. It sleeps until 09:15 ET each weekday, then calls `get_registry().build()`. Exceptions are caught and logged non-fatally — the loop always continues to the next day. On weekends it sleeps 1 hour and re-checks. The task is cancelled cleanly on shutdown.

### Frontend WS Pong
Frontend must send `{"type":"pong"}` within 10s of receiving `{"type":"ping"}` or connection closes with code 1001. **Status: not yet confirmed implemented in frontend.**

---

## Open / Phase 6 TODO

- Frontend: implement WS pong response
- Load test `/api/signals/list` and `/api/signals/history` with 50 concurrent authenticated users
- WebSocket fan-out benchmark with 50+ subscribers
- Wire `TradeExecutor` into simulation router for live paper trade execution
- Confirm `stream_manager.py` + `stream_worker.py` wired into main stream loop
- Confirm `signals/midcap_screener.py` integrated into signal pipeline
- Investigate OI field availability per symbol (affects `volume_premium_factor` fallback rate)
- Add frontend UI component tests (SignalFeed, FlowTable, SimulationPanel, login page)
- Raise backend `--cov-fail-under` from 90% to 95% once UI tests added
- Raise frontend Jest global threshold from 75% to 85%
