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
| **Tradier stream — market-hours guard** | Suppress reconnect spam outside US market hours | Added 2026-04-23 (commit 9a32d4b). `_is_market_hours()` checks ET Mon–Fri 09:30–16:00. Sleeps 60s when closed. Mode = `market_closed`. |
| **Tradier stream — session_ticks backoff fix** | Backoff grows properly when stream closes with no data | Added 2026-04-23 (commit 9a32d4b). `reconnect_attempt` only resets when real ticks were received. Off-hours polling degrades gracefully to ~60s. |
| **Demo mode** | Synthetic signal emission | Runs as cancellable background task when no live key |
| **Signal pipeline** | Flow parser → accumulator → composite score | Stable |
| **WebSocket broadcast** | Real-time signals to connected clients | Stable via `async_bus` |
| **Swarm simulation** | 6-agent GPT-4o-mini verdict engine | Stable |
| **CORS** | Preflight handling | Fixed 2026-04-22 |
| **Options universe persistence** | ~8,000-symbol tradeable universe stored in Supabase | Shipped 2026-04-23. DB snapshot loaded on startup in < 1s; refreshed every 24h in background. Full fallback chain: DB → Tradier → stale DB → seed. |
| **Universe snapshot store** | `services/universe_store.py` — Supabase read/write | Snapshots batched in 500s, pruned to last 7, partial unique index enforces single active row. uuid4 snapshot_id pre-generated in Python (supabase-py v2 `.select()` chain workaround). |
| **Universe symbols loader** | `services/symbols_loader.py` — Tradier fetch + validation | 20-concurrent semaphore, handles 401/network/empty/single-dict/lowercase edge cases. |
| **Universe background refresh** | 24h asyncio background task in `main.py` | Never blocks stream; keeps active snapshot current without restart. |

---

### 🟡 Partial / In Progress

| Feature | Description | Gap |
|---------|-------------|-----|
| **Supabase DB — signal storage** | Universe tables live; signal storage not yet wired | `options_universe_snapshots` + `options_universe_symbols` live. Signal storage + user prefs not yet wired. |
| **Stream health endpoint** | `/health/stream` exposing mode + reconnect count | `get_stats()` exists in `tradier_stream.py`; not yet exposed as dedicated HTTP endpoint. `mode` field now includes `market_closed`. |
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
| 2026-04-23 | Rapid reconnect spam overnight (dozens of connect/close cycles per minute) | No market-hours check; `reconnect_attempt` reset to 0 on every clean close regardless of data received | `_is_market_hours()` guard + `session_ticks`-aware backoff reset |

---

## Options Universe — Architecture Notes

### Before (hardcoded seed)
- `DEFAULT_SYMBOLS` — 16 hardcoded tickers
- Cold start: instant, but near-zero market coverage
- No persistence, no audit trail, no fallback on Tradier downtime

### After (DB-persisted universe)
- Up to ~8,000 validated optionable symbols loaded from Supabase snapshot
- Cold start: < 1 second (DB load) on subsequent deploys
- Background refresh every 24h, zero stream interruption
- Full fallback chain: fresh DB → Tradier validate → stale DB → seed
- `source` field distinguishes `tradier_validated` / `seed_fallback` / `cache`
- Last 7 snapshots retained for audit; older auto-purged via ON DELETE CASCADE

---

## Known Limitations

- Tradier stream only active during market hours (Mon–Fri 09:30–16:00 ET); off-hours returns `market_closed` mode with 60s polling
- No persistent signal storage — signals lost on container restart
- No user-specific signal filtering or watchlists
- No rate limiting on API endpoints
- Frontend auth state uses `localStorage` (no server-side session)
