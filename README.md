# Cipher — Decode the Market

Institutional options flow intelligence platform. Real-time whale flow detection, tier-filtered
OCC contract streaming, composite signal scoring, and AI swarm reasoning.

---

## Stack

| Layer | Tech |
|---|---|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Backend | FastAPI (Python 3.11), async WebSockets |
| Auth | JWT (python-jose + passlib bcrypt) |
| Streaming | Tradier WebSocket → STREAM-1/2/3 parallel workers → async event bus |
| AI Engine | Groq llama-3.3-70b-versatile (12-agent swarm — explicit invocation only) |
| Database | Supabase (PostgreSQL) |
| Deploy BE | Railway |
| Deploy FE | Vercel |
| CI/CD | GitHub Actions (regression-gated) |

---

## Architecture

Full architecture reference: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

### 6-Layer Pipeline (summary)

```
Layer 1 — Symbol Registry      OCC contract pre-load, O(1) lookup, tier filtering
Layer 2 — Stream Ingestion     STREAM-1/2/3 parallel workers, shared session token, ≤500 symbols/worker
Layer 3 — Trade Parsing        OCC regex, fill_price, premium, bid_ask_class, sentiment, conviction
Layer 4 — Deduplication        DedupCache — 5s TTL, sweep detection (≥3 exchanges), C-003 retroactive upgrade
Layer 5 — Repetition Accumulator + Persistence    Gate 1 ($10k OR 3 trades), Gate 2 ($50k retrigger)
Layer 6 — Signal Engine + Delivery    Composite score, async bus fan-out, WebSocket delivery, DB persistence
```

### STREAM-1/2/3 Parallel Workers

At runtime, `ceil(registry.size() / 500)` workers are spawned — typically 60–70 for a full
universe of ~31,920 OCC symbols. All workers share one Tradier session token fetched by the manager
at startup. No lock between workers — full parallel concurrency.

### Startup Resolution Order

```
1. DB snapshot < 20h old + symbol count within ±10%  → reuse snapshot_id (idempotent, fast restart)
2. Tradier chain fetch + validate                     → save to DB, then stream
3. Stale DB snapshot (any age)                        → fallback if Tradier is down
4. SEED_SYMBOLS (16 tickers)                          → last resort
```

### Signal Gating

| Gate | Condition | Effect |
|---|---|---|
| Gate 1 (persist) | `trade_count ≥ 3` OR `total_premium ≥ $10,000` | Below both = tick dropped silently |
| Gate 2 (retrigger) | `Δ total_premium ≥ $50,000` since last emit | Prevents QQQ/SPY signal spam on every tick |

### Alert Levels

| Level | Premium |
|---|---|
| `CONVICTION` | ≥ $1,000,000 |
| `STRONG_SIGNAL` | ≥ $500,000 |
| `ALERT` | ≥ $200,000 |
| `WATCH` | < $200,000 |

### Signal History

Every composite signal is persisted to `signal_history` via `services/signal_store.py`.
The dashboard queries `GET /api/signals/history` (paginated, filterable by ticker / recommendation / min_score).

### Regression Test Suite

CI enforces a hard gate — nothing merges or deploys unless the full suite passes.

| Layer | Tool | Gate |
|---|---|---|
| Backend | pytest + pytest-cov | ≥ 90% coverage (`--cov-fail-under=90`) |
| Frontend | Jest + ts-jest | ≥ 75% lines/functions globally; ≥ 90% on `useAuth.ts` |

---

## Project Structure

```
cipher/
├── backend/
│   ├── main.py                      # FastAPI app + lifespan startup sequence
│   ├── config.py                    # Settings (pydantic-settings v2)
│   ├── pytest.ini                   # pytest config + coverage gate
│   ├── .coveragerc                  # coverage omit rules + fail_under=90
│   ├── requirements.txt
│   ├── requirements-dev.txt         # pytest, pytest-cov, pytest-asyncio, httpx
│   ├── core/
│   │   ├── auth.py
│   │   └── async_bus.py             # In-memory asyncio fan-out bus
│   ├── parsers/
│   │   ├── options_flow_parser.py   # Layer 3 — OCC parse, classification
│   │   ├── bid_ask_classifier.py
│   │   └── trade_type_detector.py
│   ├── signals/
│   │   ├── repetition_accumulator.py    # Layer 5 — Gate 1 + Gate 2
│   │   ├── composite_signal_engine.py   # Layer 6 — composite score
│   │   ├── backtest_validator.py
│   │   └── midcap_screener.py
│   ├── services/
│   │   ├── tradier_stream.py        # Layer 2 entry — _process_trade pipeline
│   │   ├── stream_manager.py        # STREAM-1/2/3 worker manager
│   │   ├── stream_worker.py         # Per-worker Tradier POST + telemetry
│   │   ├── symbol_registry.py       # Layer 1 — OCC registry, tier map
│   │   ├── flow_store.py            # Layer 5/6 — flow_events + flow_episodes DB writer
│   │   ├── signal_store.py          # Layer 6 — signal_history DB writer
│   │   ├── universe_store.py        # Snapshot idempotency (U-1)
│   │   ├── chain_store.py           # OCC contract DB cache
│   │   ├── tier_engine.py           # T1/T2/T3 assignment
│   │   └── swarm_engine.py          # Groq AI swarm (explicit invocation only)
│   ├── utils/
│   │   └── dedup.py                 # Layer 4 — DedupCache, sweep detection
│   ├── routers/
│   │   ├── auth.py
│   │   ├── flow.py
│   │   ├── simulation.py
│   │   ├── ws.py                    # WebSocket — signals bus subscriber
│   │   ├── smart_signals.py
│   │   ├── history.py
│   │   ├── admin.py
│   │   └── health.py                # /health/stream — full funnel stats
│   ├── migrations/
│   │   └── 001–013_*.sql            # 013 adds UNIQUE(snapshot_id, symbol)
│   └── tests/
│       ├── test_auth_router.py
│       ├── test_admin_router.py
│       ├── test_config.py
│       ├── test_dedup.py
│       ├── test_demo_engine.py
│       ├── test_ingestion_config.py
│       ├── test_midcap_screener.py
│       ├── test_ensemble_runner.py
│       ├── test_swarm_engine.py
│       ├── test_trade_executor.py
│       ├── test_simulation_router.py
│       ├── test_smart_signals_router.py
│       ├── test_main_app.py
│       └── (+ tradier_stream, flow_store, universe_store, signal_store, repetition_accumulator...)
├── frontend/
│   ├── jest.config.ts               # Jest config + coverageThreshold
│   ├── __mocks__/
│   │   ├── styleMock.ts
│   │   └── fileMock.ts
│   └── src/
│       ├── app/
│       ├── components/
│       ├── hooks/
│       ├── lib/api.ts
│       └── types/
├── docs/
│   ├── ARCHITECTURE.md              # 6-layer architecture reference (source of truth)
│   ├── SIGNAL_ENGINE.md             # Composite score formula details
│   ├── REGRESSION_TESTING.md        # Full test inventory + CI workflow YAMLs
│   ├── BACKLOG.md
│   └── FIXES.md
└── .github/
    └── workflows/
        ├── backend.yml              # lint → regression (≥90%) → Railway
        └── frontend.yml             # typecheck → regression (≥75%) → build → Vercel
```

---

## Quick Start

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # required for tests
uvicorn main:app --reload
```

### Run Backend Tests

```bash
cd backend
pytest                      # full suite with coverage gate (≥90%)
pytest -m regression        # regression tests only
pytest --no-cov             # skip coverage for speed during development
pytest -k test_dedup        # run a single test file
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Run Frontend Tests

```bash
cd frontend
npx jest --coverage         # full suite + enforce thresholds
npx jest --watch            # watch mode during development
```

---

## Environment Variables

### Backend (Railway)

| Variable | Required | Description |
|---|---|---|
| `TRADIER_API_KEY` | Yes | Tradier brokerage API key |
| `TRADIER_BASE_URL` | Yes | `https://api.tradier.com` (production) |
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Service role key — bypasses RLS for all server-side writes |
| `SUPABASE_KEY` | No | Anon key — used for read-only public queries only |
| `GROQ_API_KEY` | No | Required only for SwarmEngine invocation |
| `JWT_SECRET` | Yes | JWT signing secret |
| `REGISTRY_REFRESH_MINS` | No | OCC registry refresh interval (default: 30) |

> ⚠️ **Never use the anon key for server-side DB writes.** `flow_store.py` and `signal_store.py`
> require `SUPABASE_SERVICE_ROLE_KEY`. The anon key respects RLS and will cause every insert to
> fail with a `42501` policy violation.

### Frontend (Vercel)

| Variable | Required | Description |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | Yes | Backend Railway URL |
| `NEXT_PUBLIC_WS_URL` | Yes | WebSocket URL (backend Railway, `wss://`) |

---

## CI/CD Pipeline

```
Push to main (backend/**)
  └── lint
        └── regression (pytest --cov-fail-under=90)
              └── Railway auto-deploy (native integration)

Push to main (frontend/**)
  └── typecheck + lint
        └── regression (jest --ci --coverage, thresholds enforced)
              └── build
                    └── deploy (vercel --prod)

Pull Request
  └── Same gates + coverage diff comment on PR
```

---

## Supabase Tables

| Table | Purpose | Migration |
|---|---|---|
| `options_universe_snapshots` | Universe snapshot metadata (idempotency by snapshot_id) | `001_options_universe.sql` |
| `options_universe_symbols` | Individual OCC symbols per snapshot — `UNIQUE(snapshot_id, symbol)` | `001_options_universe.sql` + `013` |
| `flow_events` | One row per classified options tick (batched write) | `002_flow_tables.sql` |
| `flow_episodes` | One row per Gate 2 emission (immediate write) | `002_flow_tables.sql` |
| `signal_history` | One row per composite signal emitted | `003_signal_history.sql` |
| `tier_thresholds` | Runtime T1/T2/T3 tier classification params | `011_*.sql` |
| `chain_store` | OCC contract DB cache for fast registry pre-seed | migration 013 |

> See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full schema details.
