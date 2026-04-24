# Cipher — Feature Status

> Last updated: 2026-04-23 (Phase 4)

---

## Feature Map

### 🟢 Live & Stable

| Feature | Description | Notes |
|---------|-------------|-------|
| **Auth — Register** | `POST /api/auth/register` | Fixed 2026-04-23: Next.js proxy 501 bug resolved |
| **Auth — Login** | `POST /api/auth/login` → JWT | Stable |
| **Auth — /me** | `GET /api/auth/me` with JWT | Stable |
| **Frontend deployment** | Vercel CI/CD | Fixed 2026-04-22: path + vercel.json issues resolved |
| **Tradier stream — resilient** | Live options flow ingestion | Fixed 2026-04-23: 9 failure modes resolved |
| **Tradier stream — market-hours guard** | Suppress reconnect spam outside US market hours | `_is_market_hours()` checks ET Mon–Fri 09:30–16:00 |
| **Tradier stream — session_ticks backoff fix** | Backoff grows properly when stream closes with no data | `reconnect_attempt` only resets when real ticks received |
| **Demo mode** | Synthetic signal emission | Runs as cancellable background task when no live key |
| **Signal pipeline** | Flow parser → accumulator → composite score | Stable |
| **WebSocket broadcast** | Real-time signals to connected clients | Stable via `async_bus` |
| **WebSocket ping/pong** | `{"type":"ping"}` → `{"type":"pong"}` in `useSignalStream.ts` | **Phase 4** — TODO fully resolved. Prevents Railway idle kills. |
| **Swarm simulation** | 6-agent GPT-4o-mini verdict engine | Stable |
| **CORS** | Preflight handling | Fixed 2026-04-22 |
| **Options universe persistence** | ~8,000-symbol universe stored in Supabase | DB snapshot loaded on startup in < 1s |
| **Universe snapshot store** | `services/universe_store.py` — Supabase read/write | Snapshots batched in 500s, pruned to last 7 |
| **Universe symbols loader** | `services/symbols_loader.py` — CBOE fetch + Tradier validation | 20-concurrent semaphore |
| **Universe background refresh** | 24h asyncio background task in `main.py` | Never blocks stream |
| **Universe screener** | `services/universe_screener.py` — stream-eligible screening | Priority pool always eligible |
| **DB signal persistence — flow_episodes** | Every repetition signal episode persisted to `flow_episodes` | No `id` field sent (Postgres generates bigserial) |
| **DB signal persistence — flow_events** | Every classified tick buffered and batch-flushed to `flow_events` every 5s | `id` field omitted; Postgres generates uuid |
| **signal_history persistence** | Every composite signal persisted to `signal_history` via `signal_store.py` | **Phase 4** — `003_signal_history.sql` migration. Immediate write per signal. |
| **GET /api/signals/history** | Paginated signal history endpoint | **Phase 4** — supports `ticker`, `recommendation`, `min_score`, `page`, `page_size` filters |
| **Signal History tab (frontend)** | "🕐 Signal History" dashboard tab | **Phase 4** — `useSignalHistory` hook + `SignalHistory` component |

---

### 🟡 Partial / In Progress

| Feature | Description | Gap |
|---------|-------------|-----|
| **Stream health endpoint** | `/health/stream` exposing mode + reconnect count | `get_stats()` exists; not yet exposed as dedicated HTTP endpoint |
| **Frontend styling** | Tailwind CSS available | Many components use inline styles; Tailwind underused |
| **Flow scan endpoint** | `GET /api/flow/scan` | Returns mock data — needs to query `flow_events` table from Supabase |
| **Signals list tier filter** | `/api/signals/list` tier param | Pass-through (mock data) — needs wiring to live accumulator query |

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
| 2026-04-23 | `401` after 5 min, permanent demo mode | Token fetched once at startup | Re-fetch token on every reconnect |
| 2026-04-23 | Silent TCP hang | No idle watchdog | 30s `asyncio.wait_for` per line |
| 2026-04-23 | Never recovers from 401 | `_demo_mode()` was blocking loop | Demo mode now cancellable `asyncio.Task` |
| 2026-04-23 | Rapid reconnect spam overnight | No market-hours check | `_is_market_hours()` guard + session_ticks-aware backoff |

---

## DB Write — Bug History

| Date | Symptom | Root Cause | Fix |
|------|---------|------------|-----|
| 2026-04-23 | 400 on every signal persist | `flow_store.py` writing to `composite_signals` (wrong table) | Changed target to `flow_episodes` |
| 2026-04-23 | 400 on `flow_events` insert | Client sending `id` field | Removed `id` from both row builders |
| 2026-04-23 | Log crash on None signal fields | `%`-style formatter on None value | Switched to f-strings throughout `flow_store.py` |

---

## Options Universe — Architecture Notes

### Before (hardcoded seed)
- `DEFAULT_SYMBOLS` — 16 hardcoded tickers
- Cold start: instant, but near-zero market coverage

### After (DB-persisted universe + screener)
- Up to ~8,000 validated optionable symbols loaded from Supabase snapshot
- Cold start: < 1 second (DB load) on subsequent deploys
- Background refresh every 24h, zero stream interruption
- Full fallback chain: fresh DB → Tradier validate → stale DB → seed

---

## Known Limitations

- Tradier stream only active during market hours (Mon–Fri 09:30–16:00 ET)
- No user-specific signal filtering or watchlists
- No rate limiting on API endpoints
- Frontend auth state uses `localStorage` (no server-side session)
- `GET /api/flow/scan` returns mock data, not live `flow_events` rows
