# Cipher — Product Backlog

> Maintained by: Dhruv Patel (bhaveshhpatel@yahoo.com)
> Last updated: 2026-04-28 (STREAM-1/2/3 closed, D-001–D-003 closed, ALERT-LEVEL/DEDUP-KWARGS/Gate2/H4/U-1/FLOW-DEBUG closed, docs round complete)
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
| B-009 | Wire `trade_executor.py` into signal flow | 🔲 Todo | `execution/trade_executor.py` exists but is not connected to composite signal engine output. |
| B-011 | Redis integration | 🔲 Todo | Redis is in config but not used. Candidate for signal caching + WebSocket pub/sub at scale. |
| B-012 | Wire `GET /api/flow/scan` to `flow_episodes` table | 🔲 Todo | Full pagination + filters needed. Table query fixed in Phase 4 but endpoint not fully wired. |
| B-013 | Wire `/api/signals/list` tier filter to live data | 🔲 Todo | Tier filter currently pass-through. Needs real query against live signal data. |
| B-016 | Wire midcap screener into signal pipeline | 🔲 Todo | `signals/midcap_screener.py` exists but not confirmed in signal path. |
| B-017 | Load test signals endpoints (50 concurrent users) | 🔲 Todo | Benchmark `/api/signals/list` and `/api/signals/history` under load. |
| B-018 | WebSocket fan-out benchmark (50+ subscribers) | 🔲 Todo | Test `ws.py` throughput with many simultaneous clients. |
| B-025 | Regression P5 — frontend UI component tests | 🔲 Todo | SignalFeed, FlowTable, SimulationPanel, login page, dashboard page. |
| B-026 | Frontend WS pong implementation | 🔲 Todo | Frontend must send `{"type":"pong"}` within 10s of server ping or Railway kills connection (code 1001). |
| B-027 | Raise coverage gates Phase 6 | 🔲 Todo | Backend: 90% → 95%. Frontend: 75% → 85% global. After B-025 UI tests complete. |
| B-028 | STREAM-3 regression test suite | 🔲 Todo | Test coverage for shared-session parallel worker model: token refresh detection, 64-worker fan-out, manager STREAM_HEALTH log cadence, per-worker STREAM_STATS, `_token_expired` propagation, queue_depth = 50,000. |
| B-029 | E2E smoke test: tick → DB row confirmed | 🔲 Todo | Integration test that sends a real (or realistic mock) timesale tick through the full 6-layer pipeline and asserts a row lands in `flow_events` + `flow_episodes` within 2s. |
| B-030 | Token expiry detection hardening | 🔲 Todo | STREAM-3 detects `_token_expired` within 60s. Confirm: (1) manager polls all workers, (2) token refresh + full worker respawn fires correctly, (3) no tick gap during token swap. Needs dedicated test. |
| B-031 | Verify migration 013 idempotency in production | 🔲 Todo | Migration 013 added `UNIQUE(snapshot_id, symbol)` without `IF NOT EXISTS` (PG limitation). Confirm constraint exists in Supabase and that restart cycle produces exactly 1 snapshot row set. |

---

## Completed

| # | Item | Completed | Notes |
|---|------|-----------|-------|
| STREAM-3 | Lock removed — 64 workers fully parallel, shared session token | 2026-04-28 | All 64 workers connect simultaneously. ONE shared session token fetched by manager, passed read-only. 50ms spawn stagger (thundering-herd mitigation). Manager STREAM_HEALTH log every 30s (total_ticks, active_workers, workers_stalled, queue_depth, global rate/s). Per-worker STREAM_STATS every 30s. Token expiry detection: worker sets `_token_expired`, manager detects within 60s and refreshes + respawns. Queue bumped 10,000 → 50,000. |
| STREAM-2 | Shared session token + 500-symbol chunks | 2026-04-28 | _CHUNK_SIZE restored to 500. Manager fetches ONE token before spawning workers, passes as `shared_session_token`. Workers skip `get_session_token()` when provided. Global `asyncio.Lock` serializes stream connections (1 active at a time — Tradier 1-session rule). `_token_expired` flag propagates 401s back to manager. |
| STREAM-1 | Single-worker collapse to fix Tradier 1-session limit | 2026-04-28 | Discovery that Tradier allows only 1 concurrent session. Collapsed to 1 worker (_CHUNK_SIZE 500 → 50,000) as interim. Superseded by STREAM-2/3. |
| CONFIG-STREAM-URL | `TRADIER_STREAM_URL` missing from `config.py` | 2026-04-28 | `stream_worker.py` uses `settings.TRADIER_STREAM_URL`. Config only had `TRADIER_BASE_URL` (api.tradier.com). Missing field caused AttributeError or silent empty-string host on connect. Fixed: `TRADIER_STREAM_URL` added with default `https://stream.tradier.com`. |
| ALERT-LEVEL | `flow_episodes.alert_level` always `WATCH` | 2026-04-28 | `_bus_signal_listener` read `sig.get("recommendation")` (BUY/SELL/HOLD) for the alert_level column. Fixed: `alert_level` injected into composite_signal message; `_bus_signal_listener` reads `sig.get("alert_level")`. |
| DEDUP-KWARGS | `TypeError` on every dedup call — Layer 4 no-op again | 2026-04-28 | `occ_symbol=occ_symbol` kwarg raised TypeError on `is_duplicate()` (positional param). Caught silently; `_stats["deduped"]` always 0. Fixed: pass positionally. |
| Gate-2 | Accumulator re-emission spam on active episodes | 2026-04-28 | `ingest_tick()` emitted on every tick after Gate 1. SPY/QQQ wrote hundreds of `signal_history` rows/min. Fixed: Gate 2 retrigger — only re-emit when `Δ total_premium >= $50,000`. `last_signaled_premium` field added to `RepetitionEpisode`. |
| H4 | `_sweep_upgrade_dispatched` set never evicted | 2026-04-28 | `Set[str]` grew unboundedly; missed re-dispatch after 30-min window. Fixed: `dict[str, float]` with 1800s TTL eviction before each check. |
| U-1 | Universe snapshot duplicates on every Railway restart | 2026-04-28 | `_sync_save_snapshot()` always created new UUID. No DB uniqueness constraint. Fixed: snapshot reuse if < 20h old ±10% symbol count. Migration 013 added `UNIQUE(snapshot_id, symbol)`. |
| FLOW-DEBUG | All drop gates silent in Railway logs | 2026-04-28 | Parse failures, accumulator gates, dedup hits all at DEBUG. Dead stream indistinguishable from healthy. Fixed: upgraded to INFO; first 5 ticks individually logged; 100-tick funnel summary. |
| D-003 | Worker count hard-coded to 32 | 2026-04-28 | 32 workers for ~31,920 symbols → ~half unstreamed. Fixed: `worker_count = ceil(registry.size() / _CHUNK_SIZE)`. |
| D-001 / D-002 | Dual `SymbolRegistry` build at startup | 2026-04-28 | `main.py` and `stream_options_flow()` each called `build()`. Two Tradier chain fetches, two refresh loops. Fixed: `stream_options_flow(registry=...)` accepts pre-built registry; polls `is_ready()` instead of building. |
| C-022 | CORS `allow_origin_regex` — Vercel preview + localhost | 2026-04-26 | `main.py` uses `allow_origin_regex`. Pattern covers `https://*.vercel.app`, `localhost:3000/3001`, `127.0.0.1:3000`, explicit env-var origins. |
| C-021 | Registry pre-warm loop (`_registry_prewarm_loop`) | 2026-04-26 | Background async task in `main.py`. Fires every weekday at 09:15 ET. Skips weekends. Calls `registry.build()`. Non-fatal on error. 5 test cases. |
| B-024 | Regression P4 — history/flow/ws test coverage | 2026-04-26 | `test_history_router.py`, `test_flow_endpoint.py`, `test_ws_router.py`, `test_ws_lifecycle.py`, `test_simulation_and_ws.py`, `test_flow_store.py`, `test_signal_store.py`. |
| B-025-P1 | Regression P1 — auth/admin/config tests | 2026-04-25 | `test_auth_router.py` AUTH-01–15, `test_admin_router.py` ADMIN-01–12, `test_config.py` CFG-01–10, `test_demo_engine.py` DEMO-01–14. |
| B-025-P2 | Regression P2 — dedup/swarm/ensemble/screener tests | 2026-04-25 | `test_dedup.py` DEDUP-01–22, `test_swarm_engine.py` SWM-01–25, `test_ensemble_runner.py` ENS-01–18, `test_midcap_screener.py` MCS-01–10. |
| B-025-P3 | Regression P3 — executor/routers/main tests | 2026-04-25 | `test_trade_executor.py` TE-01–14, `test_simulation_router.py` SIM-01–12, `test_smart_signals_router.py` SS-01–16, `test_main_app.py` MAIN-01–15. |
| B-025-CI | Regression CI gate setup | 2026-04-25 | `pytest.ini`, `.coveragerc`, `requirements-dev.txt`. `backend.yml` rebuilt (lint→regression). `frontend.yml` rebuilt (typecheck→regression→build→deploy). `jest.config.ts` with coverageThreshold. PR coverage bot via `orgoro/coverage@v3.2`. |
| B-008 | Stream health endpoint `GET /health/stream` | 2026-04-25 | `routers/health.py` — `StreamHealthOut` Pydantic model with 11 fields. |
| B-023 | Handle 429 explicitly in `get_session_token()` | 2026-04-25 | Explicit `if resp.status_code == 429:`. Reads `Retry-After` header (default 10s). Sleeps, then retries. |
| B-022 | Global session token semaphore (max 3 concurrent) | 2026-04-25 | `_SESSION_SEM = asyncio.Semaphore(3)` wraps `get_session_token()`. Superseded by STREAM-2 shared token model but kept as fallback for standalone paths. |
| B-021 | Stagger worker startup (200ms between workers) | 2026-04-25 | `_WORKER_STARTUP_STAGGER_MS=200` in `stream_manager.py`. Superseded by STREAM-3 50ms stagger. |
| B-020 | `GET /admin/tier-distribution` endpoint | 2026-04-25 | Returns tier distribution from active universe snapshot. |
| B-019 | Admin tier-thresholds UI + endpoints | 2026-04-25 | `GET /api/admin/tier-thresholds` + `PATCH`. `TierThresholdsCard` UI. Migration 012. |
| C-020 | Feature 4A — Tier engine + universe tier assignment | 2026-04-25 | `tier_engine.py` with T1/T2/T3 thresholds, admin whitelist, `set_tier_map()`, OI integration. |
| C-019 | Layer 4 dedup TTL + sweep detection overhaul (5 bugs) | 2026-04-24 | TTL 2s→5s, sweep window 5s→8s, fill key 2dp→1dp, bucket boundary bug, exchange field wired, singleton imported. |
| C-018 | Synthetic quote flag on flow events | 2026-04-24 | `is_synthetic_quote` boolean on `OptionsFlowEvent` + `flow_events` (migration 009). |
| C-017 | Duplicate `flow_episodes` rows per signal episode | 2026-04-24 | `_bus_signal_listener` now writes `flow_episodes` only on `composite_signal` events. |
| C-016 | `UnboundLocalError` in `persist_flow_event()` | 2026-04-24 | Missing `global _flow_event_buffer` declaration. Crashed on every 100-row flush. |
| C-015 | Stream filter `trade` → `timesale` | 2026-04-23 | `filter=trade` delivers equity ticks. `filter=timesale` delivers option contract ticks with OCC symbol and real NBBO. |
| C-014 | Over-aggressive null guards silently dropping trades | 2026-04-23 | Removed `if fill == 0: return None`. Unknown ctype defaults to PUT. |
| C-013 | Tradier stream envelope not unwrapped | 2026-04-23 | `_process_trade()` now unwraps `raw[event_type]` inner dict before parser. |
| C-010 | `flow_episodes` 401 / RLS violation — anon key fallback | 2026-04-23 | Silent fallback to `SUPABASE_KEY` (anon) when service role key absent. Removed fallback entirely. |
| C-009 | `universe_screener.py` OI screener → batch quotes | 2026-04-20 | Replaced with `_fetch_batch_quotes()` in Step 3 of universe pipeline. |
| C-008 | `stream_eligible` column missing from DB migration | 2026-04-20 | Added `stream_eligible`, `last_price`, `volume` columns + index. |
| C-007 | `config.py` missing `priority_symbols` property | 2026-04-18 | Added `@property priority_symbols` splitting env var into `list[str]`. |
| C-006 | `options_universe_snapshots.provider` NOT NULL — no default | 2026-04-18 | Always pass `"tradier"` explicitly on insert. |
| C-005 | supabase-py v2 `.select()` not available after `.insert()` | 2026-04-17 | Generate `snapshot_id` via `uuid4()` in Python before insert. |
| C-001 | Frontend deployment CI/CD fixed | 2026-04-22 | Fixed double-nested path bug, removed broken @secret refs from vercel.json. |
| C-002 | Auth register/login 501 error fixed | 2026-04-23 | Next.js proxy ReadableStream body bug, Next.js 15 async params, TS strict mode. |
| C-003 | Tradier stream — 9 failure modes fixed | 2026-04-23 | Full production-grade resilience rewrite. |
| C-004 | Options universe persistence | 2026-04-23 | ~8,000-symbol universe in Supabase. DB-first startup, 24h refresh, full fallback chain. |
| C-011 | Phase 5A — Symbol Registry (Layer 1) | 2026-04-24 | |
| C-012 | Phase 5A — Stream Manager/Worker (Layer 2) | 2026-04-24 | |

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
| 2026-04-28 | Closed STREAM-1/2/3 — shared-session parallel worker model shipped. CONFIG-STREAM-URL closed. Closed ALERT-LEVEL, DEDUP-KWARGS, Gate-2, H4, U-1, FLOW-DEBUG, D-001/D-002, D-003. Added B-028 (STREAM-3 regression tests), B-029 (E2E smoke test), B-030 (token expiry hardening), B-031 (migration 013 idempotency verification). Docs round: README, CONTRIBUTING, SIGNAL_ENGINE, FIXES, REGRESSION_TESTING, CHANGELOG, BACKLOG all updated. |
| 2026-04-26 | Closed B-024 — Regression P4 test files confirmed in 48-file suite. Added C-021 (registry prewarm), C-022 (CORS regex). Rebuilt ARCHITECTURE.md to match current code. |
| 2026-04-25 | Phase 5B complete: ~380 test cases, CI hard gate (≥90% backend, ≥75% frontend), PR coverage bot. Added B-024–B-027. Closed B-008, B-021, B-022, B-023, B-019, B-020. C-020 (Feature 4A tier engine) complete. |
| 2026-04-24 | Added C-016–C-019 — 6-layer gap-fix audit. Closed B-014 and B-015 — Layer 1/2 integration confirmed. Added C-009–C-015 — Phase 5A completions. |
| 2026-04-23 | Added C-001 through C-008. |
