# Cipher — Claude Context File

## Project Overview

**Cipher** is an institutional options flow intelligence platform with the tagline *"Decode the Market."* It detects real-time whale/institutional options flow, scores signals using a composite engine, and runs multi-agent AI swarm simulations to generate BUY/SELL/HOLD verdicts.

---

## Repository

- **GitHub**: `https://github.com/bhaveshhpatel/cipher`
- **Owner**: Dhruv Patel (bhaveshhpatel@yahoo.com)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Backend | FastAPI (Python 3.11), async WebSockets |
| Auth | JWT (`python-jose` + `passlib` bcrypt) |
| Streaming | Tradier WebSocket → async in-process event bus |
| AI Engine | OpenAI GPT-4o-mini (multi-agent swarm, 6 roles) |
| Database | Supabase (PostgreSQL) |
| Deploy (BE) | Railway |
| Deploy (FE) | Vercel |
| CI/CD | GitHub Actions |

---

## Repository Structure

```
cipher/
├── .github/
│   ├── workflows/
│   │   ├── backend.yml        # Railway deploy CI — runs from backend/ dir
│   │   └── frontend.yml       # Vercel deploy CI
├── backend/
│   ├── main.py                # FastAPI app entry, lifespan, CORS, router mounts
│   ├── config.py              # pydantic-settings config (env vars)
│   ├── requirements.txt
│   ├── Procfile               # Railway process definition
│   ├── railway.toml           # Backend-local Railway config (rootDirectory = "backend")
│   ├── core/
│   │   ├── auth.py            # JWT helpers, get_current_user dependency
│   │   └── async_bus.py       # In-process async pub/sub event bus
│   ├── parsers/
│   │   ├── options_flow_parser.py   # Raw Tradier trade → OptionsFlowEvent
│   │   ├── bid_ask_classifier.py    # ABOVE_ASK / AT_ASK / BELOW_BID etc.
│   │   └── trade_type_detector.py  # SWEEP / BLOCK / SPLIT / SINGLE
│   ├── signals/
│   │   ├── repetition_accumulator.py    # 30-min rolling window accumulator
│   │   ├── backtest_validator.py         # Historical win-rate scoring
│   │   ├── midcap_screener.py            # Mid-cap unusual activity detection
│   │   └── composite_signal_engine.py   # flow_score×0.6 + backtest_score×0.4
│   ├── simulation/
│   │   ├── swarm_engine.py      # 6 LLM agents with distinct trading roles
│   │   └── ensemble_runner.py   # Aggregate agent verdicts into consensus
│   ├── execution/
│   │   └── trade_executor.py    # Tradier order placement (not yet wired in)
│   ├── services/
│   │   └── tradier_stream.py    # Live Tradier WS stream processor + demo mode
│   └── routers/
│       ├── auth.py              # POST /api/auth/token, /register
│       ├── flow.py              # GET /api/flow/scan
│       ├── simulation.py        # POST /api/simulation/run
│       ├── ws.py                # WS /ws/signals
│       └── smart_signals.py     # GET /api/signals/composite/{ticker}, /stream/stats
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── globals.css
│   │   │   ├── page.tsx
│   │   │   └── dashboard/
│   │   │       └── page.tsx
│   │   │   └── api/auth/[...nextauth]/route.ts
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
│   │   ├── lib/
│   │   │   └── api.ts
│   │   └── types/
│   │       └── index.ts
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
├── railway.toml               # Root-level Railway config (rootDirectory = "backend")
└── claude.md
```

---

## Environment Variables

### Backend (`.env`)

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

### Frontend

| Variable | Where managed | Value |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | **Vercel dashboard only** | `https://cipher-production-6cd8.up.railway.app` |
| `NEXT_PUBLIC_WS_URL` | **Vercel dashboard only** | `wss://cipher-production-6cd8.up.railway.app/` |

> ⚠️ Do NOT add these to `vercel.json` or the GitHub Actions workflow.

---

## Key Business Logic

### Signal Pipeline

1. Tradier WebSocket emits raw trade ticks
2. `options_flow_parser.py` parses ticks → `OptionsFlowEvent`
3. `bid_ask_classifier.py` classifies fill aggressiveness
4. `trade_type_detector.py` classifies trade type (SWEEP / BLOCK / SPLIT / SINGLE)
5. `repetition_accumulator.py` groups trades into `RepetitionEpisode` objects (30-min window, min 3 trades, min $50K premium)
6. `composite_signal_engine.py` scores: `composite = flow_score × 0.6 + backtest_score × 0.4`
7. Signal published to `async_bus` → broadcast to all WS subscribers

### Composite Score Formula

```
flow_score    = min(1.0, (total_premium / 10M) × 0.65 + is_accelerating × 0.15 + min(trade_count/20, 0.20))
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
    → else: store hash in in-memory _users dict

Login:
  POST /api/auth/token (username=email, password)
    → if SUPABASE_URL + SUPABASE_KEY set: sign_in_with_password
        → credential error → 401 (no silent fallthrough)
        → service error → 503
    → else: verify against _users dict
    → success → JWT created with create_access_token({"sub": email})
```

> ⚠️ In-memory `_users` dict resets on every Railway deploy. Supabase must be configured for production.

### Swarm Simulation

Six LLM agents (GPT-4o-mini): Momentum Trader, Contrarian Analyst, Fundamental Analyst, Technical Analyst, Macro Strategist, Risk Manager.

### Alert Levels

| Level | Meaning |
|---|---|
| `CONVICTION` | Highest confidence signal |
| `STRONG_SIGNAL` | High confidence |
| `ALERT` | Moderate signal |
| `WATCH` | Low/monitoring signal |

---

## Current State & Known Gaps

| Area | Status |
|---|---|
| Frontend deployment | ✅ Live on Vercel |
| Backend deployment | ✅ Live on Railway |
| Auth — register + login | ✅ Fixed 2026-04-23 |
| Railway CLI build | ✅ Fixed 2026-04-23 — rootDirectory + working-directory align |
| Flow data | **Mocked** — deterministic seed by ticker hash; live Tradier wires exist but need valid API key |
| Supabase | Auth working; DB not actively queried yet |
| Redis | In `config.py` but not integrated |
| Frontend styling | Inline styles throughout dashboard; Tailwind installed but not fully used |
| Trade execution | `trade_executor.py` exists but not wired into signal flow |
| Anthropic key | In config but not used |

---

## CI/CD Notes

### Frontend (Vercel via GitHub Actions)
- Workflow: `.github/workflows/frontend.yml`
- Triggers on `push` to `main` when `frontend/**` files change
- All Vercel CLI steps run from **repo root** (not `frontend/`)
- `vercel.json` has `buildCommand`, `outputDirectory`, `framework` **only — no `env` block**
- Frontend env vars managed in **Vercel dashboard only**

### Backend (Railway via GitHub Actions)
- Workflow: `.github/workflows/backend.yml`
- Triggers on `push` to `main` when `backend/**` or `railway.toml` files change
- **`working-directory: backend`** — CLI runs from `backend/` so it picks up `backend/railway.toml`
- `backend/railway.toml` has `rootDirectory = "backend"` explicitly set
- Root `railway.toml` also has `rootDirectory = "backend"` as safety net
- ⚠️ **Disable Railway dashboard auto-deploy** to prevent double-deploys

### Railway Root Directory Fix (2026-04-23)

Previous failure: `Could not find root directory: /backend`

**Root cause:** Two conflicting `railway.toml` files. The GitHub Actions workflow ran `railway up` from the repo root, picking up the root `railway.toml` which had no `rootDirectory`. Railway/nixpacks couldn't locate `requirements.txt` at the repo root.

**Fix applied:**
1. `railway.toml` (root) — added `rootDirectory = "backend"`, removed `cd backend &&` from startCommand
2. `backend/railway.toml` — added `rootDirectory = "backend"` explicitly
3. `.github/workflows/backend.yml` — added `working-directory: backend` to the deploy step

---

## Coding Conventions

- Backend: Python 3.11, async/await throughout, pydantic models for all I/O
- Frontend: TypeScript strict mode, functional components, custom hooks pattern
- Auth guard: `Depends(get_current_user)` (BE) and `useAuth` hook redirect (FE)
- Monorepo: `backend/` and `frontend/` as siblings
- No ORM — direct Supabase REST/postgrest calls planned

---

## How to Run Locally

### Backend
```bash
cd backend
pip install -r requirements.txt
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

## Deployment

| Target | Platform | Trigger | URL |
|---|---|---|---|
| Backend | Railway | Push to `main` via `backend.yml` | `https://cipher-production-6cd8.up.railway.app` |
| Frontend | Vercel | Push to `main` via `frontend.yml` | Vercel project: `bhaveshhpatels-projects/cipher` |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-04-23 | **Fixed Railway CLI root directory error** — added `rootDirectory = "backend"` to both `railway.toml` files; added `working-directory: backend` to `backend.yml` deploy step |
| 2026-04-23 | **Fixed auth register bug** — Supabase login no longer silently falls through; credential errors return 401 |
| 2026-04-22 | Fixed frontend CI/CD: Vercel CLI double-nested path bug |
| 2026-04-22 | Removed `@cipher_api_url` / `@cipher_ws_url` secret refs from `frontend/vercel.json` |
| 2026-04-22 | Documented Railway auto-deploy issue |
| 2026-04-22 | Frontend confirmed live — login page visible |
| 2026-04-22 | Created `docs/BACKLOG.md` with B-001 through B-007 |
