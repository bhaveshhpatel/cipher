# Cipher — Decode the Market

Institutional options flow intelligence platform. Real-time whale flow detection,
AI swarm simulation, and composite signal scoring.

## Stack

| Layer      | Tech                                      |
|------------|-------------------------------------------|
| Frontend   | Next.js 14, TypeScript, Tailwind CSS      |
| Backend    | FastAPI (Python 3.11), async WebSockets   |
| Auth       | JWT (python-jose + passlib bcrypt)        |
| Streaming  | Tradier WebSocket → async event bus       |
| AI Engine  | Groq llama-3.3-70b-versatile (12-agent swarm) |
| Database   | Supabase (PostgreSQL)                     |
| Deploy BE  | Railway                                   |
| Deploy FE  | Vercel                                    |
| CI/CD      | GitHub Actions (regression-gated)         |

## Architecture

### Options Universe Persistence

The full ~8,000-symbol tradeable options universe is persisted in Supabase.
On cold start the stream loads in < 1 second from DB instead of re-validating via Tradier API.
A background task refreshes the universe every 24 hours without interrupting the stream.

```
Startup Resolution Order:
  1. Fresh DB snapshot (< 24h)    → load instantly, stream starts in < 1s
  2. Tradier fetch + validate     → save to DB, then stream
  3. Stale DB snapshot (any age)  → if Tradier is down
  4. SEED_SYMBOLS (16 tickers)    → last resort fallback
```

### Signal History (Phase 4)

Every composite signal emitted by the engine is persisted to `signal_history` via
`services/signal_store.py`. The dashboard's "🕐 Signal History" tab queries
`GET /api/signals/history` (paginated, filterable by ticker / recommendation / min_score).

### Regression Test Suite (Phase 5B)

The full codebase has an automated regression test suite covering backend and frontend.
CI enforces a hard gate — nothing merges or deploys unless the full suite passes.

| Layer | Tool | Gate |
|---|---|---|
| Backend | pytest + pytest-cov | ≥ 90% coverage (`--cov-fail-under=90`) |
| Frontend | Jest + ts-jest | ≥ 75% lines/functions globally; ≥ 90% on `useAuth.ts` |

Run locally:
```bash
# Backend
cd backend
pip install -r requirements-dev.txt
pytest

# Frontend
cd frontend
npx jest --coverage
```

See [docs/REGRESSION_TESTING.md](docs/REGRESSION_TESTING.md) for the full test inventory, config files, and CI workflow YAMLs.

## Project Structure

```
cipher/
├── backend/
│   ├── main.py                     # FastAPI app — startup loads universe from DB
│   ├── config.py                   # Settings (pydantic-settings v2)
│   ├── pytest.ini                  # ★ pytest config + coverage gate (Phase 5B)
│   ├── .coveragerc                 # ★ coverage omit rules + fail_under=90 (Phase 5B)
│   ├── requirements.txt
│   ├── requirements-dev.txt        # pytest, pytest-cov, pytest-asyncio, httpx
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
│   │   ├── swarm_engine.py         # 12-agent Groq swarm
│   │   └── ensemble_runner.py
│   ├── execution/
│   │   └── trade_executor.py       # Tradier order placement
│   ├── utils/
│   │   ├── dedup.py                # 2s TTL dedup + sweep detection
│   │   └── tradier_client.py
│   ├── services/
│   │   ├── tradier_stream.py
│   │   ├── flow_store.py
│   │   ├── signal_store.py
│   │   ├── symbols_loader.py
│   │   ├── universe_store.py
│   │   ├── symbol_registry.py
│   │   ├── stream_manager.py
│   │   └── stream_worker.py
│   ├── migrations/
│   │   └── 001–012_*.sql
│   ├── routers/
│   │   ├── auth.py
│   │   ├── flow.py
│   │   ├── simulation.py
│   │   ├── ws.py
│   │   ├── smart_signals.py
│   │   ├── history.py
│   │   ├── admin.py
│   │   └── health.py
│   └── tests/                      # ★ Full regression suite (Phase 5B)
│       ├── test_auth_router.py
│       ├── test_admin_router.py
│       ├── test_config.py
│       ├── test_demo_engine.py
│       ├── test_ingestion_config.py
│       ├── test_midcap_screener.py
│       ├── test_ensemble_runner.py
│       ├── test_dedup.py
│       ├── test_swarm_engine.py
│       ├── test_trade_executor.py
│       ├── test_simulation_router.py
│       ├── test_smart_signals_router.py
│       ├── test_main_app.py
│       └── (+ earlier suite: tradier_stream, flow_store, universe_store, etc.)
├── frontend/
│   ├── jest.config.ts              # ★ Jest config + coverageThreshold (Phase 5B)
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
│   ├── REGRESSION_TESTING.md      # ★ NEW — full test suite reference (Phase 5B)
│   ├── ARCHITECTURE.md
│   ├── BACKLOG.md
│   ├── FIXES.md
│   └── SIGNAL_ENGINE.md
└── .github/
    └── workflows/
        ├── backend.yml             # ★ lint → regression (≥90%) → Railway
        └── frontend.yml            # ★ typecheck → regression (≥75%) → build → Vercel
```

## Quick Start

### Backend
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # for running tests
uvicorn main:app --reload
```

### Run Backend Tests
```bash
cd backend
pytest                     # full suite with coverage gate
pytest -m regression       # regression tests only
pytest --no-cov            # skip coverage for speed
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
npx jest --coverage        # run with coverage + enforce thresholds
npx jest --watch           # watch mode during development
```

## CI/CD Pipeline

```
Push to main (backend/**)
  └── lint
        └── regression (--cov-fail-under=90)
              └── Railway auto-deploys via native integration

Push to main (frontend/**)
  └── typecheck + lint
        └── regression (jest --ci --coverage, thresholds enforced)
              └── build
                    └── deploy (vercel --prod)

Pull Request
  └── Same gates + orgoro/coverage posts PR coverage diff comment
```

## Supabase Tables

| Table | Purpose | Migration |
|---|---|---|
| `options_universe_snapshots` | Validated ~8,000-symbol universe snapshot | `001_options_universe.sql` |
| `options_universe_symbols` | Individual symbols per snapshot | `001_options_universe.sql` |
| `flow_episodes` | One row per qualifying repetition signal episode | `002_flow_tables.sql` |
| `flow_events` | One row per classified options tick | `002_flow_tables.sql` |
| `signal_history` | One row per composite signal emitted | `003_signal_history.sql` |
| `tier_thresholds` | Runtime tier classification thresholds | `011_*.sql` |

> ⚠️ **Service role key required for all server-side DB writes.**
> `flow_store.py` and `signal_store.py` must use `SUPABASE_SERVICE_KEY` (not the anon key).
