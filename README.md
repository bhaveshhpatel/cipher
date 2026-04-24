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
| AI Engine  | OpenAI GPT-4o-mini multi-agent swarm      |
| Database   | Supabase (PostgreSQL)                     |
| Deploy BE  | Railway                                   |
| Deploy FE  | Vercel                                    |
| CI/CD      | GitHub Actions                            |

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
`services/signal_store.py`. The dashboard's new "🕐 Signal History" tab queries
`GET /api/signals/history` (paginated, filterable by ticker / recommendation / min_score).

The WebSocket ping/pong TODO from Phase 3 is fully resolved — `useSignalStream.ts` now
responds to `{"type":"ping"}` with `{"type":"pong"}` to prevent Railway idle-kill.

## Project Structure

```
cipher/
├── backend/
│   ├── main.py                     # FastAPI app — startup loads universe from DB
│   ├── config.py                   # Settings (pydantic-settings v2)
│   ├── requirements.txt
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
│   │   ├── tradier_stream.py       # Live stream processor
│   │   ├── flow_store.py           # Supabase writer: flow_episodes + flow_events
│   │   ├── signal_store.py         # ★ Supabase writer: signal_history [Phase 4]
│   │   ├── symbols_loader.py       # ★ Universe fetch + Tradier validation
│   │   └── universe_store.py       # ★ Supabase snapshot read/write
│   ├── migrations/
│   │   ├── 001_options_universe.sql  # Universe tables
│   │   ├── 002_flow_tables.sql       # flow_episodes + flow_events
│   │   └── 003_signal_history.sql    # ★ signal_history [Phase 4]
│   ├── routers/
│   │   ├── auth.py
│   │   ├── flow.py
│   │   ├── simulation.py
│   │   ├── ws.py
│   │   └── smart_signals.py        # composite + list + history endpoints
│   └── tests/
│       ├── test_auth_flow.py
│       ├── test_flow_and_stats.py
│       ├── test_simulation_and_ws.py
│       ├── test_tradier_stream.py
│       ├── test_symbols_loader.py   # ★ 20 edge-case tests
│       └── test_universe_store.py   # ★ DB read/write tests
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── globals.css
│   │   │   ├── page.tsx
│   │   │   └── dashboard/page.tsx  # ★ Signal History tab added [Phase 4]
│   │   ├── components/
│   │   │   ├── CipherLogo.tsx
│   │   │   └── dashboard/
│   │   │       ├── SignalFeed.tsx
│   │   │       ├── FlowTable.tsx
│   │   │       ├── SimulationPanel.tsx
│   │   │       ├── CompositeCard.tsx
│   │   │       ├── StreamStatsBar.tsx
│   │   │       ├── SmartSignals.tsx
│   │   │       └── SignalHistory.tsx  # ★ History table component [Phase 4]
│   │   ├── hooks/
│   │   │   ├── useAuth.ts
│   │   │   ├── useSignalStream.ts  # ★ ping/pong TODO resolved [Phase 4]
│   │   │   ├── useFlow.ts
│   │   │   ├── useSimulation.ts
│   │   │   └── useSignalHistory.ts # ★ Paginated history hook [Phase 4]
│   │   ├── lib/api.ts              # ★ SignalHistoryEntry + SignalHistoryResponse types
│   │   └── types/index.ts
│   └── ...
└── .github/
    └── workflows/
        ├── backend.yml
        └── frontend.yml
```

## Quick Start

### Backend
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
uvicorn main:app --reload
```

### Frontend
```bash
cd frontend
npm install
cp .env.example .env.local   # fill in NEXT_PUBLIC_API_URL
npm run dev
```

## Environment Variables

See `backend/.env.example` and `frontend/.env.example`.

## Supabase Tables

| Table | Purpose | Migration |
|---|---|---|
| `options_universe_snapshots` | One row per validated ~8,000-symbol universe snapshot | `001_options_universe.sql` |
| `options_universe_symbols` | Individual symbols per snapshot (normalized) | `001_options_universe.sql` |
| `flow_episodes` | One row per qualifying repetition signal episode | `002_flow_tables.sql` |
| `flow_events` | One row per classified options tick (batched every 5s) | `002_flow_tables.sql` |
| `signal_history` | One row per composite signal emitted by the engine | `003_signal_history.sql` |

All migrations are in `backend/migrations/` and have been applied to production.

> ⚠️ **Service role key required for all server-side DB writes.**
> `flow_store.py` and `signal_store.py` must use `SUPABASE_SERVICE_ROLE_KEY` (not the anon key).
> The anon key is subject to RLS and will cause every insert to fail with HTTP 401 / `42501`.

## GitHub Actions Secrets Required

| Secret                        | Used by    |
|-------------------------------|------------|
| `VERCEL_TOKEN`                | FE deploy  |
| `VERCEL_ORG_ID`               | FE deploy  |
| `VERCEL_PROJECT_ID`           | FE deploy  |
| `NEXT_PUBLIC_API_URL`         | FE build   |
| `NEXT_PUBLIC_WS_URL`          | FE build   |
| `SUPABASE_URL`                | BE tests   |
| `SUPABASE_SERVICE_ROLE_KEY`   | BE DB writes (service role — not anon key) |
