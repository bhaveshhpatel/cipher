# Cipher — Technical Specifications

> Last updated: 2026-04-23

---

## System Overview

Cipher is an institutional options flow intelligence platform. It ingests real-time options trade data from Tradier's streaming API, scores signals through a composite engine, and runs a multi-agent AI swarm simulation to generate BUY/SELL/HOLD verdicts.

**Live URLs**
- Frontend: Vercel (bhaveshhpatels-projects/cipher)
- Backend: Railway (`cipher-production-6cd8.up.railway.app`)

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Backend | FastAPI (Python 3.11), async WebSockets |
| Auth | JWT (`python-jose` + `passlib` bcrypt) |
| Streaming | Tradier WebSocket → async in-process event bus |
| AI Engine | OpenAI GPT-4o-mini (6-agent swarm) |
| Database | Supabase (PostgreSQL) |
| Deploy | Railway (BE) + Vercel (FE) |
| CI/CD | GitHub Actions |

---

## Signal Pipeline

```
Tradier WebSocket
  └─ options_flow_parser.py       → OptionsFlowEvent
       └─ bid_ask_classifier.py   → fill aggressiveness
       └─ trade_type_detector.py  → SWEEP / BLOCK / SPLIT / SINGLE
  └─ repetition_accumulator.py    → RepetitionEpisode
       (30-min window, min 3 trades, min $50K premium)
  └─ composite_signal_engine.py   → composite score
       (flow_score × 0.6 + backtest_score × 0.4)
  └─ async_bus                    → broadcast to WebSocket subscribers
```

---

## Tradier Stream Architecture (Updated 2026-04-23)

### Overview

The Tradier stream connection is managed by `backend/services/tradier_stream.py`. The module is designed for production resilience — it never exits permanently and always attempts to recover a live connection.

### Session Token Lifecycle

Tradier requires a fresh session token for every stream connection. Tokens are obtained via:
```
POST /v1/markets/events/session
Authorization: Bearer <TRADIER_API_KEY>
Content-Length: 0   ← required (data={}, equivalent to curl -d "")
```

**Critical:** Session tokens expire when the stream connection closes. The token **must** be re-fetched on every reconnect — reusing a token after any disconnect will produce a 401.

### Reconnection State Machine

```
startup
  └─ while True:
      ├─ _get_session_token()          ← fresh token every iteration
      │    ├─ retry up to 3x on transient network error (2s gap)
      │    └─ return None on 401 (bad key) or exhausted retries
      │
      ├─ if no token:
      │    ├─ start _demo_mode_once() as background asyncio.Task
      │    ├─ exponential backoff (5s base, 60s cap, jitter)
      │    └─ continue → retry token fetch
      │
      ├─ if token:
      │    ├─ cancel demo task (if running)
      │    ├─ open httpx streaming POST to Tradier
      │    │
      │    ├─ if stream 401 (expired token race):
      │    │    ├─ fast retry (1s) for first 4 consecutive
      │    │    └─ slow backoff after 5 consecutive (likely bad key)
      │    │
      │    ├─ if connected:
      │    │    ├─ set mode = "live"
      │    │    ├─ read lines via _guarded_lines() [30s idle watchdog]
      │    │    └─ process each trade → signal pipeline
      │    │
      │    └─ on any error (network, timeout, idle):
      │         ├─ increment reconnect counter
      │         ├─ set mode = "reconnecting"
      │         └─ exponential backoff → continue
      │
      └─ loop forever
```

### Idle Watchdog

Tradier sends bare `\n` keepalives. If no line (including keepalives) is received within **30 seconds**, `_guarded_lines()` raises `asyncio.TimeoutError`, which triggers an immediate reconnect. This prevents silent TCP hangs from causing indefinite data gaps.

```python
async def _guarded_lines(resp):
    aiter = resp.aiter_lines().__aiter__()
    while True:
        line = await asyncio.wait_for(aiter.__anext__(), timeout=30.0)
        yield line
```

### Backoff Formula

```python
def _backoff(attempt: int) -> float:
    delay = min(60.0, 5.0 * (2 ** attempt))
    return random.uniform(0, delay)  # full jitter
```

| Attempt | Max delay |
|---------|-----------|
| 0 | 5s |
| 1 | 10s |
| 2 | 20s |
| 3 | 40s |
| 4+ | 60s (cap) |

### Demo Mode

Demo mode runs as a cancellable `asyncio.Task` (`_demo_mode_once()`). It emits synthetic signals at random intervals and is immediately cancelled when a live Tradier connection is established. It is **not** a blocking infinite loop — cancellation is clean via `asyncio.CancelledError`.

Demo mode is only entered when:
1. `TRADIER_API_KEY` is not set (permanent demo until restart)
2. Session token cannot be obtained after retries (temporary demo until token recovers)

### Stats

The module exposes `get_stats()` returning:
```json
{
  "active_symbols": 8,
  "ticks": 1420,
  "classified": 893,
  "signals": 47,
  "errors": 2,
  "reconnects": 1,
  "mode": "live"
}
```
`mode` values: `starting` | `live` | `demo` | `reconnecting`

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TRADIER_API_KEY` | Yes (for live) | Bearer token for Tradier API |
| `TRADIER_ACCOUNT_ID` | Yes (for trading) | Tradier brokerage account ID |
| `TRADIER_BASE_URL` | No | Default: `https://api.tradier.com` |
| `TRADIER_STREAM_URL` | No | Default: `https://stream.tradier.com` |

---

## Authentication

- JWT-based, issued on login, stored client-side
- `ACCESS_TOKEN_EXPIRE_MINUTES`: 1440 (24 hours)
- Protected routes: all `/api/*` except `/api/auth/register` and `/api/auth/login`
- Supabase used for user persistence; DB tables not yet actively queried beyond auth

---

## Frontend Proxy

Next.js App Router catch-all route at `app/api/[...path]/route.ts` proxies all `/api/*` calls to the Railway backend.

**Key implementation notes (updated 2026-04-23):**
- Body read as `req.text()` before forwarding — avoids `ReadableStream` / `duplex: half` issues on Vercel's Node runtime
- Next.js 15: `params` must be awaited (`Promise<{ path: string[] }>`)
- `typescript.ignoreBuildErrors: true` in `next.config.js` — proxy uses intentional casts that TS flags but are runtime-correct

---

## Deployment

### Backend (Railway)
- Nixpacks build from `backend/`
- Entry: `uvicorn main:app --host 0.0.0.0 --port 8080`
- Auto-deploys on push to `main`
- Env vars set in Railway dashboard

### Frontend (Vercel)
- Next.js project root: `frontend/`
- Auto-deploys on push to `main`
- Env vars: `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_WS_URL`

### CI/CD
- GitHub Actions: `.github/workflows/`
- Runs on push to `main` and PRs
