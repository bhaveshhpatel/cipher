# Cipher — Regression Test Suite

> Last updated: 2026-04-28
> Branch: stable/ingestion-flow-2026-04-28
> Backend: 55+ test files · CI gate ≥ 90% coverage (`--cov-fail-under=90`)
> Frontend: jest · CI gate ≥ 75% lines/functions globally

---

## How to Run

```bash
# Backend — full suite with coverage
cd backend
pip install -r requirements-dev.txt
pytest

# Backend — skip coverage for speed
pytest --no-cov

# Frontend
cd frontend
npx jest --coverage
```

---

## CI Gates

```
Push to main (backend/**)
  └── lint
        └── regression (--cov-fail-under=90)
              └── Railway auto-deploys

Push to main (frontend/**)
  └── typecheck + lint
        └── regression (jest --ci --coverage, thresholds in jest.config.ts)
              └── build
                    └── deploy (vercel --prod)

Pull Request
  └── Same gates + orgoro/coverage posts PR comment with coverage diff
```

---

## Backend Test File Inventory

| File | What It Covers |
|------|----------------|
| `conftest.py` | Shared fixtures and pytest configuration |
| `test_4a_oi_pipeline.py` | OI-gated tier classification two-pass pipeline (Feature 4A-OI) |
| `test_4a_tier_engine.py` | TierEngine: assign_tiers, OI grace path removed, T1/T2/T3 with all 3 conditions |
| `test_6layer_regression.py` | Full 6-layer pipeline end-to-end regression: Layer 1–6 integration |
| `test_admin_router.py` | `/admin/tier-thresholds` GET/PATCH, `/admin/tier-distribution` GET, admin JWT auth |
| `test_alert_level_fix.py` | ALERT-LEVEL fix: `flow_episodes.alert_level` reads `alert_level` not `recommendation`; CONVICTION/STRONG_SIGNAL/ALERT/WATCH written correctly |
| `test_async_bus.py` | `AsyncEventBus` publish, subscribe, fan-out, channel isolation |
| `test_async_bus_coverage.py` | Edge cases and branch coverage for `core/async_bus.py` |
| `test_auth_cors_regression.py` | CORS allow_origin_regex: Vercel preview URLs, localhost:3000/3001, explicit origins |
| `test_auth_flow.py` | Register → login → `/auth/me` JWT flow, expired token rejection, wrong password |
| `test_auth_router.py` | Auth router unit tests: register/login endpoint contracts |
| `test_classifier.py` | `bid_ask_classifier.py`: ABOVE_ASK/AT_ASK/MID/AT_BID/BELOW_BID; `trade_type_detector.py`: SWEEP/BLOCK/SPLIT/SINGLE; `is_golden_sweep` |
| `test_composite_signal_engine.py` | `build_composite()`: 3-component score weights (flow×0.55 + backtest×0.35 + vol×0.10), BUY/SELL/HOLD threshold at 0.65, volume_premium_factor OI fallback |
| `test_composite_signal_extended.py` | Extended composite signal scenarios: edge cases, tier impact, zero OI, capped premium |
| `test_config.py` | `config.py` Pydantic settings: env var parsing, `priority_symbols` property, defaults |
| `test_dedup_cache.py` | `DedupCache`: TTL=5s, key=(occ_symbol, size, round(fill,1)), is_duplicate, is_sweep |
| `test_dedup_cache_coverage.py` | Branch/edge coverage for `DedupCache` |
| `test_dedup_coverage.py` | Dedup full coverage: sweep window=8s, exchange count, dedup_stats(), get_exchange_count() |
| `test_dedup_edge_cases.py` | C-019 edge cases: TTL boundary, fill rounding 1dp, multi-exchange sweep detection, bucket boundary fix |
| `test_dedup_kwargs_fix.py` | DEDUP-KWARGS fix: `is_duplicate()` called with positional `occ_symbol`; no TypeError; `_stats["deduped"]` increments correctly |
| `test_demo_engine.py` | `demo_engine.py`: timesale envelope, exchange field (`exch`), inter-exchange delay 50–300ms, dedup wiring |
| `test_demo_engine_coverage.py` | Branch coverage for demo_engine |
| `test_ensemble_runner.py` | `EnsembleRunner`: majority vote, `EnsembleResult`, per-agent `name` field, bull/bear/hold counts |
| `test_flow_and_stats.py` | Flow stats helpers and `get_stats()` merge with dedup_stats |
| `test_flow_endpoint.py` | `GET /api/flow/scan`: queries `flow_episodes` (not `flow_events`), pagination, filters |
| `test_flow_store.py` | `flow_store.py`: flush interval 500ms, _FLUSH_MAX_ROWS=100 early-flush, SERVICE_ROLE_KEY only |
| `test_gate2_retrigger.py` | Gate 2 retrigger: no re-emission below $50k delta; re-emits exactly at $50k; `last_signaled_premium` updated; SPY/QQQ burst produces single row not N rows |
| `test_health_stream.py` | `GET /health/stream`: errors/reconnects/last_reconnect_at fields (B-008) |
| `test_history_router.py` | `GET /api/signals/history`: pagination, filters (ticker/direction/tier/min_conviction), SERVICE_ROLE_KEY |
| `test_ingestion_config.py` | Universe ingestion config: UNIVERSE_MIN_PRICE, UNIVERSE_MIN_VOLUME, priority_symbols, SEED_SYMBOLS |
| `test_main_app.py` | FastAPI app: all routers registered, health endpoints, lifespan spawns prewarm_task, `test_lifespan_spawns_prewarm_task` |
| `test_midcap_screener.py` | `midcap_screener.py`: mid-cap symbol filtering logic |
| `test_occ_parser.py` | `_parse_occ_symbol`, `_calc_dte`, `_parse_timestamp`, `parse_tradier_trade` full path (C-015 `last` field, C-010 OCC-derived ticker, C-011 DTE/strike/expiry, C-018 synthetic quote flag) |
| `test_options_flow_parser.py` | Extended parser tests: fill_price fallback chain, is_synthetic_quote, registry enrichment, all 4 influence tiers, conviction score, golden sweep |
| `test_registry_prewarm.py` | `_registry_prewarm_loop()`: weekend skip, weekday 09:15 ET scheduling, registry.build() called, exception non-fatal (5 cases) |
| `test_registry_shared_instance.py` | D-001 fix: `stream_options_flow(registry=...)` accepts pre-built registry; no second `build()` call; only one `refresh_loop()` task spawned (D-002) |
| `test_repetition_engine.py` | `RepetitionAccumulator`: Gate 1 (count≥3 OR prem≥$10k), Gate 2 ($50k delta retrigger), rolling window prune, cross-contract isolation; `RepetitionEpisode`: is_accelerating, summary_str; `get_alert_level()`: CONVICTION/STRONG_SIGNAL/ALERT/WATCH |
| `test_signal_store.py` | `signal_store.py`: CompositeSignal persistence, swarm fields (direction/confidence/agents JSONB/votes), SERVICE_ROLE_KEY |
| `test_signal_store_coverage.py` | Extended signal_store coverage: error paths, partial swarm data, bus channel wiring |
| `test_signal_store_r3.py` | signal_store regression-3 scenarios |
| `test_simulation_and_ws.py` | Simulation + WS integration: swarm triggered from WS signal, ensemble result forwarded |
| `test_simulation_router.py` | `POST /api/simulate`: swarm invocation, HOLD fallback without GROQ_API_KEY, agent count snapping |
| `test_smart_signals_router.py` | `/api/signals/composite/{ticker}`, `/api/signals/list`: live DB first, mock fallback, pagination |
| `test_stream_manager.py` | `StreamManager`: worker spawn, symbol diff on refresh, `_spawn_workers()` |
| `test_stream_manager_dynamic_workers.py` | D-003 fix: worker count = `ceil(registry.size() / 500)`; 31920 symbols → 64 workers; 15000 symbols → 30 workers; count logged at INFO |
| `test_stream_manager_r3.py` | Stream manager regression-3: B-021 stagger constants (200ms/0.200s), worker-N delay = N×0.2 |
| `test_stream_worker_b008.py` | B-008: `_inc_global_error()` increments `_stats["errors"]`, `_inc_global_reconnect()` sets `last_reconnect_at` (5 cases: SW-01–SW-05) |
| `test_sweep_dispatch_ttl.py` | H4 fix: `_sweep_upgrade_dispatched` is `dict[str, float]`; keys older than 1800s evicted before check; same key re-dispatched after TTL; memory does not grow unboundedly |
| `test_swarm_engine.py` | `SwarmEngine`: 3/6/9/12 agent counts, Groq invocation, HOLD fallback, agent roles |
| `test_swarm_engine_coverage.py` | Swarm engine branch coverage: agent count snapping, partial responses, all 12 agent roles |
| `test_symbol_registry_coverage.py` | `SymbolRegistry`: build(), size(), get_oi_map(), set_tier_map(), refresh_loop(), per-tier ATM/DTE params |
| `test_symbols_loader.py` | `load_universe()`: CBOE → Tradier validate → screen pipeline, source tagging, stream_eligible flag |
| `test_tier_engine.py` | `assign_tiers()`: T1/T2/T3 with vol+price+OI conditions, admin whitelist, DB threshold cache (300s) |
| `test_trade_executor.py` | `TradeExecutor`: `place_option_order()`, `get_positions()`, paper/live mode |
| `test_tradier_client.py` | `TradierClient`: session token, `get_session_token()` flow |
| `test_tradier_client_coverage.py` | B-022 semaphore (max 3 concurrent), B-023 explicit 429 → Retry-After sleep → retry |
| `test_tradier_stream.py` | `stream_options_flow()`: market-hours guard, demo mode, dedup wiring, stats merge |
| `test_universe_idempotent.py` | U-1 fix: snapshot reuse when < 20h old and symbol count within ±10%; `uq_universe_snapshot_symbol` constraint prevents duplicates on restart |
| `test_universe_screener.py` | `universe_screener.py` (deprecated): legacy screen_universe tests kept for reference |
| `test_universe_screener_coverage.py` | Universe screener branch coverage |
| `test_universe_store.py` | `universe_store.py`: load_fresh_snapshot, load_any_snapshot, save_snapshot, upsert_symbol_quotes, load_tier_map |
| `test_ws_lifecycle.py` | WS connection lifecycle: JWT auth on connect, 4001 close on bad token |
| `test_ws_router.py` | `ws.py`: ping/pong heartbeat (25s ping, 10s pong timeout, 1001 close on timeout), signal delivery |

---

## Coverage Thresholds

| Target | Threshold | Config |
|--------|-----------|--------|
| Backend | ≥ 90% | `pytest.ini` `--cov-fail-under=90` + `.coveragerc` `fail_under=90` |
| Frontend | ≥ 75% lines/functions globally | `jest.config.ts` `coverageThreshold` |

---

## Key Regression Anchors

| Anchor | Test | Why It Matters |
|--------|------|----------------|
| Registry prewarm spawned at lifespan | `test_main_app.py::test_lifespan_spawns_prewarm_task` | Ensures `_registry_prewarm_loop` task created on startup |
| Dedup actually wired in production | `test_dedup_edge_cases.py` | C-019: dedup was inert in prod before fix |
| Dedup called with positional arg (no TypeError) | `test_dedup_kwargs_fix.py` | DEDUP-KWARGS: `occ_symbol=` kwarg raised TypeError on every tick |
| flow_dedup sweep detection fires | `test_dedup_edge_cases.py` | C-019: exchange field never passed before fix |
| B-008 health stats real values | `test_stream_worker_b008.py` | Was always 0/null before fix |
| CORS Vercel preview URLs | `test_auth_cors_regression.py` | allow_origin_regex not allow_origins=["*"] |
| Service role key only (no anon fallback) | `test_flow_store.py` | C-010: silent anon fallback caused 42501 RLS errors |
| flow_episodes not flow_events | `test_flow_endpoint.py` | Phase 4 fix: 82k+ rows in flow_events |
| OI-gated tier classification (all 3 conditions) | `test_4a_oi_pipeline.py` | Feature 4A-OI: vol + price + OI all required for T1/T2 |
| alert_level written from accumulator not recommendation | `test_alert_level_fix.py` | ALERT-LEVEL: every row was WATCH before fix |
| Gate 2 blocks re-emission below $50k delta | `test_gate2_retrigger.py` | SPY/QQQ burst would write hundreds of signal_history rows per minute |
| Sweep dispatch dict evicts stale keys | `test_sweep_dispatch_ttl.py` | H4: set never evicted; reprints after 30 min were silently skipped |
| Single registry instance at startup | `test_registry_shared_instance.py` | D-001: two registries doubled Tradier chain API calls |
| Dynamic worker count from registry size | `test_stream_manager_dynamic_workers.py` | D-003: 32 hard-coded workers left ~half the OCC universe unstreamed |
| Snapshot reuse on restart (idempotent) | `test_universe_idempotent.py` | U-1: restart duplicated all universe rows with no uniqueness guard |
