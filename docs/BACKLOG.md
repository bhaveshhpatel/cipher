# Cipher — Product Backlog

> Maintained by: Dhruv Patel (bhaveshhpatel@yahoo.com)  
> Last updated: 2026-04-25 (B-021 + B-022 + B-023 stream worker startup fixes added)  
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
| B-008 | Stream health endpoint | 🔲 Todo | Expose `/health/stream` returning mode (live/demo/reconnecting/market_closed), reconnect count, last tick time, deduped count. |
| B-009 | Wire `trade_executor.py` into signal flow | 🔲 Todo | `execution/trade_executor.py` exists but is not connected to the composite signal engine output. |
| B-011 | Redis integration | 🔲 Todo | Redis is in config but not used. Candidate for signal caching + WebSocket pub/sub at scale. |
| B-012 | Wire `GET /api/flow/scan` to `flow_episodes` table | 🔲 Todo | `routers/flow.py` queries `flow_episodes` (fixed in Phase 4) but full pagination + filters needed. |
| B-013 | Wire `/api/signals/list` tier filter to live data | 🔲 Todo | Tier filter currently pass-through. Needs real query against live signal data. |
| B-016 | Wire midcap screener into signal pipeline | 🔲 Todo | `signals/midcap_screener.py` exists but not confirmed in signal path. |
| B-017 | Load test signals endpoints (50 concurrent users) | 🔲 Todo | Benchmark `/api/signals/list` and `/api/signals/history` under load. |
| B-018 | WebSocket fan-out benchmark (50+ subscribers) | 🔲 Todo | Test `ws.py` throughput with many simultaneous clients. |
| B-021 | Stagger Worker Startup (200ms between workers) | 🔲 Todo | Latency impact: One-time startup cost of 32 × 200ms = 6.4 seconds before all workers are streaming. Once running, zero ongoing latency impact — all 32 workers stream in real-time exactly as before. This is purely a startup delay, not a per-signal delay. Signal quality impact: None whatsoever. Staggering only affects when the connection is established, not what data flows through it. Verdict: Pure win. Zero downside after startup. |
| B-022 | Global Session Token Semaphore (max 3 concurrent) | 🔲 Todo | Latency impact: With Semaphore(3), 32 workers fetch tokens in batches of 3. Each get_session_token() call takes ~200–500ms round trip to Tradier. So total token acquisition time: ⌈32/3⌉×400ms≈4.3 seconds. Again, one-time startup only. The ongoing implication is during the 30-min registry refresh — the full worker restart takes ~4–5 extra seconds to restabilize. During that window you have reduced coverage, but it's the same coverage gap you already have today (just more controlled). Signal quality impact: None ongoing. The controlled restart actually improves quality vs. today's chaotic simultaneous token burst that causes a death spiral. Verdict: Pure win. Small one-time startup cost, prevents the silent-drop spiral. |
| B-023 | Handle 429 Explicitly | 🔲 Todo | Latency impact: If Tradier is already returning 429s (which is likely happening silently today), this fix adds a Retry-After sleep — typically 10–30s. However, today without this fix, a 429 causes raise_for_status() → exception → worker returns None → immediate re-backoff → re-attempt anyway. The fix actually makes the backoff smarter and shorter than the current unhandled crash path. Signal quality impact: During a 429 window, some workers are waiting rather than streaming. That's the same as today — except today they crash-loop and burn more API budget. The fix reduces overall token request pressure, meaning fewer 429s in the first place. Verdict: Net positive. Prevents crash loops that cause longer outages than the Retry-After sleep. |

---

## Completed

| # | Item | Completed | Notes |
|---|------|-----------|-------|
| B-019 | Admin tier-thresholds UI + endpoints | 2026-04-25 | `GET /api/admin/tier-thresholds` (returns active row + cache metadata) + `PATCH /api/admin/tier-thresholds` (updates columns, busts in-process cache). `TierThresholdsCard` in admin page — per-field save, T1/T2/T3 grouped, dirty state, cache badge, last-updated footer. Migration 012 (RLS + updated_at trigger). Tests ADM-05 + ADM-06. |
| B-020 | `GET /admin/tier-distribution` endpoint | 2026-04-25 | Returns `{ snapshot_id, total, tiers: { "1": {count, samples}, "2": ..., "3": ... } }` from active universe snapshot. Shipped in same commit as B-019. |
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
| C-016 | Layer 4 dedup not wired into hot path | 2026-04-24 | `DedupCache` was built (C-010) but `flow_dedup.is_duplicate()` was never called in `_process_trade()`. Fixed: added dedup gate + sweep upgrade in `tradier_stream.py`. Added `deduped` stat counter. Regression tests: `test_6layer_regression.py` tests L4-01 through L4-10. |
| C-017 | Layer 2 manager.refresh() not hooked to registry loop | 2026-04-24 | `registry.refresh_loop()` rebuilt symbols every 30min but never notified `StreamManager`. Workers streamed stale OCC symbols. Fixed: `_registry_refresh_with_manager_notify()` calls `await manager.refresh()` after every rebuild. Regression tests: L2-01 through L2-06. |
| C-018 | Layer 5 flush interval 5s → 500ms + 100-row early flush | 2026-04-24 | `_FLUSH_INTERVAL` was 5s (spec: 500ms). At 62K rows/day: ~430 rows per flush window. Fixed: `_FLUSH_INTERVAL=0.5`, `_FLUSH_MAX_ROWS=100`, early-flush in `persist_flow_event()`. Regression tests: L5-01 through L5-08. |
| C-019 | Layer 4 dedup TTL + sweep detection overhaul | 2026-04-24 | TTL 2s→5s, sweep window 5s→8s, eliminated `int(ts//2)` bucket boundary bug, fill key 2dp→1dp, exchange field wired. 5 bugs fixed. Regression tests: C-019 suite in `test_6layer_regression.py`. |
| C-020 | Feature 4A — Tier engine + universe tier assignment | 2026-04-25 | `services/tier_engine.py` (new): `TierEngine`, `_TierParams`, `set_tier_map()`, `get_tier()`, admin whitelist. `universe_store.load_tier_map()`. Migrations 010 + 011 applied. 35 tests across `test_4a_tier_engine.py`, `test_6layer_regression.py`, `test_universe_store.py`. |

---

## Dropped

| # | Item | Dropped | Reason |
|---|------|---------|--------|
| B-010 | Supabase DB — signal storage | 2026-04-23 | Completed as C-006 + C-008. All three signal tables live. |
| B-014 | Confirm Layer 1 (SymbolRegistry) wired into stream pipeline | 2026-04-24 | Confirmed wired — registry enrichment in parser + registry built in stream_options_flow(). |
| B-015 | Confirm Layer 2 (StreamManager + StreamWorker) wired into main | 2026-04-24 | Confirmed wired — StreamManager spawned in stream_options_flow() with process_fn=_process_trade. |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-04-25 | Added B-021 — Fix 1: Stagger Worker Startup (200ms between workers). |
| 2026-04-25 | Added B-022 — Fix 2: Global Session Token Semaphore (max 3 concurrent). |
| 2026-04-25 | Added B-023 — Fix 3: Handle 429 Explicitly. |
| 2026-04-25 | Closed B-019 — Admin tier-thresholds: GET read endpoint, PATCH update, TierThresholdsCard UI, migration 012, tests ADM-05/06. |
| 2026-04-25 | Closed B-020 — GET /admin/tier-distribution endpoint shipped in same commit as B-019. |
| 2026-04-25 | Added C-020 — Feature 4A tier engine complete. Added B-019, B-020 admin tier endpoints to active backlog. |
| 2026-04-24 | Added C-019 — Layer 4 dedup TTL overhaul (5 bugs). |
| 2026-04-24 | Added C-016, C-017, C-018 — 6-layer gap-fix audit. Layer 4 dedup wired, Layer 2 refresh notify hooked, Layer 5 flush corrected to 500ms/100-row. |
| 2026-04-24 | Closed B-014 and B-015 — Layer 1 and Layer 2 integration confirmed and verified. |
| 2026-04-24 | Added C-009 through C-015 — Phase 5A completions (swarm, dedup, registry, stream manager, trade executor, fill_price fix) |
| 2026-04-24 | Added B-014 through B-018 — Phase 6 TODOs (Layer 1/2 integration confirm, midcap screener, load tests) |
| 2026-04-23 | Added C-008 — Phase 4 signal history |
| 2026-04-23 | Added B-013 — wire signals/list tier filter to live data |
| 2026-04-23 | Added C-007 — Phase 3 composite score v2 + signals list endpoint |
| 2026-04-23 | Added C-006 — flow_store fix: wrong table, id field, f-string logs, 8 tests |
| 2026-04-23 | Added B-012 — wire flow scan endpoint to live flow_episodes table |
| 2026-04-23 | Added C-005 — Tradier market-hours guard + session_ticks backoff fix |
| 2026-04-23 | Added C-004 options universe persistence — 30 tests |
| 2026-04-22 | Created backlog with B-001 through B-007 |
| 2026-04-22 | Added C-001 frontend deployment fix |
| 2026-04-23 | Added C-002 auth 501 fix |
| 2026-04-23 | Added C-003 Tradier stream resilience fix |
