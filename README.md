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
│   │   ├── symbols_loader.py       # ★ Universe fetch + Tradier validation
│   │   └── universe_store.py       # ★ Supabase snapshot read/write
│   ├── migrations/
│   │   └── 001_options_universe.sql  # ★ DB schema (applied to Supabase)
│   ├── routers/
│   │   ├── auth.py
│   │   ├── flow.py
│   │   ├── simulation.py
│   │   ├── ws.py
│   │   └── smart_signals.py
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

| Table | Purpose |
|---|---|
| `options_universe_snapshots` | One row per validated ~8,000-symbol universe snapshot |
| `options_universe_symbols` | Individual symbols per snapshot (normalized) |

Migration file: `backend/migrations/001_options_universe.sql` — already applied to production.

## GitHub Actions Secrets Required

| Secret                  | Used by    |
|-------------------------|------------|
| `VERCEL_TOKEN`          | FE deploy  |
| `VERCEL_ORG_ID`         | FE deploy  |
| `VERCEL_PROJECT_ID`     | FE deploy  |
| `NEXT_PUBLIC_API_URL`   | FE build   |
| `NEXT_PUBLIC_WS_URL`    | FE build   |
