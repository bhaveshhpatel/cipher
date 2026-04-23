# Cipher — Claude Context File

## Project Overview

**Cipher** is an institutional options flow intelligence platform with the tagline *"Decode the Market."* It detects real-time whale/institutional options flow, scores signals using a composite engine, and runs multi-agent AI swarm simulations to generate BUY/SELL/HOLD verdicts.

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

## Repository Structure

```
cipher/
├── .github/
│   └── workflows/
│       ├── backend.yml        # CI only — syntax check; NO deploy steps
│       └── frontend.yml       # Vercel deploy via CLI
├── backend/
│   ├── main.py                # FastAPI app — startup loads universe from DB first
│   ├── config.py              # pydantic-settings v2 — uses model_config = SettingsConfigDict(...)
│   ├── requirements.txt       # pydantic[email] ensures email-validator is installed
│   ├── requirements-dev.txt
│   ├── nixpacks.toml
│   ├── runtime.txt            # python-3.11.9
│   ├── .python-version        # 3.11.9
│   ├── core/
│   │   ├── auth.py
│   │   └── async_bus.py
│   ├── parsers/
│   │   ├── options_flow_parser.py
│   │   ├── bid_ask_classifier.py
│   │   └── trade_type_detector.py
│   ├── signals/
│   │   ├── repetition_accumulator.py
│   │   ├── backtest_validator.py
│   │   ├── midcap_screener.py
│   │   └── composite_signal_engine.py
│   ├── simulation/
│   │   ├── swarm_engine.py
│   │   └── ensemble_runner.py
│   ├── execution/
│   │   └── trade_executor.py
│   ├── services/
│   │   ├── tradier_stream.py  # Market-hours guard + session_ticks backoff (commit 9a32d4b)
│   │   ├── symbols_loader.py  # CBOE CSV fetch + Tradier validation + fallbacks
│   │   └── universe_store.py  # Supabase snapshot read/write
│   ├── migrations/
│   │   └── 001_options_universe.sql  # DB schema for universe snapshots
│   ├── routers/
│   │   ├── auth.py
│   │   ├── flow.py
│   │   ├── simulation.py
│   │   ├── ws.py
│   │   └── smart_signals.py
│   └── tests/
│       ├── conftest.py
│       ├── test_auth_flow.py
│       ├── test_flow_and_stats.py
│       ├── test_simulation_and_ws.py
│       ├── test_tradier_stream.py  # F1–F9 + MH-1–7 + BF-1–3
│       ├── test_symbols_loader.py
│       └── test_universe_store.py
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── globals.css
│   │   │   ├── page.tsx
│   │   │   └── dashboard/page.tsx
│   │   ├── components/
│   │   │   ├── CipherLogo.tsx
│   │   │   └── dashboard/
│   │   │       ├── SignalFeed.tsx
│   │   │       ├── FlowTable.tsx
│   │   │       ├── SimulationPanel.tsx
│   │   │       ├── CompositeCard.tsx
│   │   │       └── StreamStatsBar.tsx
│   │   ├── hooks/
│   │   │   ├── useAuth.ts
│   │   │   ├── useSignalStream.ts
│   │   │   ├── useFlow.ts
│   │   │   └── useSimulation.ts
│   │   ├── lib/api.ts
│   │   └── types/index.ts
│   ├── package.json
│   ├── next.config.mjs
│   ├── tailwind.config.ts
│   ├── tsconfig.json
│   └── vercel.json
├── docs/
│   ├── BACKLOG.md
│   ├── features.md
│   ├── regression-test-plan.md
│   └── specs.md
├── railway.toml
└── claude.md
```

---

## Environment Variables

### Backend (Railway dashboard env vars)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | JWT signing secret |
| `ALGORITHM` | JWT algorithm (default: HS256) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token TTL (default: 1440) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase anon key |
| `SUPABASE_SERVICE_KEY` | Supabase service role key |
| `TRADIER_API_KEY` | Tradier brokerage API key |
| `TRADIER_ACCOUNT_ID` | Tradier account ID |
| `TRADIER_BASE_URL` | Tradier REST base (default: `https://api.tradier.com`) |
| `TRADIER_STREAM_URL` | Tradier stream base (default: `https://stream.tradier.com`) |
| `OPENAI_API_KEY` | OpenAI API key for swarm agents |
| `ANTHROPIC_API_KEY` | Anthropic API key (reserved) |
| `REDIS_URL` | Redis connection (default: `redis://localhost:6379`) |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |

### Frontend (Vercel dashboard env vars)

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `https://cipher-production-6cd8.up.railway.app` |
| `NEXT_PUBLIC_WS_URL` | `wss://cipher-production-6cd8.up.railway.app/` |

> ⚠️ `NEXT_PUBLIC_API_URL` must NOT have a trailing slash. `api.ts` strips it defensively but the env var should be clean.

> ⚠️ Do NOT add frontend env vars to `vercel.json` or GitHub Actions.

---

## Key Business Logic

### Tradier Stream — Market-Hours Guard & Backoff Fix (C-005, commit 9a32d4b)

#### `_is_market_hours()` helper

Checks US Eastern Time Mon–Fri 09:30–16:00 using `zoneinfo.ZoneInfo("America/New_York")` (Python stdlib — no extra deps).

```python
def _is_market_hours() -> bool:
    from zoneinfo import ZoneInfo
    import datetime
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:          # Saturday / Sunday
        return False
    open_  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_ = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_ <= now < close_
```

**Behaviour:**
- Market **closed** → log once + sleep 60s before next check. Zero reconnect spam. `mode = "market_closed"`.
- Market **open** → proceed to token fetch and stream connection as normal.

Example log when closed:
```
Market closed (ET: 01:28 PDT Wed) — sleeping 60s before next check
```

#### session_ticks-aware backoff reset

**Old:** `reconnect_attempt` reset to `0` on every successful connection, even instant closes with no data → backoff always restarted from ~0–5s → overnight reconnect spam.

**New:**
- `session_ticks > 0` (real data received) → reset `reconnect_attempt = 0` ✅
- `session_ticks == 0` (connected, instant close, no data) → `attempt += 1` → backoff grows up to 60s cap

#### Reconnect State Machine (current)

```
startup
  └─ while True:
      ├─ _is_market_hours()            ← if closed: log + sleep 60s + continue
      │
      ├─ _get_session_token()          ← fresh token every iteration
      │    ├─ retry up to 3x on transient network error (2s gap)
      │    └─ return None on 401 (bad key) or exhausted retries
      │
      ├─ if no token:
      │    ├─ start _demo_mode_once() as background asyncio.Task
      │    ├─ exponential backoff (5s base, 60s cap, jitter)
      │    └─ continue → retry token fetch
      │
      ├─ if token:
      │    ├─ cancel demo task (if running)
      │    ├─ open httpx streaming POST to Tradier
      │    │
      │    ├─ if stream 401 (expired token race):
      │    │    ├─ fast retry (1s) for first 4 consecutive
      │    │    └─ slow backoff after 5 consecutive (likely bad key)
      │    │
      │    ├─ if connected:
      │    │    ├─ set mode = "live"
      │    │    ├─ read lines via _guarded_lines() [30s idle watchdog]
      │    │    ├─ increment session_ticks per data line
      │    │    └─ process each trade → signal pipeline
      │    │
      │    └─ on any error (network, timeout, idle):
      │         ├─ increment reconnect counter
      │         ├─ set mode = "reconnecting"
      │         ├─ if session_ticks > 0 → reset attempt = 0
      │         ├─ else → attempt += 1
      │         └─ exponential backoff → continue
      │
      └─ loop forever
```

#### Stats — mode values

`mode` field in `get_stats()`: `starting` | `live` | `demo` | `reconnecting` | `market_closed`

#### Critical implementation notes

- **Session token POST requires `data={}` (NOT `content=b""`)** — httpx must send `Content-Length: 0` matching `curl -d ""`. Using `content=b""` omits `Content-Length` and Tradier silently fails.
- Session tokens expire on stream close — **must re-fetch on every reconnect**.
- Demo mode is a cancellable `asyncio.Task` — cancelled immediately when live connection established.

---

### Options Universe — Persistence Layer

The full universe of tradeable options symbols (~8,000 tickers) is persisted in Supabase.
This eliminates cold-start delays, Tradier downtime blind spots, and audit gaps.

#### Startup Resolution Order (`main.py → _resolve_startup_universe()`)

```
App startup
  │
  ├─ 1. Query DB for latest active snapshot (< 24h old)
  │       └─ Found & fresh → LOAD from DB → stream starts instantly ✅
  │
  ├─ 2. No fresh snapshot in DB
  │       └─ Fetch from CBOE CSV + validate via Tradier in parallel batches (semaphore=20)
  │             ├─ Success → SAVE to DB → mark active → start stream ✅
  │             └─ Tradier/CBOE down → load LAST snapshot (any age) from DB
  │                   └─ None ever in DB → SEED_SYMBOLS fallback (16 symbols) ✅
  │
  └─ 3. Background refresh every 24h (_universe_refresh_loop)
          └─ Fetch + validate new universe
                ├─ Success → SAVE new snapshot → deactivate old one ✅
                └─ Failure → keep current active snapshot, log warning ✅
```

#### DB Tables — Real Production Schema

```sql
-- One row per validated universe snapshot
options_universe_snapshots (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  symbol_count   INT NOT NULL DEFAULT 0,
  provider       TEXT NOT NULL,                        -- e.g. 'tradier'
  source         TEXT NOT NULL,                        -- 'tradier_validated' | 'seed_fallback' | 'cache'
  is_active      BOOLEAN NOT NULL DEFAULT false,
  refresh_reason TEXT NOT NULL DEFAULT 'startup',
  meta           JSONB NOT NULL DEFAULT '{}',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)

-- Individual symbols per snapshot (normalized, batch-inserted in 500s)
options_universe_symbols (
  snapshot_id  UUID → options_universe_snapshots(id) ON DELETE CASCADE,
  symbol       TEXT,
  PRIMARY KEY (snapshot_id, symbol)
)
```

#### Key Design Decisions
- Refresh cadence: **every 24h** — options-active universe changes slowly
- Keep **last 7 snapshots** — auto-purge older ones via ON DELETE CASCADE
- **Never block stream on refresh** — background asyncio task
- `source` field distinguishes full coverage vs degraded fallback
- `provider` field is always `"tradier"` — the only data provider currently integrated. **NOT NULL, no default — must always be sent explicitly in insert.**
- Partial unique index enforces only ONE active snapshot at the DB level
- **snapshot_id generated via `uuid4()` in Python** — passed explicitly in the insert payload; never read back from insert response (see Known Issues below)

#### Services

| File | Responsibility |
|---|---|
| `services/symbols_loader.py` | Fetches optionable symbols from CBOE CSV (no auth), validates each via Tradier expirations (20 concurrent), handles all edge cases |
| `services/universe_store.py` | Supabase read/write — `load_fresh_snapshot()`, `load_any_snapshot()`, `save_snapshot()` (batched inserts of 500, prunes to 7 snapshots) |
| `migrations/001_options_universe.sql` | DDL for both tables + 3 indexes including partial unique index |

---

### Signal Pipeline

1. `_resolve_startup_universe()` loads symbol list from DB (or CBOE/seed fallback)
2. Tradier WebSocket emits raw trade ticks for those symbols
3. `options_flow_parser.py` parses ticks → `OptionsFlowEvent`
4. `bid_ask_classifier.py` classifies fill aggressiveness
5. `trade_type_detector.py` classifies trade type (SWEEP / BLOCK / SPLIT / SINGLE)
6. `repetition_accumulator.py` groups trades into `RepetitionEpisode` (30-min window, min 3 trades, min $50K premium)
7. `composite_signal_engine.py` scores: `composite = flow_score × 0.6 + backtest_score × 0.4`
8. Signal published to `async_bus` → broadcast to all WS subscribers

### Composite Score Formula

```
flow_score     = min(1.0, (total_premium / 10M) × 0.65 + is_accelerating × 0.15 + min(trade_count/20, 0.20))
backtest_score = historical win-rate by (ticker, contract_type, DTE bucket, tier)
composite      = flow_score × 0.6 + backtest_score × 0.4

Recommendation:
  composite >= 0.65 AND BULLISH → BUY
  composite >= 0.65 AND BEARISH → SELL
  else                          → HOLD
```

### Auth Flow

```
Register:
  POST /api/auth/register (email, password)
    → if SUPABASE_URL + SUPABASE_SERVICE_KEY set: create user via admin API
    → else: store hash in in-memory _users dict (resets on redeploy)

Login:
  POST /api/auth/token (username=email, password)
    → if SUPABASE_URL + SUPABASE_KEY set: sign_in_with_password
        → credential error → 401
        → service error → 503
    → else: verify against _users dict
    → success → JWT {"sub": email}
```

> ⚠️ In-memory `_users` resets on every Railway deploy. Supabase must be configured for persistent auth.

### CORS / Preflight Notes

- `main.py` uses FastAPI `CORSMiddleware` with `allow_methods=["*"]`
- `routers/auth.py` has explicit `@router.options("/register")` and `@router.options("/token")` handlers returning 200 to guarantee preflight never returns 400
- `frontend/src/lib/api.ts` strips trailing slash from `NEXT_PUBLIC_API_URL` defensively

### Swarm Simulation

Six GPT-4o-mini agents: Momentum Trader, Contrarian Analyst, Fundamental Analyst, Technical Analyst, Macro Strategist, Risk Manager.

### Alert Levels

| Level | Meaning |
|---|---|
| `CONVICTION` | Highest confidence |
| `STRONG_SIGNAL` | High confidence |
| `ALERT` | Moderate |
| `WATCH` | Low/monitoring |

---

## Supabase Schema

### Tables Live in Production (`cipher-database` — `kpajucxqlrteckfuafvq`)

| Table | Purpose | Migration |
|---|---|---|
| `options_universe_snapshots` | One row per validated universe snapshot | `001_options_universe.sql` |
| `options_universe_symbols` | Individual symbols per snapshot (normalized) | `001_options_universe.sql` |

**`options_universe_snapshots` columns (production):**

| Column | Type | Nullable | Default |
|---|---|---|---|
| `id` | UUID | NO | `gen_random_uuid()` |
| `fetched_at` | TIMESTAMPTZ | NO | `now()` |
| `symbol_count` | INT | NO | `0` |
| `provider` | TEXT | **NO** | **none — must send explicitly** |
| `source` | TEXT | NO | none — must send explicitly |
| `is_active` | BOOLEAN | NO | `false` |
| `refresh_reason` | TEXT | NO | `'startup'` |
| `meta` | JSONB | NO | `'{}'` |
| `created_at` | TIMESTAMPTZ | NO | `now()` |

> Auth tables are managed by Supabase Auth (built-in `auth.users`).

---

## Current State & Known Gaps

| Area | Status |
|---|---|
| Frontend deployment | ✅ Live on Vercel |
| Backend deployment | ✅ Live on Railway (native GitHub integration) |
| Backend startup crash (pydantic-settings) | ✅ Fixed 2026-04-23 |
| email-validator missing | ✅ Fixed 2026-04-23 |
| CORS preflight 400 on /register | ✅ Fixed 2026-04-23 |
| Auth — register + login | ✅ Fixed 2026-04-23 |
| Railway deploy pipeline | ✅ Fixed 2026-04-23 |
| Python version pinning | ✅ Fixed 2026-04-23 |
| Tradier stream 401 loop | ✅ Fixed 2026-04-23 |
| Tradier session token Content-Length | ✅ Fixed 2026-04-23 |
| Tradier stream | ✅ Live |
| Options universe persistence | ✅ Live 2026-04-23 |
| universe_store AttributeError on .select() | ✅ Fixed 2026-04-23 — uuid4 snapshot_id |
| Tradier overnight reconnect spam | ✅ Fixed 2026-04-23 — market-hours guard + session_ticks backoff (C-005) |
| **universe_store provider NOT NULL crash** | ✅ **Fixed 2026-04-23 — send provider="tradier" in insert (C-006)** |
| Flow data | Live Tradier stream running; demo mode fallback if key missing |
| Supabase | Auth working; universe tables live; signal storage not yet wired |
| Redis | In config but not integrated |
| Frontend styling | Inline styles throughout dashboard; Tailwind installed but underused |
| Trade execution | `trade_executor.py` exists but not wired into signal flow |
| Anthropic key | In config but not used |

---

## Known Issues / Gotchas

### supabase-py v2 — No `.select()` after `.insert()`

`supabase==2.15.2` returns a `SyncQueryRequestBuilder` from `.insert()` which does **not** expose a `.select()` method. Chaining `.insert().select().execute()` raises:
```
AttributeError: 'SyncQueryRequestBuilder' object has no attribute 'select'
```
**Fix applied in `universe_store.py`:** Generate `snapshot_id = str(uuid4())` in Python before the insert and pass it explicitly in the payload. The ID is known ahead of time so we never need to read it back from the insert response. This is stable across all supabase-py v2 versions.

**Rule:** Never chain `.select()` after `.insert()` anywhere in this codebase with `supabase==2.15.2`.

### options_universe_snapshots — `provider` column is NOT NULL with no default

The `provider` column was added to the production table outside the original migration and has **no default value**. Every insert into `options_universe_snapshots` **must** include `"provider": "tradier"` explicitly or the DB will reject with:
```
null value in column "provider" of relation "options_universe_snapshots" violates not-null constraint
```
**Fix applied in `universe_store.py` (`_sync_save_snapshot`):** `"provider": "tradier"` is now always included in the insert dict. The migration SQL (`001_options_universe.sql`) has also been updated to reflect the full production schema.

**Rule:** Any future insert into `options_universe_snapshots` must include `provider`.

### Tradier Stream — Off-Hours Behaviour

Outside market hours (Mon–Fri 09:30–16:00 ET, weekends), the stream loop:
- Logs `Market closed (ET: ...) — sleeping 60s before next check` once per minute
- Sets `mode = "market_closed"` in stats / `/health`
- Does NOT attempt token fetch or stream connection
- Reconnect backoff only resets when `session_ticks > 0` (real data received), so off-hours polling stays at the 60s cap rather than spamming rapid retries

---

## CI/CD Architecture

### Backend — Railway Native GitHub Integration

Railway deploys automatically on push to `main`. No CLI, no token, no GitHub Actions deploy step.

**Root `railway.toml`:**
```toml
[build]
builder = "nixpacks"
rootDirectory = "backend"
watchPatterns = ["backend/**"]

[deploy]
startCommand = "uvicorn main:app --host 0.0.0.0 --port $PORT"
healthcheckPath = "/health"
healthcheckTimeout = 30
restartPolicyType = "on_failure"
```

**Python version pinning (3 signals for Railway):**
- `backend/nixpacks.toml` — `NIXPACKS_PYTHON_VERSION=3.11`
- `backend/runtime.txt` — `python-3.11.9`
- `backend/.python-version` — `3.11.9`

### Backend — GitHub Actions (`backend.yml`)
CI only — syntax check on all `.py` files. No deploy steps.

### Frontend — Vercel via GitHub Actions (`frontend.yml`)
Vercel CLI deploy on push to `main` when `frontend/**` changes.

---

## Python Dependencies

- `requirements.txt` — production deps only (no pytest)
- `requirements-dev.txt` — dev/test deps: `pytest`, `pytest-asyncio`
- Key versions: `fastapi==0.115.12`, `pydantic[email]==2.11.4`, `pydantic-settings==2.9.1`, `pandas==2.2.3`, `numpy==2.2.5`, `supabase==2.15.2`
- All packages have prebuilt wheels for both cp311 and cp313

## pydantic-settings v2 Notes

- `config.py` uses `model_config = SettingsConfigDict(...)` — NOT the old inner `class Config`
- The old `class Config` pattern causes a `PydanticUserError` crash on startup in pydantic-settings ≥ 2.3
- `pydantic[email]` extra required for `EmailStr` fields in request models

---

## Coding Conventions

- Backend: Python 3.11, async/await throughout, pydantic models for all I/O
- Frontend: TypeScript strict mode, functional components, custom hooks
- Auth guard: `Depends(get_current_user)` (BE) / `useAuth` hook redirect (FE)
- Monorepo: `backend/` and `frontend/` as siblings at repo root
- No ORM — direct Supabase REST/postgrest calls
- **Do NOT chain `.select()` after `.insert()` with supabase-py v2** — use `uuid4()` pattern instead
- **Do NOT add market-hours logic using third-party libs** — use `zoneinfo` (stdlib) only
- **Always include `provider` in any insert to `options_universe_snapshots`** — NOT NULL, no default

---

## How to Run Locally

### Backend
```bash
cd backend
pip install -r requirements.txt
pip install -r requirements-dev.txt  # for tests
cp .env.example .env
uvicorn main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

---

## Deployment URLs

| Target | Platform | URL |
|---|---|---|
| Backend | Railway | `https://cipher-production-6cd8.up.railway.app` |
| Frontend | Vercel | Vercel project: `bhaveshhpatels-projects/cipher` |
| Supabase DB | Supabase | Project ID: `kpajucxqlrteckfuafvq` (cipher-database, us-west-2) |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-04-23 | **C-006: universe_store provider NOT NULL fix** — added `"provider": "tradier"` to `_sync_save_snapshot` insert payload; updated `001_options_universe.sql` to match full production schema (provider, refresh_reason, meta, created_at columns); updated claude.md schema table and Known Issues. |
| 2026-04-23 | **C-005: Tradier market-hours guard + session_ticks backoff fix** — `_is_market_hours()` helper (ET timezone, stdlib), market-closed guard at top of reconnect loop (60s sleep, one log/min), `session_ticks`-aware backoff reset, `mode = market_closed` in stats. Eliminates overnight reconnect spam. 10 new test cases (MH-1–7, BF-1–3). |
| 2026-04-23 | **Fixed universe_store AttributeError** — replaced broken `.insert().select()` chain with `uuid4()` pre-generated snapshot_id |
| 2026-04-23 | **Options universe persistence shipped** — `symbols_loader.py`, `universe_store.py`, `migrations/001_options_universe.sql`, updated `main.py` startup + 24h background refresh loop, 30 test cases |
| 2026-04-23 | **Supabase migration applied** — `options_universe_snapshots` + `options_universe_symbols` tables live |
| 2026-04-23 | **Fixed Tradier session token Content-Length** — changed `content=b""` to `data={}` |
| 2026-04-23 | **Fixed Tradier 401 infinite loop** — 401 guards + demo-mode fallback |
| 2026-04-23 | **Added Tradier regression tests** — `test_tradier_stream.py` (F1–F9) |
| 2026-04-23 | **Fixed CORS preflight 400** — explicit OPTIONS handlers + trailing slash strip |
| 2026-04-23 | **Fixed missing email-validator** — `pydantic[email]` |
| 2026-04-23 | **Fixed runtime startup crash** — pydantic-settings v2 migration |
| 2026-04-23 | **Fixed pip build failure** — deps upgraded + runtime.txt + .python-version |
| 2026-04-23 | **Fixed Railway deploy pipeline** — native GitHub integration |
| 2026-04-23 | **Fixed auth register bug** — Supabase credential errors return 401 |
| 2026-04-22 | Fixed frontend CI/CD Vercel path bug |
| 2026-04-22 | Frontend confirmed live — login page visible |
