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
| **Tradier stream — market-hours guard** | Suppress reconnect spam outside US market hours | Added 2026-04-23. `_is_market_hours()` checks ET Mon–Fri 09:30–16:00. |
| **Tradier stream — session_ticks backoff fix** | Backoff grows properly when stream closes with no data | Added 2026-04-23. `reconnect_attempt` only resets when real ticks received. |
| **Demo mode** | Synthetic signal emission | Runs as cancellable background task when no live key |
| **Signal pipeline** | Flow parser → accumulator → composite score | Stable |
| **WebSocket broadcast** | Real-time signals to connected clients | Stable via `async_bus` |
| **Swarm simulation** | 6-agent GPT-4o-mini verdict engine | Stable |
| **CORS** | Preflight handling | Fixed 2026-04-22 |
| **Options universe persistence** | ~8,000-symbol universe stored in Supabase | Shipped 2026-04-23. DB snapshot loaded on startup in < 1s. |
| **Universe snapshot store** | `services/universe_store.py` — Supabase read/write | Snapshots batched in 500s, pruned to last 7. Includes `stream_eligible` flag per symbol. |
| **Universe symbols loader** | `services/symbols_loader.py` — CBOE fetch + Tradier validation | 20-concurrent semaphore. Returns `(symbols, source, stream_eligible_set)`. |
| **Universe background refresh** | 24h asyncio background task in `main.py` | Never blocks stream; passes `stream_eligible_set` to `save_snapshot`. |
| **Universe screener** | `services/universe_screener.py` — stream-eligible screening | Added 2026-04-23. Priority pool always eligible. Remaining symbols screened via Tradier OI check. Batch throttle via `UNIVERSE_BATCH_DELAY_MS`. Fallback to `UNIVERSE_STREAM_ELIGIBLE_DEFAULT`. |
| **DB signal persistence — flow_episodes** | Every repetition signal episode persisted to `flow_episodes` via `flow_store.py` | Fixed 2026-04-23: was incorrectly writing to `composite_signals` (wrong schema → 400). Now correctly targets `flow_episodes`. No `id` field sent (Postgres generates bigserial). |
| **DB signal persistence — flow_events** | Every classified tick buffered and batch-flushed to `flow_events` every 5s | Fixed 2026-04-23: `id` field removed from payload (Postgres generates uuid). f-string logging fixed to prevent crash on None values. |

---

### 🟡 Partial / In Progress

| Feature | Description | Gap |
|---------|-------------|-----|
| **Stream health endpoint** | `/health/stream` exposing mode + reconnect count | `get_stats()` exists; not yet exposed as dedicated HTTP endpoint. |
| **Frontend styling** | Tailwind CSS available | Many components use inline styles; Tailwind underused |
| **Flow scan endpoint** | `GET /api/flow/scan` | Returns mock data — needs to query `flow_events` table from Supabase |

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
| 2026-04-23 | 400 on every signal persist | `flow_store.py` writing to `composite_signals` (wrong table, wrong schema) | Changed target to `flow_episodes`; renamed `persist_composite_signal` → `persist_flow_episode` |
| 2026-04-23 | 400 on `flow_events` insert | Client sending `id` field; Postgres uuid column rejects client-provided value | Removed `id` from both row builders; Postgres generates all IDs |
| 2026-04-23 | Log crash on None signal fields | `log.info("... %,.0f", None)` — `%` formatter cannot format None | Switched to f-strings throughout `flow_store.py` |

---

## Options Universe — Architecture Notes

### Before (hardcoded seed)
- `DEFAULT_SYMBOLS` — 16 hardcoded tickers
- Cold start: instant, but near-zero market coverage
- No persistence, no audit trail, no fallback

### After (DB-persisted universe + screener)
- Up to ~8,000 validated optionable symbols loaded from Supabase snapshot
- Cold start: < 1 second (DB load) on subsequent deploys
- Background refresh every 24h, zero stream interruption
- Full fallback chain: fresh DB → Tradier validate → stale DB → seed
- `source` field: `tradier_validated` / `seed_fallback` / `cache`
- `stream_eligible` column per symbol — controls which subset the Tradier stream monitors
- Priority symbols (configured via `UNIVERSE_PRIORITY_SYMBOLS`) always stream
- Last 7 snapshots retained; older auto-purged via ON DELETE CASCADE

---

## Known Limitations

- Tradier stream only active during market hours (Mon–Fri 09:30–16:00 ET)
- No user-specific signal filtering or watchlists
- No rate limiting on API endpoints
- Frontend auth state uses `localStorage` (no server-side session)
- `GET /api/flow/scan` returns mock data, not live `flow_events` rows
