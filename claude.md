# Cipher — Claude Context File

> Last updated: 2026-04-23 (Phase 3)
> This file is the authoritative AI-assistant context document for the Cipher codebase.
> Keep it updated after every phase so future sessions have full project context.

---

## What Is Cipher?

**Cipher** is an institutional options flow intelligence platform with the tagline *"Decode the Market."* It detects real-time whale/institutional options flow, scores signals using a composite engine, and runs multi-agent AI swarm simulations to generate BUY/SELL/HOLD verdicts.

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
| AI Engine | OpenAI GPT-4o-mini (multi-agent swarm, 6 roles) |
| Database | Supabase (PostgreSQL) |
| Deploy (BE) | Railway |
| Deploy (FE) | Vercel |
| CI/CD | GitHub Actions (CI only for backend; deploy via Railway native GitHub integration) |

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
- Flow store fixes (REG-FS-1 through REG-FS-3): correct table targeting, removed client-sent IDs, f-string logging
- Comprehensive test suite: `test_tradier_stream.py`, `test_flow_store.py`, `test_universe_store.py`, `test_symbols_loader.py`

### Phase 3 — Volume-Weighted Scoring, Filters, Heartbeat (current)
- **`options_flow_parser.py`**: Size field guard — `size == 0` or missing → `return None`, preventing zero-premium events from entering accumulator
- **`composite_signal_engine.py`**: New 3-component scoring
  - Added `volume_weighted_premium_factor()` — measures premium conviction relative to open interest
  - New weights: `flow × 0.55 + backtest × 0.35 + volume_premium × 0.10`
  - `CompositeSignal` dataclass now includes `volume_premium_factor` field
- **`smart_signals.py`**: Hardened and expanded
  - New `GET /api/signals/list` endpoint with pagination (`page`, `page_size`) and filters (`direction`, `tier`, `min_conviction`)
  - Input validation via FastAPI `Query()` constraints + enum checks
  - `CompositeOut` response model includes `volume_premium_factor`
- **`ws.py`**: Full ping/pong heartbeat
  - Server pings every 25s (`{"type":"ping"}`)
  - Expects `{"type":"pong"}` within 10s
  - Closes with code 1001 on pong timeout
  - Prevents Railway idle TCP timeout disconnections

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
            AND volume >= UNIVERSE_MIN_VOLUME (default 100,000)
        - Priority symbols (UNIVERSE_PRIORITY_SYMBOLS) always forced eligible
        - Upsert all symbols into options_universe_symbols table
        ↓  _fetch_batch_quotes()  in services/symbols_loader.py
           upsert_symbol_quotes() in services/universe_store.py
Step 4: Extract stream_eligible=true symbols → StreamPoolManager
        (~1,000–2,000 after price/volume filter)
        ↓  main.py startup reads eligible_set from load_universe() return value
Step 5: Save snapshot to options_universe_snapshots
        ↓  save_snapshot()  in services/universe_store.py
```

### DB Columns Added (migration 002)
`options_universe_symbols` now has:
- `stream_eligible BOOLEAN NOT NULL DEFAULT false`
- `last_price NUMERIC(12,4)`
- `volume BIGINT`
- Index: `idx_universe_symbols_eligible` on `(snapshot_id, stream_eligible) WHERE stream_eligible = true`

### Config Knobs (Railway env vars)

| Var | Default | Purpose |
|---|---|---|
| `UNIVERSE_MIN_PRICE` | `1.0` | Min last_price to be stream-eligible |
| `UNIVERSE_MIN_VOLUME` | `100000` | Min daily volume to be stream-eligible |
| `UNIVERSE_QUOTES_BATCH_SIZE` | `200` | Symbols per /quotes request |
| `UNIVERSE_QUOTES_CONCURRENCY` | `28` | Parallel batch requests |
| `UNIVERSE_PRIORITY_SYMBOLS` | `SPY,QQQ,AAPL,TSLA,NVDA,MSFT,AMZN,META,GOOGL,AMD` | Always stream-eligible regardless of price/volume |

---

## Signal Pipeline (Phase 3)

```
Tradier SSE tick
  → parse_tradier_trade()
       └── size == 0 / missing → return None (skip)
  → RepetitionAccumulator.ingest()
       threshold: ≥3 trades, ≥$50K premium, 30-min rolling window
       → RepetitionEpisode
  → build_composite(ep, accumulator)
       flow_score          × 0.55
       backtest_score      × 0.35
       volume_premium_factor × 0.10
       → CompositeSignal { BUY | SELL | HOLD, 0–1 score }
  → bus.publish_all()
       → ws.py         → connected WebSocket clients
       → flow_store.py → Supabase flow_episodes + flow_events
```

---

## Composite Score Weights

| Phase | Formula |
|-------|---------|
| Phase 2 | `flow × 0.60 + backtest × 0.40` |
| Phase 3 | `flow × 0.55 + backtest × 0.35 + volume_premium × 0.10` |

**Recommendation threshold:** composite ≥ 0.65 → BUY (bullish) or SELL (bearish)

`volume_weighted_premium_factor` = `total_premium / (open_interest × 100)`, capped 0–1.
Falls back to `0.5` neutral when OI is unavailable from Tradier.

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

---

## Repository Structure

```
cipher/
├── .github/
│   └── workflows/
│       ├── backend.yml        # CI only — syntax check; NO deploy steps
│       └── frontend.yml       # Vercel deploy via CLI
├── backend/
│   ├── main.py                # FastAPI app — startup loads universe from DB first
│   ├── config.py              # pydantic-settings v2 — priority_symbols property added
│   ├── requirements.txt       # pydantic[email] ensures email-validator is installed
│   ├── requirements-dev.txt
│   ├── nixpacks.toml
│   ├── runtime.txt            # python-3.11.9
│   ├── .python-version        # 3.11.9
│   ├── migrations/
│   │   ├── 001_options_universe.sql          # base tables
│   │   └── 002_universe_symbols_quotes.sql   # stream_eligible, last_price, volume columns
│   ├── core/
│   │   ├── auth.py
│   │   └── async_bus.py
│   ├── parsers/
│   │   ├── options_flow_parser.py     # [Phase 3] size==0 guard
│   │   ├── bid_ask_classifier.py
│   │   └── trade_type_detector.py
│   ├── services/
│   │   ├── flow_store.py          # DB writer: flow_events + flow_episodes — uses SERVICE ROLE KEY only
│   │   ├── symbols_loader.py      # Steps 1–3: CBOE fetch, validation, batch quotes
│   │   ├── universe_store.py      # Steps 4–5: DB read/write + upsert_symbol_quotes
│   │   ├── universe_screener.py   # DEPRECATED — OI-based screener, no longer called
│   │   └── tradier_stream.py      # Resilient WebSocket stream processor
│   ├── signals/
│   │   ├── repetition_accumulator.py
│   │   ├── composite_signal_engine.py  # [Phase 3] 3-component scoring + volume_premium_factor
│   │   └── backtest_validator.py
│   ├── routers/
│   │   ├── ws.py              # [Phase 3] WebSocket + ping/pong heartbeat
│   │   ├── smart_signals.py   # [Phase 3] /composite/{ticker} + /list endpoint
│   │   ├── flow.py            # /api/flow/scan — currently mocked
│   │   ├── auth.py
│   │   └── simulation.py
│   └── tests/
│       ├── test_symbols_loader.py
│       ├── test_tradier_stream.py
│       ├── test_flow_store.py
│       └── test_universe_store.py
├── frontend/
│   └── (Next.js 14 app)
├── docs/
│   ├── ARCHITECTURE.md        # System data flow and DB schema
│   ├── BACKLOG.md
│   ├── FIXES.md               # Chronological log of all bug fixes applied
│   ├── SIGNAL_ENGINE.md
│   ├── features.md
│   ├── regression-test-plan.md
│   └── specs.md
└── claude.md                  # This file — Claude context for code changes
```

---

## Key File Map

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI app, lifespan startup, router registration |
| `backend/config.py` | Pydantic settings — env vars |
| `backend/services/tradier_stream.py` | SSE stream loop, market-hours guard, demo mode, stats |
| `backend/parsers/options_flow_parser.py` | Tradier tick → `OptionsFlowEvent` |
| `backend/parsers/bid_ask_classifier.py` | ABOVE_ASK / AT_ASK / MID / AT_BID / BELOW_BID |
| `backend/parsers/trade_type_detector.py` | SWEEP / BLOCK / SPLIT / SINGLE |
| `backend/signals/repetition_accumulator.py` | Groups events into `RepetitionEpisode`, emits at threshold |
| `backend/signals/composite_signal_engine.py` | `build_composite()` — 3-component scoring → BUY/SELL/HOLD |
| `backend/signals/backtest_validator.py` | Historical win-rate lookup |
| `backend/routers/ws.py` | WebSocket `/ws/signals` with ping/pong heartbeat |
| `backend/routers/smart_signals.py` | `/api/signals/composite/{ticker}` + `/api/signals/list` |
| `backend/routers/flow.py` | `/api/flow/scan` — **currently mocked** |
| `backend/routers/auth.py` | JWT auth endpoints |
| `backend/routers/simulation.py` | Paper trading simulation |
| `backend/core/async_bus.py` | In-memory async event bus |
| `backend/core/auth.py` | JWT decode, `get_current_user` dependency |
| `backend/services/flow_store.py` | Supabase DB writer — `flow_episodes` + `flow_events` |
| `backend/services/universe_store.py` | Options universe snapshot persistence |

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/auth/register` | No | Register user |
| POST | `/api/auth/login` | No | Login, returns JWT |
| GET | `/api/auth/me` | JWT | Current user info |
| GET | `/api/signals/composite/{ticker}` | JWT | Single-ticker composite signal |
| GET | `/api/signals/list` | JWT | Paginated signal list with filters |
| GET | `/api/signals/stream/stats` | JWT | Stream stats (ticks, signals, mode) |
| GET | `/api/flow/scan` | JWT | Flow scan (mocked — Phase 4 TODO) |
| POST | `/api/simulate` | JWT | Run paper trading simulation |
| WS | `/ws/signals?token=<jwt>` | JWT (query) | Live signal stream |

### `/api/signals/list` Query Params

| Param | Type | Default | Constraints |
|-------|------|---------|-------------|
| `page` | int | 1 | ≥1 |
| `page_size` | int | 20 | 1–100 |
| `direction` | string | — | `bullish` / `bearish` / `neutral` |
| `tier` | string | — | `whale` / `institutional` / `large` / `retail` |
| `min_conviction` | float | 0.0 | 0.0–1.0 |

---

## Known Fixes Applied

| ID | Description |
|---|---|
| C-005 | supabase-py v2 does not expose `.select()` after `.insert()` — generate `snapshot_id` via `uuid4()` in Python before insert |
| C-006 | `options_universe_snapshots.provider` is `NOT NULL` with no default — always pass `"tradier"` explicitly |
| C-007 | `config.py` missing `priority_symbols` property — added `@property` that parses `UNIVERSE_PRIORITY_SYMBOLS` string into `list[str]` |
| C-008 | `stream_eligible` column missing from DB migration — added in `002_universe_symbols_quotes.sql` along with `last_price` and `volume` |
| C-009 | `universe_screener.py` OI-based per-symbol screening replaced by `_fetch_batch_quotes()` batch quotes (Step 3) — screener marked deprecated |
| C-010 | `flow_store.py` was falling back to `SUPABASE_KEY` (anon key) when `SUPABASE_SERVICE_ROLE_KEY` was missing — anon key respects RLS and caused every `flow_episodes` insert to fail with 401/42501. Fixed: removed fallback, `SUPABASE_SERVICE_ROLE_KEY` is now required exclusively. See `docs/FIXES.md` for full details. |

---

## Supabase Critical Rules

1. **Always use `SUPABASE_SERVICE_ROLE_KEY`** for `flow_store.py` inserts — the anon key fails with `42501` due to RLS
2. **Never send `id` fields** for `flow_events` (uuid) or `flow_episodes` (bigserial) — Postgres generates them
3. **No `.select()` chained after `.insert()`** in supabase-py v2 (breaks batch inserts)

### Supabase Key Reference

| Key | Env var | Used by | Bypasses RLS? |
|-----|---------|---------|---------------|
| Anon / Public | `SUPABASE_KEY` | `universe_store.py` (reads only) | ❌ No |
| Service Role | `SUPABASE_SERVICE_ROLE_KEY` | `flow_store.py` (writes) | ✅ Yes |

---

## Important Implementation Notes

### Step 3 — Tradier Single-Symbol Dict Edge Case
When only 1 symbol is in a `/v1/markets/quotes` batch, Tradier returns a **dict** instead of a **list** for `quotes.quote`. The loader handles this:
```python
if isinstance(quotes_raw, dict):
    quotes_raw = [quotes_raw]
```

### Step 3 — Price Field Fallback Order
Tradier quote responses use inconsistent field names. The loader tries in order:
`last` → `last_price` → `close` → `prevclose`

### upsert_symbol_quotes() Timing
`upsert_symbol_quotes()` is called from `load_universe()` BEFORE `save_snapshot()`.
If no active snapshot exists yet (first ever startup), it logs a warning and is a no-op.
The `stream_eligible` flag written by `save_snapshot()` is authoritative — it uses
the `eligible_set` returned by `_fetch_batch_quotes()` directly.

### universe_screener.py
Kept in the repo for reference and backward test compatibility.
`screen_universe()` emits a deprecation warning log if called.
Do NOT re-add a call to it from `load_universe()`.

### flow_store.py — Key Selection
`flow_store.py` is the **only** module that writes options flow data to the DB.
It **must** use `SUPABASE_SERVICE_ROLE_KEY`. Never introduce a fallback to `SUPABASE_KEY` here.
See the Supabase Critical Rules section above.

### volume_premium_factor — OI Fallback
`volume_weighted_premium_factor()` = `total_premium / (open_interest × 100)`, capped 0–1.
Falls back to `0.5` neutral when OI is unavailable from Tradier.
Do not treat 0.5 as a signal — it means OI data was absent.

---

## Environment Variables (Full List)

```
# Auth
SECRET_KEY=
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# Supabase
SUPABASE_URL=
SUPABASE_KEY=                      # anon key — used by universe_store.py (reads)
SUPABASE_SERVICE_ROLE_KEY=          # service role key — REQUIRED by flow_store.py (writes)

# Tradier
TRADIER_API_KEY=
TRADIER_ACCOUNT_ID=
TRADIER_BASE_URL=https://api.tradier.com
TRADIER_STREAM_URL=https://stream.tradier.com

# AI
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GROQ_API_KEY=

# Misc
REDIS_URL=redis://localhost:6379
ALLOWED_ORIGINS=http://localhost:3000

# Universe pipeline
UNIVERSE_PRIORITY_SYMBOLS=SPY,QQQ,AAPL,TSLA,NVDA,MSFT,AMZN,META,GOOGL,AMD
UNIVERSE_BATCH_DELAY_MS=0
UNIVERSE_STREAM_ELIGIBLE_DEFAULT=true
UNIVERSE_MIN_PRICE=1.0
UNIVERSE_MIN_VOLUME=100000
UNIVERSE_QUOTES_BATCH_SIZE=200
UNIVERSE_QUOTES_CONCURRENCY=28
```

---

## Known Issues / Phase 4 TODO

- `GET /api/flow/scan` returns mock data — wire to live `flow_events` Supabase query
- `/api/signals/list` tier filter is pass-through in mock mode — wire to live accumulator in Phase 4
- `volume_premium_factor` falls back to `0.5` when OI is unavailable from Tradier — investigate OI field availability per symbol
- Frontend needs to implement WS pong response (`{"type":"pong"}`) to survive Phase 3 heartbeat
- Load test `/api/signals/list` with 50 concurrent authenticated users
- WebSocket fan-out benchmark with 50+ subscribers
