# Cipher — AI Context File

> Last updated: 2026-04-26 (Feature 4A-OI + Registry Prewarm + Test Suite + C-019 dedup fixes + CORS regex + B-008/021/022/023)
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
- **`utils/dedup.py`** (NEW): `DedupCache`. TTL=5s (C-019), sweep_window=8s. Key: `(occ_symbol, size, round(fill,1))`. No time-bucket. Singleton: `flow_dedup`.
- **`utils/tradier_client.py`** (NEW): Tradier REST API client.
- **`execution/trade_executor.py`** (NEW): `TradeExecutor` — `place_option_order`, `get_positions`.
- **`services/stream_manager.py`**, **`stream_worker.py`** (NEW): stream pool (32 parallel workers).
- **`services/symbol_registry.py`** (NEW): OCC contract map registry.
- **`signals/midcap_screener.py`** (NEW): mid-cap screener.
- Migration 004: swarm fields on `signal_history`

### Phase 5B — Regression Test Suite + CI Gate
- **Full automated regression test suite** covering the entire backend and frontend codebase.
- **48 backend test files**, CI hard gate: backend ≥90% coverage (`--cov-fail-under=90`), frontend ≥75% lines/functions globally.
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
- Weekend handling: if today is already past 09:15, next_prewarm advances to next weekday
- Wired into `lifespan()` startup as `prewarm_task = asyncio.create_task(_registry_prewarm_loop())`
- `prewarm_task` is cancelled and awaited in the lifespan cleanup alongside all other background tasks
- Tests: `backend/tests/test_registry_prewarm.py` (5 cases) + `test_lifespan_spawns_prewarm_task` in `test_main_app.py`

### C-019 — Dedup Layer 4 Overhaul (CURRENT)
- **TTL**: 2s → 5s — covers worst-case PHLX/MIAX lag (2–5s)
- **Sweep window**: 5s → 8s
- **Bucket boundary bug eliminated**: removed `int(ts//2)` — replaced with pure first-seen TTL comparison
- **Fill key**: 2dp → 1dp — absorbs ±$0.01 feed rounding across exchanges
- **Dedup was completely inert in production** — `flow_dedup` was never imported or called in `_process_trade()`. Fixed.
- **Sweep detection never fired** — `exchange` field was never passed to `is_duplicate()`. Fixed via `"exch"/"exchange"` fallback.
- `dedup_stats()` and `get_exchange_count()` added; merged into `/health` via `get_stats()`

### B-008/B-021/B-022/B-023 — Stream Hardening (CURRENT)
- **B-008**: `_stats["errors"]`, `_stats["reconnects"]`, `_stats["last_reconnect_at"]` were always 0/null. Fixed via `_inc_global_error()` / `_inc_global_reconnect()` in `stream_worker.py`.
- **B-021**: Cold-start worker stagger — worker `i` sleeps `i × 0.200s` before first token fetch. Reconnects do NOT re-apply stagger.
- **B-022**: Global `asyncio.Semaphore(3)` in `tradier_client.py` caps concurrent session token requests at 3.
- **B-023**: Explicit HTTP 429 handling — reads `Retry-After` header, sleeps that duration (default 10s), then retries.

### CORS Regex (CURRENT)
- `main.py` uses `allow_origin_regex` (NOT `allow_origins=["*"]` which breaks `allow_credentials=True`)
- Pattern covers: `https://*.vercel.app`, `http://localhost:3000`, `http://localhost:3001`, `http://127.0.0.1:3000`, plus any explicit origins from `CORS_ALLOWED_ORIGINS` env var
- Pattern is logged at startup: `[main] CORS allow_origin_regex: ...`

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
4. init_registry(watchlist, tier_map)
5. registry.build() — blocking until OI available
6. Stamp OI onto SymbolQuotes → final tier re-classification
7. registry.set_tier_map(tier_map) + upsert_symbol_quotes()
8. Start background tasks:
   - registry_refresh_task  (registry.refresh_loop)
   - prewarm_task           (_registry_prewarm_loop — rebuilds registry at 09:15 ET each weekday)
   - stream_task            (stream_options_flow)
   - db_write_task          (start_flow_writer)
   - signal_write_task      (start_signal_writer)
   - refresh_task           (_universe_refresh_loop — 24h loop)
9. Yield (app serves requests)
10. Shutdown: cancel all 6 tasks, await graceful exit
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
       └── fill_price = tick["last"] or tick.get("price") or mid  (C-015)
  → DedupCache.is_duplicate()  [utils/dedup.py]  C-019
       └── key: (occ_symbol, size, round(fill, 1))
       └── TTL: 5s  sweep_window: 8s
       └── exchange: trade_payload["exch"] or ["exchange"]
       └── duplicate → drop
       └── canonical → check is_sweep()
       └── 3+ unique exchanges within 8s → trade_type = SWEEP
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
│   ├── main.py                # FastAPI app + lifespan + _registry_prewarm_loop + CORS regex
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
│   └── tests/                 # 48 test files
│       ├── test_registry_prewarm.py        ← prewarm loop (5 cases)
│       ├── test_main_app.py                ← + lifespan_spawns_prewarm_task
│       ├── test_4a_oi_pipeline.py
│       ├── test_4a_tier_engine.py
│       ├── test_auth_cors_regression.py    ← CORS regex tests
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
| `backend/main.py` | FastAPI app, lifespan startup, all router registration, `_registry_prewarm_loop`, CORS regex |
| `backend/config.py` | Pydantic settings — all env vars |
| `backend/pytest.ini` | pytest config — `asyncio_mode=auto`, `--cov-fail-under=90` |
| `backend/.coveragerc` | coverage.py omit rules, `fail_under=90` |
| `backend/services/tradier_stream.py` | SSE stream loop, market-hours guard, demo mode, stats, dedup wired (C-019) |
| `backend/parsers/options_flow_parser.py` | Tradier tick → `OptionsFlowEvent`, fill_price=`tick["last"]` (C-015), size==0 guard, `is_synthetic_quote` (C-018) |
| `backend/parsers/bid_ask_classifier.py` | ABOVE_ASK / AT_ASK / MID / AT_BID / BELOW_BID |
| `backend/parsers/trade_type_detector.py` | SWEEP / BLOCK / SPLIT / SINGLE |
| `backend/signals/repetition_accumulator.py` | Groups events into `RepetitionEpisode` |
| `backend/signals/composite_signal_engine.py` | `build_composite()` — 3-component scoring |
| `backend/signals/backtest_validator.py` | Historical win-rate lookup |
| `backend/simulation/swarm_engine.py` | 12-agent Groq LLM swarm |
| `backend/simulation/ensemble_runner.py` | Majority-vote aggregator → `EnsembleResult` |
| `backend/execution/trade_executor.py` | Tradier order placement (paper + live) |
| `backend/utils/dedup.py` | `DedupCache` TTL=5s, sweep_win=8s, key=(occ,size,fill_1dp), no time-bucket. Singleton: `flow_dedup`. C-019. |
| `backend/utils/tradier_client.py` | Tradier REST client. Semaphore(3) B-022. 429 handler B-023. |
| `backend/services/signal_store.py` | Supabase writer for `signal_history` — SERVICE KEY only |
| `backend/services/flow_store.py` | Supabase writer for `flow_episodes`/`flow_events` — SERVICE KEY only. Flush 500ms/100 rows (L5 fix). |
| `backend/services/symbol_registry.py` | OCC contract map registry, `get_oi_map()`, `set_tier_map()`, `refresh_loop()` |
| `backend/services/tier_engine.py` | Dynamic T1/T2/T3 classification. All 3 conditions (vol+price+OI). DB-backed thresholds cached 300s. |
| `backend/services/stream_manager.py` | `StreamManager`. Worker stagger B-021. |
| `backend/services/stream_worker.py` | Per-worker stream. `startup_delay_s=i*0.2`. `_inc_global_error/reconnect` B-008. |
| `backend/routers/ws.py` | WebSocket `/ws/signals` with ping/pong heartbeat |
| `backend/routers/smart_signals.py` | `/composite/{ticker}` + `/list` — live DB + mock fallback |
| `backend/routers/history.py` | `/api/signals/history` — paginated signal_history queries |
| `backend/routers/admin.py` | Tier threshold admin endpoints |
| `backend/routers/health.py` | `/health/stream` — stream stats including dedup_stats (B-008) |

---

## FastAPI Endpoints

| Path | Method | Auth | Notes |
|------|--------|------|-------|
| `/` | GET | No | Root health check |
| `/health` | GET | No | Health root |
| `/api/health` | GET | No | Health alias |
| `/stream/stats` | GET | No | Railway health probe alias (no /api prefix) |
| `/api/stream/stats` | GET | JWT | Authenticated stream stats alias |
| `/health/stream` | GET | No | Full stream stats incl. dedup (B-008) |
| `/api/auth/register` | POST | No | Register |
| `/api/auth/login` | POST | No | Login |
| `/api/auth/me` | GET | JWT | Current user |
| `/api/flow/scan` | GET | JWT | Flow episodes (82k+ rows) |
| `/api/signals/composite/{ticker}` | GET | JWT | Composite signal for ticker |
| `/api/signals/list` | GET | JWT | Paginated signals |
| `/api/signals/history` | GET | JWT | Signal history with filters |
| `/api/simulate` | POST | JWT | Swarm simulation |
| `/ws/signals` | WS | JWT | Live signal WebSocket |
| `/admin/tier-thresholds` | GET/PATCH | Admin JWT | Tier threshold admin |
| `/admin/tier-distribution` | GET | Admin JWT | Tier distribution |

---

## Critical Implementation Rules

1. **Always use `SUPABASE_SERVICE_KEY`** for writes to `flow_episodes`, `flow_events`, `signal_history`, `tier_thresholds` — anon key fails with `42501` (RLS)
2. **Never send `id` fields** — Postgres generates them server-side
3. **No `.select()` after `.insert()`** in supabase-py v2
4. **`flow_events` is empty** — live data is in `flow_episodes` (82k+ rows)
5. **`flow_dedup` singleton** in `utils/dedup.py`: TTL=5s, sweep_window=8s, key=(occ_symbol, size, round(fill,1)). No time-bucket. Exchange field passed via `"exch"/"exchange"` fallback.
6. **CORS**: use `allow_origin_regex` not `allow_origins=["*"]` — wildcard breaks `allow_credentials=True`
7. **Registry prewarm**: `_registry_prewarm_loop` is a 6th background task in lifespan. Always cancel + await it on shutdown.
8. **fill_price**: always `tick["last"]` primary, `tick.get("price")` fallback, bid/ask mid last resort (C-015)

---

## Supabase Tables

| Table | Writer | Key Used | Notes |
|-------|--------|----------|---------|
| `flow_episodes` | `flow_store.py` | SERVICE_KEY | 82k+ rows, primary flow data |
| `flow_events` | `flow_store.py` | SERVICE_KEY | Batched 500ms/100 rows; `expiry` nullable |
| `signal_history` | `signal_store.py` | SERVICE_KEY | Composite signals + swarm fields |
| `options_universe_symbols` | `universe_store.py` + `tier_engine.py` | ANON/SERVICE | Symbol quotes, stream_eligible, tier/OI/avg_vol |
| `options_universe_snapshots` | `universe_store.py` | ANON | Universe snapshots |
| `tier_thresholds` | admin endpoint | SERVICE_KEY | Single active row; cached 300s |

---

## Environment Variables

| Variable | Used by | Required |
|----------|---------|----------|
| `TRADIER_API_KEY` | tradier_stream.py | Yes (live mode) |
| `TRADIER_BASE_URL` | tradier_stream.py | Yes |
| `TRADIER_STREAM_URL` | tradier_stream.py | Yes |
| `TRADIER_ACCOUNT_ID` | trade_executor.py | Yes (paper/live trading) |
| `SUPABASE_URL` | flow_store, signal_store, universe_store | Yes |
| `SUPABASE_SERVICE_KEY` | flow_store, signal_store, tier_engine | **Yes — service role key** |
| `SUPABASE_KEY` | universe_store, smart_signals (reads) | Yes (anon key) |
| `SECRET_KEY` | auth.py | Yes |
| `ALGORITHM` | auth.py | Yes (default: HS256) |
| `GROQ_API_KEY` | swarm_engine.py | Yes (swarm; HOLD fallback if absent) |
| `SWARM_N_AGENTS` | swarm_engine.py | No (default: 6) |
| `REGISTRY_MAX_DTE` | symbol_registry.py | No (default: 90) |
| `REGISTRY_REFRESH_MINS` | symbol_registry.py | No (default: 30) |
| `TIER_ADMIN_WHITELIST` | tier_engine.py | No — comma-separated tickers forced to Tier 1 |
| `TIER_THRESHOLD_CACHE_TTL_S` | tier_engine.py | No (default: 300) |
| `STREAM_WORKER_STARTUP_DELAY_S` | stream_worker.py | No (default: 0.2) |
| `TRADIER_SESSION_MAX_CONCURRENCY` | tradier_client.py | No (default: 3) |
| `TRADIER_SESSION_429_DEFAULT_SLEEP_S` | tradier_client.py | No (default: 10) |
| `CORS_ALLOWED_ORIGINS` | main.py | No — comma-separated extra origins for CORS regex |
