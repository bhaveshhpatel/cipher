# Cipher — Technical Specifications

> Last updated: 2026-04-23 (Phase 4)

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

## Signal History — Phase 4

### Overview

Every composite signal emitted by the engine is now persisted to the `signal_history` table via `services/signal_store.py`. This enables the dashboard's new "🕐 Signal History" tab to display historical signals with full scoring breakdown.

### DB Schema

```sql
-- Migration: backend/migrations/003_signal_history.sql
CREATE TABLE signal_history (
  id                    BIGSERIAL PRIMARY KEY,
  ticker                TEXT NOT NULL,
  recommendation        TEXT NOT NULL,           -- BUY / SELL / HOLD
  composite_score       NUMERIC NOT NULL,
  flow_score            NUMERIC NOT NULL,
  backtest_score        NUMERIC NOT NULL,
  volume_premium_factor NUMERIC,
  reasoning             TEXT,
  contract_type         TEXT,                    -- CALL / PUT
  alert_level           TEXT,                    -- WATCH / ALERT / STRONG_SIGNAL / CONVICTION
  total_premium         NUMERIC,
  trade_count           INT,
  signal_ts             TIMESTAMPTZ DEFAULT now(),
  created_at            TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_signal_history_ticker     ON signal_history (ticker);
CREATE INDEX idx_signal_history_signal_ts  ON signal_history (signal_ts DESC);
CREATE INDEX idx_signal_history_rec        ON signal_history (recommendation);
```

### `signal_store.py`

- Subscribes to `signal_store` channel on `AsyncEventBus`
- Writes one row per composite signal — no batching (signals are low-frequency)
- Uses `SUPABASE_SERVICE_ROLE_KEY` (same rule as `flow_store.py` — never anon key)
- Never sends `id` — Postgres generates `bigserial`

### `GET /api/signals/history`

```
GET /api/signals/history
  ?page=1
  &page_size=20
  &ticker=AAPL
  &recommendation=BUY
  &min_score=0.65
```

Response:
```json
{
  "signals": [ { ...CompositeSignal fields + signal_ts + alert_level } ],
  "page": 1,
  "page_size": 20,
  "total": 143
}
```

### Frontend Integration

| File | Role |
|---|---|
| `frontend/src/types/api.ts` | `SignalHistoryEntry` type + `SignalHistoryResponse` type |
| `frontend/src/hooks/useSignalHistory.ts` | Fetches paginated history; handles loading/error states |
| `frontend/src/components/SignalHistory.tsx` | Table with ticker, recommendation badge, scores, alert level, timestamp |
| `frontend/src/app/dashboard/page.tsx` | "🕐 Signal History" tab added alongside existing tabs |

### WebSocket Ping/Pong — TODO Resolved

`frontend/src/hooks/useSignalStream.ts` now handles the full ping/pong contract:

```typescript
// Phase 4 — ping/pong handler
if (parsed.type === 'ping') {
  ws.send(JSON.stringify({ type: 'pong' }));
  return;
}
```

This closes the TODO introduced in Phase 3 and prevents Railway from killing idle WebSocket connections.

---

## Options Universe Persistence (Added 2026-04-23)

### Overview

The full universe of tradeable options symbols (~8,000 tickers) is persisted in Supabase.

### Startup Resolution Order

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
```

### DB Schema

```sql
CREATE TABLE options_universe_snapshots (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  symbol_count  INT NOT NULL,
  source        TEXT NOT NULL
    CHECK (source IN ('tradier_validated', 'seed_fallback', 'cache')),
  is_active     BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE options_universe_symbols (
  snapshot_id  UUID NOT NULL
    REFERENCES options_universe_snapshots(id) ON DELETE CASCADE,
  symbol       TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, symbol)
);

CREATE UNIQUE INDEX idx_universe_snapshots_single_active
  ON options_universe_snapshots (is_active)
  WHERE is_active = true;
```

**Migration file:** `backend/migrations/001_options_universe.sql`

---

## Flow Store — DB Signal Persistence

### flow_episodes Schema

```sql
CREATE TABLE flow_episodes (
  id             BIGSERIAL PRIMARY KEY,
  ticker         TEXT NOT NULL,
  direction      TEXT,
  contract_type  TEXT,
  strike         NUMERIC,
  expiry         TEXT,
  total_premium  NUMERIC,
  trade_count    INT,
  alert_level    TEXT,
  is_accelerating BOOLEAN DEFAULT false,
  seed_episode   TEXT,
  signal_ts      TEXT,
  created_at     TIMESTAMPTZ DEFAULT now()
);
```

### flow_events Schema

```sql
CREATE TABLE flow_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
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

### ID Generation Contract

> Neither `flow_events`, `flow_episodes`, nor `signal_history` rows are ever sent with an `id` field.
> Postgres generates all IDs server-side. Sending a client-provided `id` causes a 400 error.

---

## Supabase Schema — All Tables

| Table | Purpose | Migration |
|---|---|---|
| `options_universe_snapshots` | One row per validated ~8,000-symbol universe snapshot | `001_options_universe.sql` |
| `options_universe_symbols` | Individual symbols per snapshot | `001_options_universe.sql` |
| `flow_episodes` | One row per qualifying repetition signal episode | `002_flow_tables.sql` |
| `flow_events` | One row per classified options tick (batched) | `002_flow_tables.sql` |
| `signal_history` | One row per composite signal emitted | `003_signal_history.sql` |

---

## Signal Pipeline

```
_resolve_startup_universe()
  └─ Tradier WebSocket
       └─ options_flow_parser.py       → OptionsFlowEvent
       └─ repetition_accumulator.py    → RepetitionEpisode
       └─ composite_signal_engine.py   → CompositeSignal
       └─ async_bus fan-out:
            ├── "signals"      → ws.py → WebSocket clients
            ├── "db_writer"    → flow_store.py → flow_episodes + flow_events
            └── "signal_store" → signal_store.py → signal_history  [Phase 4]
```

---

## Tradier Stream Architecture

### Market-Hours Guard

`_is_market_hours()` checks US Eastern Time Mon–Fri 09:30–16:00.

### Reconnect Backoff

```python
def _backoff(attempt: int) -> float:
    delay = min(60.0, 5.0 * (2 ** attempt))
    return random.uniform(0, delay)
```

| Attempt | Max delay |
|---------|-----------|
| 0 | 5s |
| 1 | 10s |
| 2 | 20s |
| 3 | 40s |
| 4+ | 60s (cap) |

### Stats

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

## Authentication

- JWT-based, issued on login, stored client-side
- `ACCESS_TOKEN_EXPIRE_MINUTES`: 1440 (24 hours)
- Protected routes: all `/api/*` except `/api/auth/register` and `/api/auth/login`

---

## Frontend Proxy

Next.js App Router catch-all route at `app/api/[...path]/route.ts` proxies all `/api/*` calls to the Railway backend.

- Body read as `req.text()` before forwarding
- Next.js 15: `params` must be awaited
- `typescript.ignoreBuildErrors: true` in `next.config.js`

---

## Deployment

### Backend (Railway)
- Entry: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Auto-deploys on push to `main`

### Frontend (Vercel)
- Next.js project root: `frontend/`
- Auto-deploys on push to `main`
- Env vars: `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_WS_URL`
