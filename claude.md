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
| CI/CD | GitHub Actions (CI only for backend; deploy via Railway native GitHub integration) |

---

## Repository Structure

```
cipher/
├── .github/
│   └── workflows/
│       ├── backend.yml        # CI only — syntax check; NO deploy steps
│       └── frontend.yml       # Vercel deploy via CLI
├── backend/
│   ├── main.py                # FastAPI entry, lifespan, CORS, router mounts
│   ├── config.py              # pydantic-settings (env vars)
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
│   │   └── tradier_stream.py
│   └── routers/
│       ├── auth.py
│       ├── flow.py
│       ├── simulation.py
│       ├── ws.py
│       └── smart_signals.py
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
├── railway.toml               # SINGLE authoritative Railway config (root)
└── claude.md
```

---

## Environment Variables

### Backend (Railway dashboard env vars)

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

### Frontend (Vercel dashboard env vars)

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `https://cipher-production-6cd8.up.railway.app` |
| `NEXT_PUBLIC_WS_URL` | `wss://cipher-production-6cd8.up.railway.app/` |

> ⚠️ Do NOT add frontend env vars to `vercel.json` or GitHub Actions.

---

## Key Business Logic

### Signal Pipeline

1. Tradier WebSocket emits raw trade ticks
2. `options_flow_parser.py` parses ticks → `OptionsFlowEvent`
3. `bid_ask_classifier.py` classifies fill aggressiveness
4. `trade_type_detector.py` classifies trade type (SWEEP / BLOCK / SPLIT / SINGLE)
5. `repetition_accumulator.py` groups trades into `RepetitionEpisode` (30-min window, min 3 trades, min $50K premium)
6. `composite_signal_engine.py` scores: `composite = flow_score × 0.6 + backtest_score × 0.4`
7. Signal published to `async_bus` → broadcast to all WS subscribers

### Composite Score Formula

```
flow_score     = min(1.0, (total_premium / 10M) × 0.65 + is_accelerating × 0.15 + min(trade_count/20, 0.20))
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
    → else: store hash in in-memory _users dict (resets on redeploy)

Login:
  POST /api/auth/token (username=email, password)
    → if SUPABASE_URL + SUPABASE_KEY set: sign_in_with_password
        → credential error → 401
        → service error → 503
    → else: verify against _users dict
    → success → JWT {"sub": email}
```

> ⚠️ In-memory `_users` resets on every Railway deploy. Supabase must be configured for persistent auth.

### Swarm Simulation

Six GPT-4o-mini agents: Momentum Trader, Contrarian Analyst, Fundamental Analyst, Technical Analyst, Macro Strategist, Risk Manager.

### Alert Levels

| Level | Meaning |
|---|---|
| `CONVICTION` | Highest confidence |
| `STRONG_SIGNAL` | High confidence |
| `ALERT` | Moderate |
| `WATCH` | Low/monitoring |

---

## Current State & Known Gaps

| Area | Status |
|---|---|
| Frontend deployment | ✅ Live on Vercel |
| Backend deployment | ✅ Live on Railway (native GitHub integration) |
| Auth — register + login | ✅ Fixed 2026-04-23 |
| Railway deploy pipeline | ✅ Fixed 2026-04-23 — native GitHub integration, no CLI |
| Flow data | **Mocked** — deterministic seed by ticker hash; live Tradier wires exist but need valid API key |
| Supabase | Auth working; DB not actively queried yet |
| Redis | In `config.py` but not integrated |
| Frontend styling | Inline styles throughout dashboard; Tailwind installed but underused |
| Trade execution | `trade_executor.py` exists but not wired into signal flow |
| Anthropic key | In config but not used |

---

## CI/CD Architecture

### Backend — Railway Native GitHub Integration (authoritative)

Railway deploys the backend automatically when changes are pushed to `main`. No CLI, no token, no GitHub Actions deploy step.

**How it works:**
- Railway service is connected to `github.com/bhaveshhpatel/cipher`
- `railway.toml` at repo root is the single source of truth
- `rootDirectory = "backend"` → nixpacks builds from `backend/`
- `watchPatterns = ["backend/**"]` → only redeploys when backend files change
- `startCommand = "uvicorn main:app --host 0.0.0.0 --port $PORT"`
- `healthcheckPath = "/health"` → validated after every deploy

**To verify/enable in Railway dashboard:**
1. Railway dashboard → your service → Settings → Source
2. Confirm GitHub repo `bhaveshhpatel/cipher` is connected
3. Confirm branch is `main`
4. Confirm root directory is left blank (railway.toml handles it)
5. Disable any manual deploy triggers to avoid double-deploys

**Root `railway.toml` (single authoritative config):**
```toml
[build]
builder = "nixpacks"
rootDirectory = "backend"
watchPatterns = ["backend/**"]

[deploy]
startCommand = "uvicorn main:app --host 0.0.0.0 --port $PORT"
healthcheckPath = "/health"
healthcheckTimeout = 30
restartPolicyType = "on_failure"
```

**Files removed (were causing conflicts):**
- `backend/railway.toml` — deleted; caused `backend/backend` path resolution error
- `backend/Procfile` — deleted; redundant with `startCommand` in railway.toml

### Backend — GitHub Actions (`backend.yml`)

CI only — runs syntax check on all `.py` files. Does NOT deploy.
- Triggers on push/PR to `main` when `backend/**` or `railway.toml` changes
- No `RAILWAY_TOKEN` secret needed
- Railway deploy happens independently via native integration

### Frontend — Vercel via GitHub Actions (`frontend.yml`)
- Vercel CLI deploy triggered by push to `main` when `frontend/**` changes
- All env vars managed in Vercel dashboard only

---

## Coding Conventions

- Backend: Python 3.11, async/await throughout, pydantic models for all I/O
- Frontend: TypeScript strict mode, functional components, custom hooks
- Auth guard: `Depends(get_current_user)` (BE) / `useAuth` hook redirect (FE)
- Monorepo: `backend/` and `frontend/` as siblings at repo root
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

## Deployment URLs

| Target | Platform | URL |
|---|---|---|
| Backend | Railway | `https://cipher-production-6cd8.up.railway.app` |
| Frontend | Vercel | Vercel project: `bhaveshhpatels-projects/cipher` |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-04-23 | **Fixed Railway deploy pipeline** — deleted `backend/railway.toml` (caused `backend/backend` path error) and `backend/Procfile` (redundant); rewrote `backend.yml` to CI-only (syntax check, no deploy); Railway now deploys via native GitHub integration using root `railway.toml` exclusively |
| 2026-04-23 | **Fixed auth register bug** — Supabase login no longer silently falls through; credential errors return 401 |
| 2026-04-22 | Fixed frontend CI/CD: Vercel CLI double-nested path bug |
| 2026-04-22 | Removed secret refs from `frontend/vercel.json` |
| 2026-04-22 | Frontend confirmed live — login page visible |
| 2026-04-22 | Created `docs/BACKLOG.md` with B-001 through B-007 |
