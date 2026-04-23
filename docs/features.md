# Cipher — Feature Status

> Last updated: 2026-04-23

---

## Feature Map

### 🟢 Live & Stable

| Feature | Description | Notes |
|---------|-------------|-------|
| **Auth — Register** | `POST /api/auth/register` | Fixed 2026-04-23: Next.js proxy 501 bug resolved |
| **Auth — Login** | `POST /api/auth/login` → JWT | Stable |
| **Auth — /me** | `GET /api/auth/me` with JWT | Stable |
| **Frontend deployment** | Vercel CI/CD | Fixed 2026-04-22: path + vercel.json issues resolved |
| **Tradier stream — resilient** | Live options flow ingestion | Fixed 2026-04-23: 9 failure modes resolved (see specs.md) |
| **Demo mode** | Synthetic signal emission | Runs as cancellable background task when no live key |
| **Signal pipeline** | Flow parser → accumulator → composite score | Stable |
| **WebSocket broadcast** | Real-time signals to connected clients | Stable via `async_bus` |
| **Swarm simulation** | 6-agent GPT-4o-mini verdict engine | Stable |
| **CORS** | Preflight handling | Fixed 2026-04-22 |

---

### 🟡 Partial / In Progress

| Feature | Description | Gap |
|---------|-------------|-----|
| **Supabase DB** | Auth works; PostgreSQL available | Tables not yet actively queried beyond auth. Signal storage + user prefs not wired. |
| **Stream health endpoint** | `/health/stream` exposing mode + reconnect count | `get_stats()` exists in `tradier_stream.py`; not yet exposed as HTTP endpoint |
| **Frontend styling** | Tailwind CSS available | Many components use inline styles; Tailwind underused |

---

### 🔴 Not Yet Started

| Feature | Description | Backlog ID |
|---------|-------------|------------|
| **Trade executor** | `execution/trade_executor.py` exists but not wired into signal flow | B-009 |
| **Redis** | In config, not integrated | B-011 |
| **Admin page** | Role-based access for bhaveshhpatel@yahoo.com | B-001 |
| **Paid subscription model** | Tier / pricing system | B-002 |
| **Paper trading** | Signal-based simulated trade tracking | B-004 |
| **Dashboard charting** | Price + signal overlay charts | B-007 |

---

## Tradier Stream — Failure Mode History

| Date | Symptom | Root Cause | Fix |
|------|---------|------------|-----|
| 2026-04-23 | `401 — session token rejected` after 5 min, permanent demo mode | Token fetched once at startup, reused after stream drop (tokens expire on close) | Re-fetch token on every reconnect |
| 2026-04-23 | `peer closed connection without sending complete message body` | No idle watchdog; silent TCP hang went undetected | 30s `asyncio.wait_for` per line |
| 2026-04-23 | Never recovers from 401 — stays in demo mode forever | `_demo_mode()` was blocking infinite loop with `return` after 401 | Demo mode is now cancellable `asyncio.Task`; loop always retries |

---

## Known Limitations

- Tradier stream only active during market hours; off-hours returns demo signals
- No persistent signal storage — signals lost on container restart
- No user-specific signal filtering or watchlists
- No rate limiting on API endpoints
- Frontend auth state uses `localStorage` (no server-side session)
