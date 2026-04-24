# Cipher — Layer-by-Layer Architecture Trace

> **Repository:** [bhaveshhpatel/cipher](https://github.com/bhaveshhpatel/cipher)  
> **Last Updated:** April 24, 2026  
> **Purpose:** Full codebase comprehension reference — data flow, component roles, database schema, CI/CD, and known open items.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Architecture Stack](#architecture-stack)
3. [End-to-End Data Flow](#end-to-end-data-flow)
4. [Backend — Key Files & Roles](#backend--key-files--roles)
5. [Frontend Structure](#frontend-structure)
6. [Database (Supabase)](#database-supabase)
7. [CI/CD Pipelines](#cicd-pipelines)
8. [Critical Runtime Rules](#critical-runtime-rules)
9. [Phase 4 Open Items (TODOs)](#phase-4-open-items-todos)

---

## System Overview

**Cipher** is an institutional options flow intelligence platform ("Decode the Market"). It ingests real-time options trade data from Tradier's SSE stream, scores it through a composite AI signal engine, and fans out live BUY/SELL/HOLD signals to authenticated WebSocket clients. Signals are simultaneously persisted to a Supabase PostgreSQL database.

---

## Architecture Stack

| Layer | Technology | Deployed On |
|---|---|---|
| **Frontend** | Next.js 14, TypeScript, Tailwind CSS | Vercel |
| **Backend** | FastAPI (Python 3.11), async | Railway |
| **Database** | Supabase (PostgreSQL) | Supabase |
| **CI/CD** | GitHub Actions | GitHub → Railway / Vercel |

---

## End-to-End Data Flow

The full pipeline runs through **5 sequential stages** from symbol universe bootstrap to real-time client fan-out.

### Stage 1 — Universe Bootstrap

**File:** `services/symbols_loader.py`

- On startup, pulls ~5,500 symbols from the CBOE symbol list.
- Validates each symbol against Tradier's `/expirations` endpoint.
- Batch-fetches quotes in groups of **200 symbols per batch**, **28 concurrent batches**.
- Filters down to ~1,000–2,000 `stream_eligible` symbols using the criteria:
  - `last_price ≥ 1.0`
  - `volume ≥ 100,000`
- **Priority symbols** (SPY, QQQ, AAPL, etc.) are always included regardless of filters.
- Eligible symbols are persisted to Supabase (`options_universe_symbols`) with `stream_eligible = TRUE`.

### Stage 2 — Tradier SSE Stream

**File:** `services/tradier_stream.py`

- Opens a persistent WebSocket connection to `stream.tradier.com` for the eligible symbol set.
- Implements a **resilient reconnect loop** with watchdog timer, exponential backoff, and jitter to handle dropped connections.
- Raw options tick data is passed downstream to the parser.

### Stage 3 — Parse → Accumulate

**Files:** `services/options_flow_parser.py`, `services/repetition_accumulator.py`

- Each tick passes through `options_flow_parser.py`:
  - **Zero-size guard** filters out noise ticks with no volume.
- Parsed ticks enter the `RepetitionAccumulator`:
  - Groups trades by `(ticker, strike, expiry, type)` within a **30-minute rolling window**.
  - Emits a `RepetitionEpisode` when a group reaches **≥ 3 trades AND ≥ $50K premium**.

### Stage 4 — Signal Scoring

**File:** `services/composite_signal_engine.py`

Applies a **3-component weighted formula**:

```
composite_score = (flow_score × 0.55) + (backtest_score × 0.35) + (volume_premium_score × 0.10)
```

**Verdict logic:**

| Condition | Verdict |
|---|---|
| `composite ≥ 0.65` AND bullish direction | **BUY** |
| `composite ≥ 0.65` AND bearish direction | **SELL** |
| `composite < 0.65` | **HOLD** |

> **Note:** OI (Open Interest) score currently falls back to `0.5` neutral when OI data is unavailable for a symbol.

### Stage 5 — Fan-Out

**File:** `core/async_bus.py` (`AsyncEventBus`)

Publishes each scored signal simultaneously to two consumers:

1. **`routers/ws.py`** → All connected, authenticated WebSocket clients (real-time push).
2. **`services/flow_store.py`** → Supabase tables `flow_episodes` and `flow_events` (persistence).

---

## Backend — Key Files & Roles

### `main.py`

FastAPI application entry point. Uses a **lifespan context manager** for startup sequencing:

1. Load symbol universe from DB (or bootstrap from CBOE if empty).
2. Start the Tradier SSE stream with the eligible symbol set.
3. Register all API routers.

### `config.py`

Pydantic `Settings` v2. Parses all Railway environment variables. Includes a `@property` for `priority_symbols` that splits a comma-delimited env string into `list[str]`.

### `routers/ws.py`

WebSocket endpoint: `GET /ws/signals?token=<jwt>`

- **Auth:** Validates JWT token on connection upgrade. Rejects unauthenticated connections immediately.
- **Heartbeat:** Server pings every **25 seconds**; expects a `{"type":"pong"}` response within **10 seconds**.
  - On timeout, closes connection with code `1001` (prevents Railway idle TCP drops).
- Subscribes the connection to the `AsyncEventBus` for real-time signal delivery.

### `routers/smart_signals.py`

REST endpoints for signal data:

- `GET /api/signals/composite/{ticker}` — Latest composite score for a single ticker.
- `GET /api/signals/list` — Paginated signal list with filters: `direction`, `tier`, `min_conviction`.

> **Status:** Tier filter is pass-through in mock mode — live accumulator wiring is a Phase 4 TODO.

### `routers/flow.py`

- `GET /api/flow/scan` — **Currently mocked.** Returns static placeholder data.
- **Phase 4 TODO:** Wire to live `flow_events` Supabase query.

### `routers/simulation.py`

- `POST /api/simulate` — Paper trading simulation endpoint. Accepts a signal and simulates a trade outcome.

### `services/flow_store.py`

The **sole database writer** for options flow data. Critical constraints:

- **MUST** use `SUPABASE_SERVICE_ROLE_KEY` — the anon key fails with Postgres error `42501` (RLS policy violation).
- **Never** chains `.select()` after `.insert()` — unsupported in `supabase-py` v2.
- **Never** sends `id` fields in insert payloads — the DB auto-generates them via `DEFAULT gen_random_uuid()`.

### `services/universe_store.py`

Read-only DB access for symbol universe data. Uses `SUPABASE_KEY` (anon key, RLS enforced).

---

## Frontend Structure

```
frontend/
├── .github/workflows/frontend.yml   # Vercel deploy via GitHub Actions
├── vercel.json                       # Vercel project config
├── .deploy-trigger                   # Force-redeployment sentinel file
└── src/
    └── app/                          # Next.js 14 App Router
```

- Deployed to **Vercel** via `frontend/.github/workflows/frontend.yml` on every push to `main`.
- `.deploy-trigger` is a dummy file bumped to force Vercel cache invalidation when needed.
- Frontend WebSocket clients **must** respond to server pings with `{"type":"pong"}` to survive the Phase 3 heartbeat (currently a TODO).

---

## Database (Supabase)

### Applied Migrations

| Migration | File | What It Created |
|---|---|---|
| `001` | `001_options_universe.sql` | `options_universe_snapshots`, `options_universe_symbols`, `flow_episodes`, `flow_events` |
| `002` | `002_universe_symbols_quotes.sql` | Added `stream_eligible BOOLEAN`, `last_price NUMERIC(12,4)`, `volume BIGINT` + partial index on eligible symbols |

### Key Table Roles

| Table | Writer | Reader | Purpose |
|---|---|---|---|
| `options_universe_symbols` | `symbols_loader.py` (service key) | `universe_store.py` (anon key) | Stream-eligible symbol list with quote data |
| `options_universe_snapshots` | `symbols_loader.py` (service key) | — | Bootstrap run metadata |
| `flow_episodes` | `flow_store.py` (service key) | REST endpoints | Aggregated repetition episodes |
| `flow_events` | `flow_store.py` (service key) | `flow.py` router (TODO) | Individual trade events within an episode |

### RLS Key Rule

```
flow_store.py  →  SUPABASE_SERVICE_ROLE_KEY  (bypasses RLS — write access)
universe_store.py  →  SUPABASE_KEY (anon)    (RLS enforced — read-only)
```

---

## CI/CD Pipelines

### Backend (`backend/.github/workflows/backend.yml`)

- **Trigger:** Push to `main`.
- **Action:** Syntax check + lint (Python) only. **No deploy step.**
- **Deploy:** Handled by **Railway's native GitHub integration** — Railway auto-deploys from `main` directly.

### Frontend (`frontend/.github/workflows/frontend.yml`)

- **Trigger:** Push to `main`.
- **Action:** Installs dependencies → builds Next.js app → deploys to Vercel via Vercel CLI.
- **Secrets required:** `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`.

---

## Critical Runtime Rules

| Rule | Detail |
|---|---|
| **Service key for writes** | `flow_store.py` must use `SUPABASE_SERVICE_ROLE_KEY` or all inserts fail with `42501` |
| **No `.select()` after `.insert()`** | `supabase-py` v2 limitation — chain will raise an exception |
| **No `id` in insert payloads** | All PK columns are `DEFAULT gen_random_uuid()` — sending an `id` will conflict |
| **WebSocket pong** | Frontend must respond `{"type":"pong"}` within 10s of each server ping or connection closes with code `1001` |
| **Railway idle TCP** | The 25s ping / 10s pong timeout is specifically designed to prevent Railway from dropping idle WebSocket connections |
| **Priority symbols always streamed** | SPY, QQQ, AAPL, etc. bypass the `last_price`/`volume` filter in `symbols_loader.py` |
| **OI fallback** | If Open Interest data is unavailable, `composite_signal_engine.py` uses `0.5` as a neutral OI score |

---

## Phase 4 Open Items (TODOs)

| # | Component | Description | Priority |
|---|---|---|---|
| 1 | `routers/flow.py` | Wire `GET /api/flow/scan` to live `flow_events` Supabase query (currently returns mock data) | High |
| 2 | Frontend `ws.py` client | Implement WebSocket pong response (`{"type":"pong"}`) to survive Phase 3 heartbeat | High |
| 3 | `routers/smart_signals.py` | Connect `/api/signals/list` tier filter to live accumulator — currently pass-through in mock mode | Medium |
| 4 | `composite_signal_engine.py` | Investigate per-symbol OI field availability; replace `0.5` neutral fallback with real data | Medium |
| 5 | Load testing | Validate system stability under 50 concurrent authenticated WebSocket users | Medium |

---

*Document auto-generated from full codebase read of `bhaveshhpatel/cipher` on April 24, 2026.*
