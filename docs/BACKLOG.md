# Cipher — Product Backlog

> Maintained by: Dhruv Patel (bhaveshhpatel@yahoo.com)  
> Last updated: 2026-04-26 (B-024 closed, C-021 registry prewarm, C-022 CORS regex, ARCHITECTURE.md rebuild)  
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
| B-009 | Wire `trade_executor.py` into signal flow | 🔲 Todo | `execution/trade_executor.py` exists but is not connected to the composite signal engine output. |
| B-011 | Redis integration | 🔲 Todo | Redis is in config but not used. Candidate for signal caching + WebSocket pub/sub at scale. |
| B-012 | Wire `GET /api/flow/scan` to `flow_episodes` table | 🔲 Todo | `routers/flow.py` queries `flow_episodes` (fixed in Phase 4) but full pagination + filters needed. |
| B-013 | Wire `/api/signals/list` tier filter to live data | 🔲 Todo | Tier filter currently pass-through. Needs real query against live signal data. |
| B-016 | Wire midcap screener into signal pipeline | 🔲 Todo | `signals/midcap_screener.py` exists but not confirmed in signal path. |
| B-017 | Load test signals endpoints (50 concurrent users) | 🔲 Todo | Benchmark `/api/signals/list` and `/api/signals/history` under load. |
| B-018 | WebSocket fan-out benchmark (50+ subscribers) | 🔲 Todo | Test `ws.py` throughput with many simultaneous clients. |
| B-025 | Regression P5 — frontend UI component tests | 🔲 Todo | SignalFeed, FlowTable, SimulationPanel, login page, dashboard page |
| B-026 | Frontend WS pong implementation | 🔲 Todo | Frontend must send `{"type":"pong"}` within 10s of ping or Railway kills connection (code 1001) |
| B-027 | Raise coverage gates Phase 6 | 🔲 Todo | Backend: 90% → 95%. Frontend: 75% → 85% global. After P5 UI tests complete. |

---

## Completed

| # | Item | Completed | Notes |
|---|------|-----------|-------|
| B-025-P1 | Regression P1 — auth/admin/config tests | 2026-04-25 | `test_auth_router.py` AUTH-01–15, `test_admin_router.py` ADMIN-01–12, `test_config.py` CFG-01–10, `test_demo_engine.py` DEMO-01–14 |
| B-025-P2 | Regression P2 — dedup/swarm/ensemble/screener tests | 2026-04-25 | `test_dedup.py` DEDUP-01–22, `test_swarm_engine.py` SWM-01–25, `test_ensemble_runner.py` ENS-01–18, `test_midcap_screener.py` MCS-01–10 |
| B-025-P3 | Regression P3 — executor/routers/main tests | 2026-04-25 | `test_trade_executor.py` TE-01–14, `test_simulation_router.py` SIM-01–12, `test_smart_signals_router.py` SS-01–16, `test_main_app.py` MAIN-01–15 |
| B-025-CI | Regression CI gate setup | 2026-04-25 | `pytest.ini`, `.coveragerc`, `requirements-dev.txt` updated. `backend.yml` rebuilt (lint→regression). `frontend.yml` rebuilt (typecheck→regression→build→deploy). `jest.config.ts` with coverageThreshold. PR coverage bot via `orgoro/coverage@v3.2`. |
| B-024 | Regression P4 — history/flow/ws full test coverage | 2026-04-26 | `test_history_router.py`, `test_flow_endpoint.py`, `test_ws_router.py`, `test_ws_lifecycle.py`, `test_simulation_and_ws.py`, `test_flow_store.py`, `test_signal_store.py` all present in 48-file test suite. |
| B-008 | Stream health endpoint `GET /health/stream` | 2026-04-25 | `routers/health.py` — `StreamHealthOut` Pydantic model with 11 fields. |
| B-023 | Handle 429 Explicitly in `get_session_token()` | 2026-04-25 | Explicit `if resp.status_code == 429:`. Reads `Retry-After` header (default 10s). |
| B-022 | Global Session Token Semaphore (max 3 concurrent) | 2026-04-25 | `_SESSION_SEM = asyncio.Semaphore(3)` wraps `get_session_token()`. |
| B-021 | Stagger Worker Startup (200ms between workers) | 2026-04-25 | `_WORKER_STARTUP_STAGGER_MS=200` in `stream_manager.py`. |
| B-019 | Admin tier-thresholds UI + endpoints | 2026-04-25 | `GET /api/admin/tier-thresholds` + `PATCH`. `TierThresholdsCard` UI. Migration 012. |
| B-020 | `GET /admin/tier-distribution` endpoint | 2026-04-25 | Returns tier distribution from active universe snapshot. |
| C-022 | CORS allow_origin_regex — Vercel preview + localhost | 2026-04-26 | `main.py` uses `allow_origin_regex` (not `allow_origins=["*"]` which breaks `allow_credentials=True`). Pattern: `https://*.vercel.app`, `localhost:3000/3001`, `127.0.0.1:3000`, explicit env-var origins. Tested in `test_auth_cors_regression.py`. |
| C-021 | Registry pre-warm loop (`_registry_prewarm_loop`) | 2026-04-26 | Background async task in `main.py`. Fires every weekday at 09:15 ET. Skips weekends. Calls `registry.build()`. Non-fatal on error. Wired as `prewarm_task` in lifespan; cancelled + awaited on shutdown. 5 test cases in `test_registry_prewarm.py` + `test_lifespan_spawns_prewarm_task` in `test_main_app.py`. |
| C-001 | Frontend deployment CI/CD fixed | 2026-04-22 | Fixed double-nested path bug, removed broken @secret refs from vercel.json. |
| C-002 | Auth register/login 501 error fixed | 2026-04-23 | Next.js proxy: ReadableStream body bug, Next.js 15 async params, TS strict mode. |
| C-003 | Tradier stream — 9 failure modes fixed | 2026-04-23 | Full production-grade resilience rewrite. |
| C-004 | Options universe persistence | 2026-04-23 | ~8,000-symbol universe in Supabase. DB-first startup, 24h refresh, full fallback chain. |
| C-005 | Tradier stream — market-hours guard + backoff fix | 2026-04-23 | |
| C-006 | flow_store.py — fix wrong table + id field + log crash | 2026-04-23 | |
| C-007 | Phase 3 — composite score v2 + signals list | 2026-04-23 | |
| C-008 | Phase 4 — signal history | 2026-04-23 | |
| C-009 | Phase 5A — AI swarm expansion (12 agents) | 2026-04-24 | |
| C-010 | Phase 5A — DedupCache (Layer 4) | 2026-04-24 | |
| C-011 | Phase 5A — Symbol Registry (Layer 1) | 2026-04-24 | |
| C-012 | Phase 5A — Stream Manager/Worker (Layer 2) | 2026-04-24 | |
| C-013 | Phase 5A — Trade Executor | 2026-04-24 | |
| C-014 | Phase 5A — Migration 004 (swarm fields) | 2026-04-24 | |
| C-015 | fill_price bug fix — Layer 3 parser | 2026-04-24 | |
| C-016 | Layer 4 dedup not wired into hot path | 2026-04-24 | |
| C-017 | Layer 2 manager.refresh() not hooked to registry loop | 2026-04-24 | |
| C-018 | Layer 5 flush interval 5s → 500ms + 100-row early flush | 2026-04-24 | |
| C-019 | Layer 4 dedup TTL + sweep detection overhaul | 2026-04-24 | TTL 2s→5s, sweep window 5s→8s, fill key 2dp→1dp, bucket boundary bug eliminated, exchange field wired. |
| C-020 | Feature 4A — Tier engine + universe tier assignment | 2026-04-25 | |

---

## Dropped

| # | Item | Dropped | Reason |
|---|------|---------|--------|
| B-010 | Supabase DB — signal storage | 2026-04-23 | Completed as C-006 + C-008. All three signal tables live. |
| B-014 | Confirm Layer 1 (SymbolRegistry) wired into stream pipeline | 2026-04-24 | Confirmed wired. |
| B-015 | Confirm Layer 2 (StreamManager + StreamWorker) wired into main | 2026-04-24 | Confirmed wired. |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-04-26 | Closed B-024 — Regression P4 test files confirmed in 48-file suite. Added C-021 (registry prewarm), C-022 (CORS regex). Rebuilt ARCHITECTURE.md to match current code. |
| 2026-04-25 | Phase 5B complete: ~380 test cases, CI hard gate (≥90% backend, ≥75% frontend), PR coverage bot. Added B-024–B-027. |
| 2026-04-25 | Closed B-008 — Stream health endpoint wired. |
| 2026-04-25 | Closed B-021 — Stagger worker startup. |
| 2026-04-25 | Closed B-022 — Session token semaphore(3). |
| 2026-04-25 | Closed B-023 — Explicit 429 + Retry-After handler. |
| 2026-04-25 | Closed B-019 — Admin tier-thresholds endpoints + UI. |
| 2026-04-25 | Closed B-020 — GET /admin/tier-distribution. |
| 2026-04-25 | Added C-020 — Feature 4A tier engine complete. |
| 2026-04-24 | Added C-016, C-017, C-018, C-019 — 6-layer gap-fix audit. |
| 2026-04-24 | Closed B-014 and B-015 — Layer 1 and Layer 2 integration confirmed. |
| 2026-04-24 | Added C-009 through C-015 — Phase 5A completions. |
| 2026-04-23 | Added C-001 through C-008. |
