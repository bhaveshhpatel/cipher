# Cipher — Deployment Guide

> Last updated: 2026-04-28 (branch: stable/ingestion-flow-2026-04-28)
> Stack: Railway (backend) · Vercel (frontend) · Supabase (Postgres + Auth)

---

## Architecture Overview

```
Vercel (Next.js frontend)
    │  HTTPS + WSS
    ▼
Railway (FastAPI backend)
    │  service role key
    ▼
Supabase (Postgres)
    │
    ├── options_universe_symbols
    ├── flow_events
    ├── flow_episodes
    ├── signal_history
    └── tier_thresholds

Railway (FastAPI backend)
    │  TRADIER_API_KEY
    ├── api.tradier.com   (REST: quotes, chains, session token)
    └── stream.tradier.com (SSE stream: timesale events)
         └── 64 workers × 500 symbols = 31,920 OCC symbols
```

---

## Required Environment Variables

### Railway (Backend)

Set all of these in the Railway service → Variables panel.

#### Auth
| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_KEY` | ✅ | JWT signing secret. Use a long random string. |

#### Supabase
| Variable | Required | Description |
|----------|----------|-------------|
| `SUPABASE_URL` | ✅ | Project URL (e.g. `https://xyz.supabase.co`) |
| `SUPABASE_SERVICE_ROLE_KEY` | ✅ | Service role key. Required for all DB writes (flow_episodes, signal_history). No anon fallback — missing this causes 42501 RLS errors on every insert. |
| `SUPABASE_ANON_KEY` | ⚠️ optional | Used only for public-read operations. Prefer service role key for all backend use. |

#### Tradier
| Variable | Required | Description |
|----------|----------|-------------|
| `TRADIER_API_KEY` | ✅ | API key from developer.tradier.com. Used for REST: quotes, chains, expirations. |
| `TRADIER_STREAM_TOKEN` | ✅ | Stream access token (same account). Passed to `get_session_token()`. |
| `TRADIER_ACCOUNT_ID` | ✅ | Account ID for order endpoints (`trade_executor.py`). |
| `TRADIER_BASE_URL` | optional | Default: `https://api.tradier.com`. Override for sandbox. |
| `TRADIER_STREAM_URL` | optional | Default: `https://stream.tradier.com`. **Different host from REST.** Missing this caused zero ticks on every deploy before CONFIG-STREAM-URL fix (2026-04-28). |

> **Tradier session limit:** Individual/Developer accounts allow exactly **1 concurrent stream session**. Cipher manages this with a single shared session token distributed to all 64 workers. Do NOT run multiple backend instances simultaneously against the same Tradier account — each instance will fight for the 1 session slot.

#### AI / LLM
| Variable | Required | Description |
|----------|----------|-------------|
| `GROQ_API_KEY` | ⚠️ optional | Enables AI swarm (12 Groq agents). Without it, `/api/simulate` returns `HOLD` fallback. |
| `SWARM_N_AGENTS` | optional | Default: `6`. Snapped to nearest: 3, 6, 9, 12. |

#### Universe / Stream Eligibility
| Variable | Required | Description |
|----------|----------|-------------|
| `UNIVERSE_MIN_PRICE` | optional | Default: `1.0`. Min last price for stream eligibility. |
| `UNIVERSE_MIN_VOLUME` | optional | Default: `100000`. Min daily volume for stream eligibility. |
| `UNIVERSE_QUOTES_BATCH_SIZE` | optional | Default: `200`. Symbols per Tradier quotes batch call. |
| `UNIVERSE_QUOTES_CONCURRENCY` | optional | Default: `28`. Concurrent batch workers during universe build. |

#### App
| Variable | Required | Description |
|----------|----------|-------------|
| `APP_ENV` | optional | Default: `production`. |
| `LOG_LEVEL` | optional | Default: `INFO`. Set `DEBUG` only locally — Railway log volume is high at DEBUG. |
| `CORS_ALLOWED_ORIGINS` | optional | Comma-separated origins. Default includes `https://cipher.vercel.app`, `https://cipher-git-main.vercel.app`, `http://localhost:3000`. Add Vercel preview URLs if needed (or rely on `allow_origin_regex` which covers `*.vercel.app` automatically). |

---

### Vercel (Frontend)

Set in Vercel → Project Settings → Environment Variables.

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXT_PUBLIC_API_URL` | ✅ | Backend URL, e.g. `https://cipher-backend.up.railway.app` |
| `NEXT_PUBLIC_WS_URL` | ✅ | WebSocket URL, e.g. `wss://cipher-backend.up.railway.app/api/ws` |
| `NEXT_PUBLIC_SUPABASE_URL` | ✅ | Supabase project URL (same as backend) |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | ✅ | Supabase anon key (public, safe to expose) |

---

## CI/CD Pipeline

### Backend (GitHub Actions → Railway)

Trigger: push to `main` with changes in `backend/**`

```
lint (ruff)
  └── regression (pytest --cov-fail-under=90)
        └── Railway auto-deploy (on push to main)
```

- Coverage gate: **≥ 90%** hard fail.
- PR coverage comment posted automatically via `orgoro/coverage@v3.2`.
- Railway deploys only after `regression` job passes.

### Frontend (GitHub Actions → Vercel)

Trigger: push to `main` with changes in `frontend/**`

```
typecheck (tsc --noEmit)
  └── regression (jest --ci --coverage, thresholds in jest.config.ts)
        └── build (next build)
              └── deploy (vercel --prod)
```

- Coverage gate: **≥ 75%** lines/functions globally.
- Deploy blocked until all three prior jobs pass.

---

## Database Migrations

Migrations live in `backend/migrations/`. Apply in order using the Supabase SQL editor or `supabase db push`.

| Migration | Description |
|-----------|-------------|
| 001 | Initial schema: `users`, `flow_events` |
| 002 | Auth tables, JWT setup |
| 003 | `signal_history` table |
| 004 | Swarm fields on `signal_history` (direction, confidence, agents JSONB, votes) |
| 005 | Schema repair for signal_history |
| 006 | `flow_episodes` table |
| 007 | `alert_level` column on `flow_episodes` |
| 008 | Flow episode indexes |
| 009 | `is_synthetic_quote` boolean on `flow_events` |
| 010 | `tier`, `open_interest`, `average_volume` on `options_universe_symbols` |
| 011 | `tier_thresholds` table |
| 012 | `updated_at` auto-trigger on `tier_thresholds`; RLS SELECT policy for `authenticated` |
| 013 | `UNIQUE(snapshot_id, symbol)` on `options_universe_symbols`. Deduplicates existing rows. Removes orphaned `options_chain_cache` rows. **Required for snapshot idempotency on restart (U-1 fix, 2026-04-28).** |

> Migration 013 uses bare `ADD CONSTRAINT` without `IF NOT EXISTS` (not supported in PostgreSQL). It is safe to re-run only if the constraint does not yet exist. Check first: `SELECT conname FROM pg_constraint WHERE conname = 'uq_universe_snapshot_symbol';`

---

## Startup Sequence

On Railway deploy, `lifespan()` in `main.py` runs:

1. `registry.build()` — fetches full OCC symbol universe from Tradier chains (~31,920 symbols, ~2–3 min cold start)
2. `_stamp_oi(quotes, oi_map)` — stamps average chain OI onto all `SymbolQuote` objects
3. `assign_tiers(quotes)` — T1/T2/T3 classification using DB thresholds + OI gate
4. `upsert_symbol_quotes(tier_map=...)` — writes/updates universe snapshot in Supabase (idempotent via migration 013 UNIQUE constraint)
5. `stream_manager.start(registry=registry)` — spawns 64 workers (ceil(31920/500)), shared session token, 50ms stagger
6. `prewarm_task = asyncio.create_task(_registry_prewarm_loop())` — schedules next 09:15 ET registry rebuild
7. All 8 routers mounted and serving

Cold start time: **~3–4 minutes** (dominated by registry build + Tradier chain API calls).

---

## Local Development

```bash
# Backend
cd backend
cp .env.example .env          # fill in real values
pip install -r requirements.txt
pip install -r requirements-dev.txt
uvicorn main:app --reload --port 8000

# Run tests
pytest
pytest --no-cov              # faster, no coverage

# Frontend
cd frontend
cp .env.local.example .env.local   # fill NEXT_PUBLIC_* vars
npm install
npm run dev
```

---

## Common Issues

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Zero ticks in Railway logs | `TRADIER_STREAM_URL` missing or pointing to wrong host | Ensure `TRADIER_STREAM_URL=https://stream.tradier.com` in Railway vars |
| `42501` RLS error on flow_episodes insert | `SUPABASE_SERVICE_ROLE_KEY` missing or empty | Set the service role key in Railway — no anon fallback |
| All `alert_level` rows are `WATCH` | Old code before ALERT-LEVEL fix | Deploy from `stable/ingestion-flow-2026-04-28` or later |
| Duplicate universe rows on restart | Missing migration 013 UNIQUE constraint | Run migration 013 in Supabase SQL editor |
| Swarm returns `HOLD` on all simulations | `GROQ_API_KEY` not set | Add Groq API key to Railway vars |
| Frontend WS drops after ~10s | Pong not sent by frontend (B-026 open) | Temporary: disable 1001 close in `ws.py`; proper fix is B-026 |
| Coverage gate fails in CI | New code path not covered | Run `pytest --cov --cov-report=html` locally to identify gaps |
