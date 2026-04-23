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
| Database | Supabase (PostgreSQL) — universe + signal persistence + auth |
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

### supabase-py v2 — uuid4 snapshot_id Pattern

`supabase==2.15.2` does **not** support chaining `.select()` after `.insert()`. Doing so raises:
```
AttributeError: 'SyncQueryRequestBuilder' object has no attribute 'select'
```
**Fix:** Generate `snapshot_id = str(uuid4())` in Python before the insert and pass it explicitly in the payload. The ID is known ahead of time — no need to read it back from the response.

> **Rule:** Never chain `.select()` after `.insert()` anywhere in this codebase with `supabase==2.15.2`.

### Test Coverage

| File | Cases | What's covered |
|---|---|---|
| `tests/test_symbols_loader.py` | 20 | Success path, 401, network error, empty results, single-dict Tradier quirk, symbol normalization, exception isolation per symbol, all 6 `load_universe()` fallback scenarios |
| `tests/test_universe_store.py` | 10 | Fresh snapshot hit, no snapshot, stale fallback, empty symbol guard, insert failure, DB exception, prune-when-over-7, batch-insert for >500 symbols, uuid4 pre-generated id passed in payload, no `.select()` chained after `.insert()` |

---

## Flow Store — DB Signal Persistence (Fixed 2026-04-23)

### Overview

`services/flow_store.py` is the **only** module that writes options flow data to Supabase. It subscribes to the `db_writer` channel on the async event bus and persists:

1. **Signal episodes** → `flow_episodes` table (immediate write on every qualifying signal)
2. **Raw classified ticks** → `flow_events` table (batched every 5 seconds)

### ID Generation Contract

> **Critical rule:** Neither `flow_events` nor `flow_episodes` rows are ever sent with an `id` field.
> Postgres generates IDs server-side:
> - `flow_events.id` → `uuid` via `DEFAULT gen_random_uuid()`
> - `flow_episodes.id` → `bigserial` (auto-increment)
>
> Sending a client-provided `id` will cause a 400 / schema mismatch error. Never add `id` to any row dict in `flow_store.py`.

### flow_episodes Schema

```sql
CREATE TABLE flow_episodes (
  id             BIGSERIAL PRIMARY KEY,          -- Postgres-generated, never sent by client
  ticker         TEXT NOT NULL,
  direction      TEXT,                           -- REPEAT_BUY / REPEAT_SELL
  contract_type  TEXT,                           -- CALL / PUT
  strike         NUMERIC,
  expiry         TEXT,
  total_premium  NUMERIC,
  trade_count    INT,
  alert_level    TEXT,                           -- WATCH / ALERT / STRONG_SIGNAL / CONVICTION
  is_accelerating BOOLEAN DEFAULT false,
  seed_episode   TEXT,
  signal_ts      TEXT,
  created_at     TIMESTAMPTZ DEFAULT now()
);
```

### flow_events Schema

```sql
CREATE TABLE flow_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- Postgres-generated, never sent
  ticker          TEXT NOT NULL,
  contract_type   TEXT,
  strike          NUMERIC,
  expiry          TEXT,
  premium         NUMERIC,
  trade_type      TEXT DEFAULT 'UNKNOWN',
  sentiment       TEXT DEFAULT 'UNKNOWN',
  influence_tier  TEXT DEFAULT 'UNKNOWN',
  conviction_score NUMERIC DEFAULT 0.0,
  is_golden_sweep BOOLEAN DEFAULT false,
  created_at      TIMESTAMPTZ DEFAULT now()
);
```

### Bus Integration

```
bus.publish_all(signal_dict)
  └── channel: "db_writer"
        └── flow_store._bus_signal_listener()
              └── if signal["type"] == "signal":
                    → persist_flow_episode(signal["data"])
                          → _insert_rows("flow_episodes", [row])
```

### Batching Strategy (flow_events)

- `persist_flow_event(ev_dict)` appends to `_flow_event_buffer` (in-memory list)
- `_flush_flow_events()` runs every `_FLUSH_INTERVAL = 5` seconds
- Batch is atomically drained: `batch = buf.copy(); buf.clear()`
- On flush failure: data is logged as lost (no retry queue — by design for simplicity)

### Logging Contract

All log lines in `flow_store.py` use **f-strings**, never `%`-style formatting.

Reason: `%`-style formatting defers evaluation to the logging framework. If any value is `None` and the format specifier is numeric (e.g. `%,.0f`), the logging call raises `TypeError` at runtime, silently dropping the log line and potentially crashing the writer.

```python
# CORRECT — f-string evaluated immediately, None renders as "None"
log.info(f"[flow_store] flow_episode saved: {row['ticker']} prem=${(row['total_premium'] or 0):,.0f}")

# WRONG — crashes if total_premium is None
log.info("[flow_store] saved prem=$%,.0f", row['total_premium'])
```

### No-op Behavior

If `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_KEY` is not set, `start_flow_writer()` returns immediately with a warning log. No DB writes are attempted and no exceptions are raised.

### Test Coverage

| File | Cases | What's covered |
|---|---|---|
| `tests/test_flow_store.py` | 8 | Episode row schema (no old `composite_signals` columns), event row has no `id`, sparse input defaults, f-string log with None, buffer drain, no-op without env vars, `_insert_rows` called with `"flow_episodes"` not `"composite_signals"`, `persist_flow_event` buffers without network call |

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
       └─ async_bus                    → fan-out:
            ├── "signals"   → ws.py → WebSocket clients
            └── "db_writer" → flow_store.py → Supabase (flow_episodes + flow_events)
```

---

## Tradier Stream Architecture (Updated 2026-04-23)

### Overview

The Tradier stream connection is managed by `backend/services/tradier_stream.py`. The module is designed for production resilience — it never exits permanently and always attempts to recover a live connection.

### Market-Hours Guard (Added 2026-04-23 — commit 9a32d4b)

A `_is_market_hours()` helper checks US Eastern Time Mon–Fri 09:30–16:00 using `zoneinfo.ZoneInfo("America/New_York")` (Python stdlib, no extra deps).

**Behaviour at the top of the reconnect loop:**
- If market is **closed** → log once and sleep 60 seconds before the next check. Zero reconnect spam.
- If market is **open** → proceed to token fetch and stream connection as normal.

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

`_stats["mode"]` is set to `"market_closed"` while outside market hours — visible on `/health`.

### Reconnect Backoff — session_ticks Fix (commit 9a32d4b)

**Old behaviour:** `reconnect_attempt` was reset to `0` on every successful connection, even when the stream connected but instantly closed with no data (off-hours). This produced rapid reconnect spam.

**New behaviour:**
- `session_ticks > 0` (real data received) → reset `reconnect_attempt = 0` ✅
- `session_ticks == 0` (connected, instant close, no data) → `attempt += 1` → backoff grows up to 60s cap

### Session Token Lifecycle

Tradier requires a fresh session token for every stream connection. Tokens are obtained via:
```
POST /v1/markets/events/session
Authorization: Bearer <TRADIER_API_KEY>
Content-Length: 0   ← required (data={}, equivalent to curl -d "")
```

**Critical:** Session tokens expire when the stream connection closes. The token **must** be re-fetched on every reconnect.

### Reconnection State Machine

```
startup
  └─ while True:
      ├─ _is_market_hours()            ← if closed, sleep 60s and continue
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
      │    ├─ read lines via _guarded_lines() [30s idle watchdog]
      │    ├─ increment session_ticks per data line
      │    └─ process each trade → signal pipeline
      │
      └─ loop forever
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
`mode` values: `starting` | `live` | `demo` | `reconnecting` | `market_closed`

---

## Supabase Schema

### Live Tables (`cipher-database` — `kpajucxqlrteckfuafvq`)

| Table | Purpose | Migration |
|---|---|---|
| `options_universe_snapshots` | One row per validated ~8,000-symbol universe snapshot | `001_options_universe.sql` |
| `options_universe_symbols` | Individual symbols per snapshot (normalized, ON DELETE CASCADE) | `001_options_universe.sql` |
| `flow_episodes` | One row per qualifying repetition signal episode | manual (apply schema above) |
| `flow_events` | One row per classified options tick (batched writes) | manual (apply schema above) |

> Auth tables are managed by Supabase Auth (built-in `auth.users`).

---

## Authentication

- JWT-based, issued on login, stored client-side
- `ACCESS_TOKEN_EXPIRE_MINUTES`: 1440 (24 hours)
- Protected routes: all `/api/*` except `/api/auth/register` and `/api/auth/login`

---

## Frontend Proxy

Next.js App Router catch-all route at `app/api/[...path]/route.ts` proxies all `/api/*` calls to the Railway backend.

**Key implementation notes (updated 2026-04-23):**
- Body read as `req.text()` before forwarding — avoids `ReadableStream` / `duplex: half` issues on Vercel's Node runtime
- Next.js 15: `params` must be awaited (`Promise<{ path: string[] }>`)
- `typescript.ignoreBuildErrors: true` in `next.config.js`

---

## Deployment

### Backend (Railway)
- Nixpacks build from `backend/`
- Entry: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Auto-deploys on push to `main`

### Frontend (Vercel)
- Next.js project root: `frontend/`
- Auto-deploys on push to `main`
- Env vars: `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_WS_URL`

### CI/CD
- GitHub Actions: `.github/workflows/`
- Runs on push to `main` and PRs
