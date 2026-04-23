# Cipher — Technical Specifications

> Last updated: 2026-04-23

---

## System Overview

Cipher is an institutional options flow intelligence platform. It ingests real-time options trade data from Tradier's streaming API, scores signals through a composite engine, and runs a multi-agent AI swarm simulation to generate BUY/SELL/HOLD verdicts.

**Live URLs**
- Frontend: Vercel (bhaveshhpatels-projects/cipher)
- Backend: Railway (`cipher-production-6cd8.up.railway.app`)
- Database: Supabase `cipher-database` — project ID `kpajucxqlrteckfuafvq` (us-west-2)

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Backend | FastAPI (Python 3.11), async WebSockets |
| Auth | JWT (`python-jose` + `passlib` bcrypt) |
| Streaming | Tradier WebSocket → async in-process event bus |
| AI Engine | OpenAI GPT-4o-mini (6-agent swarm) |
| Database | Supabase (PostgreSQL) — universe snapshots + auth |
| Deploy | Railway (BE) + Vercel (FE) |
| CI/CD | GitHub Actions |

---

## Options Universe Persistence (Added 2026-04-23)

### Overview

The full universe of tradeable options symbols (~8,000 tickers) is persisted in Supabase. This eliminates cold-start delays, Tradier downtime blind spots, and audit gaps.

### Startup Resolution Order

`main.py` → `_resolve_startup_universe()` runs at app startup:

```
App startup
  │
  ├─ 1. Query DB for latest active snapshot (< 24h old)
  │       └─ Found & fresh → LOAD from DB → stream starts in < 1s ✅
  │
  ├─ 2. No fresh snapshot
  │       └─ Fetch from Tradier + validate (parallel, semaphore=20)
  │             ├─ Success → SAVE to DB → mark active → start stream ✅
  │             └─ Tradier down → load LAST snapshot (any age) from DB
  │                   └─ None ever in DB → SEED_SYMBOLS (16 tickers) ✅
  │
  └─ 3. Background refresh every 24h (_universe_refresh_loop asyncio.Task)
          └─ Success → SAVE new snapshot → deactivate old one ✅
          └─ Failure → keep current active snapshot, log warning ✅
```

### DB Schema

```sql
-- One row per validated universe snapshot
CREATE TABLE options_universe_snapshots (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  symbol_count  INT NOT NULL,
  source        TEXT NOT NULL
    CHECK (source IN ('tradier_validated', 'seed_fallback', 'cache')),
  is_active     BOOLEAN NOT NULL DEFAULT true
);

-- Individual symbols per snapshot (normalized)
CREATE TABLE options_universe_symbols (
  snapshot_id  UUID NOT NULL
    REFERENCES options_universe_snapshots(id) ON DELETE CASCADE,
  symbol       TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, symbol)
);

-- Partial unique index: only 1 active snapshot at a time (DB-level enforcement)
CREATE UNIQUE INDEX idx_universe_snapshots_single_active
  ON options_universe_snapshots (is_active)
  WHERE is_active = true;
```

**Migration file:** `backend/migrations/001_options_universe.sql` — applied to production 2026-04-23.

### Key Design Decisions

| Decision | Rationale |
|---|---|
| 24h refresh cadence | Options-active universe changes slowly (IPOs, delistings). Daily is sufficient. |
| Keep last 7 snapshots | Audit / debugging. ON DELETE CASCADE auto-purges older ones. |
| Never block stream on refresh | Background asyncio task — stream runs with current universe while refresh is in-flight. |
| `source` field | Distinguishes `tradier_validated` (full coverage) vs `seed_fallback` (degraded) vs `cache` (loaded from DB). |
| Partial unique index | DB-level safety net: only one `is_active = true` row can ever exist. |
| Batch insert (500) | Supabase REST performance — bulk inserts chunked to avoid payload limits. |

### Services

| File | Responsibility |
|---|---|
| `services/symbols_loader.py` | `load_universe(settings)` — fetches optionable symbols from Tradier REST, validates each via expiration check (20 concurrent via semaphore), handles 401, network errors, empty results, single-dict Tradier responses, lowercase symbols. Returns `(List[str], source_str)`. |
| `services/universe_store.py` | `load_fresh_snapshot(max_age_hours=24)`, `load_any_snapshot()`, `save_snapshot(symbols, source)`. Batches symbol inserts in 500s. Prunes to last 7 snapshots. |
| `migrations/001_options_universe.sql` | DDL for both tables + 3 indexes. Applied to `cipher-database`. |

### Test Coverage

| File | Cases | What's covered |
|---|---|---|
| `tests/test_symbols_loader.py` | 20 | Success path, 401, network error, empty results, single-dict Tradier quirk, symbol normalization, exception isolation per symbol, all 6 `load_universe()` fallback scenarios |
| `tests/test_universe_store.py` | 10 | Fresh snapshot hit, no snapshot, stale fallback, empty symbol guard, insert failure, DB exception, prune-when-over-7, batch-insert for >500 symbols |

---

## Signal Pipeline

```
_resolve_startup_universe()   ← loads symbol list from DB (or fallback)
  └─ Tradier WebSocket (subscribed to universe symbols)
       └─ options_flow_parser.py       → OptionsFlowEvent
            └─ bid_ask_classifier.py   → fill aggressiveness
            └─ trade_type_detector.py  → SWEEP / BLOCK / SPLIT / SINGLE
       └─ repetition_accumulator.py    → RepetitionEpisode
            (30-min window, min 3 trades, min $50K premium)
       └─ composite_signal_engine.py   → composite score
            (flow_score × 0.6 + backtest_score × 0.4)
       └─ async_bus                    → broadcast to WebSocket subscribers
```

---

## Tradier Stream Architecture (Updated 2026-04-23)

### Overview

The Tradier stream connection is managed by `backend/services/tradier_stream.py`. The module is designed for production resilience — it never exits permanently and always attempts to recover a live connection.

### Session Token Lifecycle

Tradier requires a fresh session token for every stream connection. Tokens are obtained via:
```
POST /v1/markets/events/session
Authorization: Bearer <TRADIER_API_KEY>
Content-Length: 0   ← required (data={}, equivalent to curl -d "")
```

**Critical:** Session tokens expire when the stream connection closes. The token **must** be re-fetched on every reconnect — reusing a token after any disconnect will produce a 401.

### Reconnection State Machine

```
startup
  └─ while True:
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
      │    │    └─ process each trade → signal pipeline
      │    │
      │    └─ on any error (network, timeout, idle):
      │         ├─ increment reconnect counter
      │         ├─ set mode = "reconnecting"
      │         └─ exponential backoff → continue
      │
      └─ loop forever
```

### Idle Watchdog

Tradier sends bare `\n` keepalives. If no line (including keepalives) is received within **30 seconds**, `_guarded_lines()` raises `asyncio.TimeoutError`, which triggers an immediate reconnect.

```python
async def _guarded_lines(resp):
    aiter = resp.aiter_lines().__aiter__()
    while True:
        line = await asyncio.wait_for(aiter.__anext__(), timeout=30.0)
        yield line
```

### Backoff Formula

```python
def _backoff(attempt: int) -> float:
    delay = min(60.0, 5.0 * (2 ** attempt))
    return random.uniform(0, delay)  # full jitter
```

| Attempt | Max delay |
|---------|-----------|
| 0 | 5s |
| 1 | 10s |
| 2 | 20s |
| 3 | 40s |
| 4+ | 60s (cap) |

### Demo Mode

Demo mode runs as a cancellable `asyncio.Task` (`_demo_mode_once()`). It emits synthetic signals at random intervals and is immediately cancelled when a live Tradier connection is established.

Demo mode is only entered when:
1. `TRADIER_API_KEY` is not set (permanent demo until restart)
2. Session token cannot be obtained after retries (temporary demo until token recovers)

### Stats

The module exposes `get_stats()` returning:
```json
{
  "active_symbols": 8012,
  "ticks": 1420,
  "classified": 893,
  "signals": 47,
  "errors": 2,
  "reconnects": 1,
  "mode": "live"
}
```
`mode` values: `starting` | `live` | `demo` | `reconnecting`

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TRADIER_API_KEY` | Yes (for live) | Bearer token for Tradier API |
| `TRADIER_ACCOUNT_ID` | Yes (for trading) | Tradier brokerage account ID |
| `TRADIER_BASE_URL` | No | Default: `https://api.tradier.com` |
| `TRADIER_STREAM_URL` | No | Default: `https://stream.tradier.com` |

---

## Supabase Schema

### Live Tables (`cipher-database` — `kpajucxqlrteckfuafvq`)

| Table | Purpose | Migration |
|---|---|---|
| `options_universe_snapshots` | One row per validated ~8,000-symbol universe snapshot | `001_options_universe.sql` |
| `options_universe_symbols` | Individual symbols per snapshot (normalized, ON DELETE CASCADE) | `001_options_universe.sql` |

> Auth tables are managed by Supabase Auth (built-in `auth.users`).

---

## Authentication

- JWT-based, issued on login, stored client-side
- `ACCESS_TOKEN_EXPIRE_MINUTES`: 1440 (24 hours)
- Protected routes: all `/api/*` except `/api/auth/register` and `/api/auth/login`
- Supabase used for user persistence; signal storage not yet wired

---

## Frontend Proxy

Next.js App Router catch-all route at `app/api/[...path]/route.ts` proxies all `/api/*` calls to the Railway backend.

**Key implementation notes (updated 2026-04-23):**
- Body read as `req.text()` before forwarding — avoids `ReadableStream` / `duplex: half` issues on Vercel's Node runtime
- Next.js 15: `params` must be awaited (`Promise<{ path: string[] }>`)
- `typescript.ignoreBuildErrors: true` in `next.config.js` — proxy uses intentional casts that TS flags but are runtime-correct

---

## Deployment

### Backend (Railway)
- Nixpacks build from `backend/`
- Entry: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Auto-deploys on push to `main`
- Env vars set in Railway dashboard

### Frontend (Vercel)
- Next.js project root: `frontend/`
- Auto-deploys on push to `main`
- Env vars: `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_WS_URL`

### CI/CD
- GitHub Actions: `.github/workflows/`
- Runs on push to `main` and PRs
