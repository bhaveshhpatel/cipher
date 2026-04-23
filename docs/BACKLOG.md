# Cipher — Product Backlog

> Maintained by: Dhruv Patel (bhaveshhpatel@yahoo.com)  
> Last updated: 2026-04-23  
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
| B-010 | Supabase DB — signal storage | 🔲 Todo | Universe tables live (C-004). Wire signal storage + user prefs into DB. |
| B-011 | Redis integration | 🔲 Todo | Redis is in config but not used. Candidate for signal caching + WebSocket pub/sub at scale. |

---

## Completed

| # | Item | Completed | Notes |
|---|------|-----------|-------|
| C-001 | Frontend deployment CI/CD fixed | 2026-04-22 | Fixed double-nested path bug, removed broken @secret refs from vercel.json, confirmed login page live. |
| C-002 | Auth register/login 501 error fixed | 2026-04-23 | Next.js proxy: fixed ReadableStream body bug (Vercel runtime), Next.js 15 async params, TS strict mode. |
| C-003 | Tradier stream — 9 failure modes fixed | 2026-04-23 | Full production-grade resilience rewrite. See `docs/specs.md` § Tradier Stream Architecture. |
| C-004 | Options universe persistence | 2026-04-23 | ~8,000-symbol tradeable universe persisted in Supabase. DB-first startup (< 1s cold start), 24h background refresh, full fallback chain. `symbols_loader.py` + `universe_store.py` + `001_options_universe.sql` migration (applied). 30 test cases. See `docs/specs.md` § Options Universe Persistence. |
| C-005 | Tradier stream — market-hours guard + backoff fix | 2026-04-23 | Commit 9a32d4b. `_is_market_hours()` helper (ET timezone, stdlib only). Market-closed guard at top of reconnect loop — sleeps 60s, logs once per minute. `session_ticks`-aware backoff: attempt resets only when real data was received. `mode = market_closed` exposed in stats. Eliminates overnight reconnect spam. |

---

## Dropped

| # | Item | Dropped | Reason |
|---|------|---------|--------|
| — | — | — | — |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-04-23 | Added C-005 — Tradier market-hours guard + session_ticks backoff fix |
| 2026-04-23 | Updated B-008 — mode enum now includes `market_closed` |
| 2026-04-23 | Added C-004 options universe persistence — symbols_loader, universe_store, DB migration, 30 tests |
| 2026-04-23 | Updated B-010 — universe tables live; now specifically about signal storage |
| 2026-04-22 | Created backlog with B-001 through B-007 |
| 2026-04-22 | Added C-001 frontend deployment fix |
| 2026-04-23 | Added C-002 auth 501 fix |
| 2026-04-23 | Added C-003 Tradier stream resilience fix (9 failure modes) |
| 2026-04-23 | Added B-008 stream health endpoint, B-009 trade executor wiring, B-010 Supabase DB, B-011 Redis |
