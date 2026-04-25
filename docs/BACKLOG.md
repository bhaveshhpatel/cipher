# Cipher — Product Backlog

> Maintained by: Dhruv Patel (bhaveshhpatel@yahoo.com)  
> Last updated: 2026-04-24 (Phase 5A)  
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
| B-012 | Wire `GET /api/flow/scan` to `flow_episodes` table | 🔲 Todo | `routers/flow.py` queries `flow_episodes` (fixed in Phase 4) but full pagination + filters needed. |
| B-013 | Wire `/api/signals/list` tier filter to live data | 🔲 Todo | Tier filter currently pass-through. Needs real query against live signal data. |
| B-014 | Confirm Layer 1 (SymbolRegistry) wired into stream pipeline | 🔲 Todo | `symbol_registry.py` exists but integration into main flow loop unconfirmed. |
| B-015 | Confirm Layer 2 (StreamManager + StreamWorker) wired into main | 🔲 Todo | `stream_manager.py` / `stream_worker.py` exist but main.py integration unconfirmed. |
| B-016 | Wire midcap screener into signal pipeline | 🔲 Todo | `signals/midcap_screener.py` exists but not confirmed in signal path. |
| B-017 | Load test signals endpoints (50 concurrent users) | 🔲 Todo | Benchmark `/api/signals/list` and `/api/signals/history` under load. |
| B-018 | WebSocket fan-out benchmark (50+ subscribers) | 🔲 Todo | Test `ws.py` throughput with many simultaneous clients. |

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
| C-008 | Phase 4 — signal history | 2026-04-23 | `signal_history` table, `signal_store.py`, `GET /api/signals/history`, `SignalHistory` component, History tab. |
| C-009 | Phase 5A — AI swarm expansion (12 agents) | 2026-04-24 | `swarm_engine.py` 12-agent Groq swarm, `SWARM_N_AGENTS` config, swarm fields in `signal_history`. |
| C-010 | Phase 5A — DedupCache (Layer 4) | 2026-04-24 | `utils/dedup.py` — 2s TTL dedup, sweep detection, module-level `flow_dedup` singleton. |
| C-011 | Phase 5A — Symbol Registry (Layer 1) | 2026-04-24 | `services/symbol_registry.py` — OCC contract map, O(1) lookup, 30-min refresh. |
| C-012 | Phase 5A — Stream Manager/Worker (Layer 2) | 2026-04-24 | `services/stream_manager.py` + `stream_worker.py` — 32 parallel Tradier connections. |
| C-013 | Phase 5A — Trade Executor | 2026-04-24 | `execution/trade_executor.py` — Tradier REST order placement, paper + live mode. |
| C-014 | Phase 5A — Migration 004 (swarm fields) | 2026-04-24 | `004_swarm_fields.sql` — adds swarm_direction, swarm_confidence, swarm_agents JSONB, vote counts. |
| C-015 | fill_price bug fix — Layer 3 parser | 2026-04-24 | Stream sends `last` as fill price, not `price`. Fixed: `tick["last"] or tick.get("price") or 0`. |

---

## Dropped

| # | Item | Dropped | Reason |
|---|------|---------|--------|
| B-010 | Supabase DB — signal storage | 2026-04-23 | Completed as C-006 + C-008. All three signal tables live. |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-04-24 | Added C-009 through C-015 — Phase 5A completions (swarm, dedup, registry, stream manager, trade executor, fill_price fix) |
| 2026-04-24 | Added B-014 through B-018 — Phase 6 TODOs (Layer 1/2 integration confirm, midcap screener, load tests) |
| 2026-04-23 | Added C-008 — Phase 4 signal history |
| 2026-04-23 | Added B-013 — wire signals/list tier filter to live data |
| 2026-04-23 | Added C-007 — Phase 3 composite score v2 + signals list endpoint |
| 2026-04-23 | Added C-006 — flow_store fix: wrong table, id field, f-string logs, 8 tests |
| 2026-04-23 | Closed B-010 → Dropped (superseded by C-006 + C-008) |
| 2026-04-23 | Added B-012 — wire flow scan endpoint to live flow_episodes table |
| 2026-04-23 | Added C-005 — Tradier market-hours guard + session_ticks backoff fix |
| 2026-04-23 | Added C-004 options universe persistence — 30 tests |
| 2026-04-22 | Created backlog with B-001 through B-007 |
| 2026-04-22 | Added C-001 frontend deployment fix |
| 2026-04-23 | Added C-002 auth 501 fix |
| 2026-04-23 | Added C-003 Tradier stream resilience fix |
