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

## Project Structure

```
cipher/
├── backend/
│   ├── main.py                     # FastAPI app entry
│   ├── config.py                   # Settings (pydantic-settings)
│   ├── requirements.txt
│   ├── core/
│   │   ├── auth.py                 # JWT auth helpers
│   │   └── async_bus.py            # In-process event bus
│   ├── parsers/
│   │   ├── options_flow_parser.py  # Raw trade → OptionsFlowEvent
│   │   ├── bid_ask_classifier.py   # ABOVE_ASK / AT_ASK / etc.
│   │   └── trade_type_detector.py  # SWEEP / BLOCK / SPLIT
│   ├── signals/
│   │   ├── repetition_accumulator.py   # Rolling window repetition
│   │   ├── backtest_validator.py        # Historical win-rate score
│   │   ├── midcap_screener.py           # Mid-cap unusual activity
│   │   └── composite_signal_engine.py  # Flow + backtest composite
│   ├── simulation/
│   │   ├── swarm_engine.py         # Multi-agent LLM voting
│   │   └── ensemble_runner.py      # Aggregate verdicts
│   ├── execution/
│   │   └── trade_executor.py       # Tradier order placement
│   ├── services/
│   │   └── tradier_stream.py       # Live stream processor
│   └── routers/
│       ├── auth.py                 # POST /api/auth/token, /register
│       ├── flow.py                 # GET  /api/flow/scan
│       ├── simulation.py           # POST /api/simulation/run
│       ├── ws.py                   # WS   /ws/signals
│       └── smart_signals.py        # GET  /api/signals/composite/{ticker}
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx          # Root layout + fonts
│   │   │   ├── globals.css         # Design system CSS
│   │   │   ├── page.tsx            # Login / landing page
│   │   │   └── dashboard/
│   │   │       └── page.tsx        # Main dashboard
│   │   ├── components/
│   │   │   ├── CipherLogo.tsx      # SVG logo
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
│   │   ├── lib/
│   │   │   └── api.ts
│   │   └── types/
│   │       └── index.ts
│   ├── package.json
│   ├── next.config.ts
│   ├── tailwind.config.ts
│   ├── tsconfig.json
│   └── .env.example
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

## GitHub Actions Secrets Required

| Secret                  | Used by    |
|-------------------------|------------|
| `RAILWAY_TOKEN`         | BE deploy  |
| `VERCEL_TOKEN`          | FE deploy  |
| `VERCEL_ORG_ID`         | FE deploy  |
| `VERCEL_PROJECT_ID`     | FE deploy  |
| `NEXT_PUBLIC_API_URL`   | FE build   |
| `NEXT_PUBLIC_WS_URL`    | FE build   |
