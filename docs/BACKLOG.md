# Cipher — Product Backlog

> Maintained by: Dhruv Patel (bhaveshhpatel@yahoo.com)  
> Last updated: 2026-04-23 (Phase 4)  
> **Status legend:** `🔲 Todo` · `🔄 In Progress` · `✅ Done` · `🚫 Dropped`

---

## Active Backlog

| # | Item | Status | Notes |
|---|------|--------|-------|
| B-001 | Admin page for business owner/founder | 🔲 Todo | Scoped to bhaveshhpatel@yahoo.com. Role-based access, protected route. |
| B-002 | Paid subscription model — brainstorm | 🔲 Todo | Explore tiers, pricing, freemium vs. gated flow. |
| B-003 | Configurable business features & architecture | 🔲 Todo | Feature flags / config system to toggle which components are deployed and active. |
| B-004 | Paper trading for backtesting / performance tracking | 🔲 Todo | Simulate trades based on signals; track win/loss over time without real capital. |
| B-005 | Trading options for customers (real or paper) | 🔲 Todo | Customer-facing paper or live trading — requires brokerage integration + risk disclaimers. |
| B-006 | Product ideation — PM mode | 🔲 Todo | Explore adjacent product ideas; act as PM to define features, user personas, market fit. |
| B-007 | Charting on dashboard | 🔲 Todo | Add price/signal charts to the main dashboard (e.g. options flow overlaid on price chart). |
| B-008 | Stream health endpoint | 🔲 Todo | Expose `/health/stream` returning mode (live/demo/reconnecting/market_closed), reconnect count, last tick time. |
| B-009 | Wire `trade_executor.py` into signal flow | 🔲 Todo | `execution/trade_executor.py` exists but is not connected to the composite signal engine output. |
| B-011 | Redis integration | 🔲 Todo | Redis is in config but not used. Candidate for signal caching + WebSocket pub/sub at scale. |
| B-012 | Wire `GET /api/flow/scan` to `flow_events` table | 🔲 Todo | `routers/flow.py` currently returns mock data. Needs Supabase query against live `flow_events` rows. |
| B-013 | Wire `/api/signals/list` tier filter to live accumulator | 🔲 Todo | Tier filter currently pass-through (mock). Needs real query against live signal data. |

---

## Completed

| # | Item | Completed | Notes |
|---|------|-----------|-------|
| C-001 | Frontend deployment CI/CD fixed | 2026-04-22 | Fixed double-nested path bug, removed broken @secret refs from vercel.json. |
| C-002 | Auth register/login 501 error fixed | 2026-04-23 | Next.js proxy: ReadableStream body bug, Next.js 15 async params, TS strict mode. |
| C-003 | Tradier stream — 9 failure modes fixed | 2026-04-23 | Full production-grade resilience rewrite. |
| C-004 | Options universe persistence | 2026-04-23 | ~8,000-symbol universe in Supabase. DB-first startup, 24h refresh, full fallback chain. 30 tests. |
| C-005 | Tradier stream — market-hours guard + backoff fix | 2026-04-23 | `_is_market_hours()`, session_ticks-aware backoff, `market_closed` mode. |
| C-006 | flow_store.py — fix wrong table + id field + log crash | 2026-04-23 | `persist_flow_episode()` targets `flow_episodes`. No `id` in row builders. f-string logs. 8 tests. |
| C-007 | Phase 3 — composite score v2 + signals list | 2026-04-23 | `volume_premium_factor` (×0.10), weights 0.55/0.35/0.10, `/api/signals/list` endpoint, size==0 guard. |
| C-008 | Phase 4 — signal history | 2026-04-23 | `signal_history` table (`003_signal_history.sql`), `signal_store.py`, `GET /api/signals/history`, `useSignalHistory` hook, `SignalHistory` component, dashboard History tab. WS ping/pong TODO resolved in `useSignalStream.ts`. |

---

## Dropped

| # | Item | Dropped | Reason |
|---|------|---------|--------|
| B-010 | Supabase DB — signal storage | 2026-04-23 | Completed as C-006 + C-008. All three signal tables live. |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-04-23 | Added C-008 — Phase 4 signal history (signal_store, history endpoint, frontend tab, ping/pong resolved) |
| 2026-04-23 | Added B-013 — wire signals/list tier filter to live data |
| 2026-04-23 | Added C-007 — Phase 3 composite score v2 + signals list endpoint |
| 2026-04-23 | Added C-006 — flow_store fix: wrong table, id field, f-string logs, 8 tests |
| 2026-04-23 | Closed B-010 → moved to Dropped (superseded by C-006 + C-008) |
| 2026-04-23 | Added B-012 — wire flow scan endpoint to live flow_events table |
| 2026-04-23 | Added C-005 — Tradier market-hours guard + session_ticks backoff fix |
| 2026-04-23 | Added C-004 options universe persistence — 30 tests |
| 2026-04-22 | Created backlog with B-001 through B-007 |
| 2026-04-22 | Added C-001 frontend deployment fix |
| 2026-04-23 | Added C-002 auth 501 fix |
| 2026-04-23 | Added C-003 Tradier stream resilience fix |
