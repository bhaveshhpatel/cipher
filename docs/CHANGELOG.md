# Changelog

All notable changes to the Cipher backend are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

---

## [Ingestion Pipeline Stability] — 2026-04-28

### Summary

Nine production bugs fixed on the `stable/ingestion-flow-2026-04-28` branch covering:
alert level mis-routing, dedup TypeError, unbounded memory in sweep dispatch, Gate 2
re-emission spam, observability gaps, universe snapshot duplication, dual registry
instantiation, hard-coded worker count, and first-tick log blindness.

### Fixed

#### ALERT-LEVEL — `flow_episodes.alert_level` Always `WATCH`
- `_bus_signal_listener` was reading `sig.get("recommendation")` (BUY/SELL/HOLD) for the
  `alert_level` column. Every `flow_episodes` row was persisted as `WATCH`.
- **Fix:** `composite_signal` message now includes `alert_level` in its `signal` sub-dict.
  `_bus_signal_listener` reads `sig.get("alert_level")` (CONVICTION/STRONG_SIGNAL/ALERT/WATCH).
- **Files:** `backend/services/tradier_stream.py`, `backend/services/flow_store.py`
- **Test:** `test_alert_level_fix.py`

#### DEDUP-KWARGS — `TypeError` on Every Tick (Dedup Production No-op Again)
- `flow_dedup.is_duplicate()` was called with `occ_symbol=occ_symbol` as a keyword arg.
  `DedupCache.is_duplicate()` defines it as positional — Python raised `TypeError` on every
  tick. Caught silently by outer try/except; `_stats["deduped"]` always 0; all exchange
  copies written to DB.
- **Fix:** Pass `occ_symbol` as first positional argument.
- **Files:** `backend/services/tradier_stream.py`
- **Test:** `test_dedup_kwargs_fix.py`

#### H4 — `_sweep_upgrade_dispatched` Set Never Evicted (Memory Leak)
- `_sweep_upgrade_dispatched` was `Set[str]` with no eviction. Accumulated indefinitely
  over the trading day. Also caused missed sweep upgrades for contracts reprinting after
  30 min (stale key blocked re-dispatch).
- **Fix:** Changed to `dict[str, float]` (key → timestamp). Keys older than
  `_SWEEP_DISPATCH_TTL_S = 1800s` evicted before each membership check.
- **Files:** `backend/services/tradier_stream.py`
- **Test:** `test_sweep_dispatch_ttl.py`

#### Gate 2 — Accumulator Re-Emission Spam on Active Episodes
- `ingest_tick()` returned the episode on every tick after Gate 1 was crossed. SPY/QQQ
  would write a new `signal_history` and `flow_episodes` row on every single tick (10–100/sec).
- **Fix:** Added `last_signaled_premium` to `RepetitionEpisode`. Gate 2: only re-emit when
  `total_premium - last_signaled_premium >= SIGNAL_RETRIGGER_THRESHOLD ($50,000)`.
- **Files:** `backend/signals/repetition_accumulator.py`
- **Test:** `test_gate2_retrigger.py`

#### FLOW-DEBUG — Silent Drop Gates (No Railway Log Visibility)
- All tick drop gates logged at DEBUG or were silent. A dead stream looked identical to a
  healthy one in Railway logs.
- **Fix:** Parse failures, accumulator gates, and dedup hits upgraded to INFO. First 5 ticks
  individually logged. Periodic 100-tick funnel summary at INFO.
- **Files:** `backend/services/tradier_stream.py`
- **New stats in `/health/stream`:** `parsed_count`, `accumulator_gated`, `parse_failed`

#### U-1 — Snapshot Duplication on Every Railway Restart
- `universe_store._sync_save_snapshot()` always created a new snapshot UUID on startup.
  No uniqueness constraint on `options_universe_symbols` — N restarts created N copies.
- **Fix:** Reuse existing snapshot if < 20h old and symbol count within ±10%.
  Added `UNIQUE(snapshot_id, symbol)` constraint via migration 013.
- **Files:** `backend/services/universe_store.py`
- **Test:** `test_universe_idempotent.py`

#### D-001 / D-002 — Dual `SymbolRegistry` Instances at Startup
- `main.py` and `stream_options_flow()` each called `init_registry()` + `build()`.
  Two full Tradier chain fetches, two `refresh_loop()` tasks, doubled cold-start time.
- **Fix:** `stream_options_flow()` accepts `registry=` parameter. When provided, polls
  `registry.is_ready()` instead of building. `refresh_loop()` only spawned by lifespan.
- **Files:** `backend/services/tradier_stream.py`, `backend/main.py`
- **Test:** `test_registry_shared_instance.py`

#### D-003 — Worker Count Hard-Coded to 32 (Half Universe Unstreamed)
- 32 workers hard-coded. ~31,920 OCC symbols at 500/worker requires 64. ~16,000 symbols
  were never streamed, silently.
- **Fix:** `worker_count = math.ceil(registry.size() / _CHUNK_SIZE)`. Logged at INFO.
- **Files:** `backend/services/stream_manager.py`
- **Test:** `test_stream_manager_dynamic_workers.py`

---

## [Registry Prewarm] — 2026-04-26

### Summary
Added `_registry_prewarm_loop()` to `main.py` — a background async task that pre-builds
the OCC symbol registry at 09:15 ET each trading day, before the 09:30 market open.
Eliminates cold-start latency on registry lookups at market open.

### Added
- `backend/main.py`: `_registry_prewarm_loop()` — infinite async loop, skips weekends,
  sleeps until 09:15 ET, calls `get_registry().build()`, survives exceptions non-fatally.
- `prewarm_task` wired into `lifespan()` startup and shutdown.
- `backend/tests/test_registry_prewarm.py` (5 tests): weekend skip, timing, build call,
  exception non-fatal.
- `test_main_app.py`: `test_lifespan_spawns_prewarm_task` added.

---

## [Feature 4A-OI] — 2026-04-25

### Summary
Average chain open interest is now computed during `symbol_registry.build()` and used as a
hard gate for T1/T2 tier classification. Zero OI blocks T1/T2 promotion regardless of volume
or price.

### Added
- `SymbolRegistry.get_oi_map()` — returns `{symbol: avg_oi}` after `build()`
- `main._stamp_oi(quotes, oi_map)` — stamps avg chain OI onto `SymbolQuote` objects in-place
- Two-pass tier assignment in `lifespan()` and `_universe_refresh_loop()`
- 28 new tests across `test_4a_oi_pipeline.py` and related files

### Changed
- `tier_engine._classify()`: OI grace path removed — all 3 conditions (vol + price + OI)
  required for T1/T2
- `universe_store._sync_upsert_symbol_quotes()`: `open_interest` field included in every upsert

---

## [Phase 5B — Regression Test Suite + CI Gate] — 2026-04-25

### Summary
Comprehensive automated regression suite (90%+ backend, 75%+ frontend). GitHub Actions CI
enforces hard coverage gates on every push and PR. Nothing deploys unless all tests pass.

### Added
- 13+ new backend test files covering auth, admin, config, demo, ingestion, ensemble,
  dedup, swarm, trade executor, simulation, smart signals, main app
- `backend/pytest.ini`, `backend/.coveragerc`, `backend/requirements-dev.txt`
- `frontend/jest.config.ts`, `frontend/__mocks__/styleMock.ts` + `fileMock.ts`
- `.github/workflows/backend.yml`: lint → regression (≥90%) → Railway deploys
- `.github/workflows/frontend.yml`: typecheck → regression (≥75%) → build → Vercel deploys
- PR coverage comment via `orgoro/coverage@v3.2`

---

## [Feature 4A] — 2026-04-25 (c.020 in FIXES)

### Summary
Dynamic tier classification. `tier_thresholds` DB table, editable via admin API.

### Added
- `services/tier_engine.py`: `assign_tiers()`, `_classify()`, `_fetch_thresholds()`,
  `invalidate_thresholds_cache()`, T1 admin whitelist (SPY, QQQ, AAPL, TSLA, NVDA, MSFT,
  AMZN, META, GOOGL, AMD, PLTR, COIN)
- `routers/admin.py`: GET/PATCH `/api/admin/tier-thresholds`, GET `/api/admin/tier-distribution`
- Migrations 010 (`tier` + `open_interest` + `average_volume` on `options_universe_symbols`),
  011 (`tier_thresholds` table)
- Tests: TE-01–22 in `test_4a_tier_engine.py`

---

## [Phase 3+5A — Composite Signal Engine + Swarm] — 2026-04-24

### Summary
Composite scoring pipeline (flow × 0.55 + backtest × 0.35 + vwpf × 0.10).
BUY/SELL/HOLD recommendation at composite ≥ 0.65. Swarm (12 Groq agents) available via
async path. `signal_history` and `flow_episodes` persistence added.

### Added
- `signals/composite_signal_engine.py`: `build_composite()`, `build_composite_async()`,
  `CompositeSignal` dataclass, `compute_flow_score()`, `volume_weighted_premium_factor()`
- `signals/backtest_validator.py`: `get_backtest_score()` historical win-rate lookup
- `signals/repetition_accumulator.py`: Gate 1 (count≥3 OR prem≥$10k), `get_alert_level()`,
  `RepetitionEpisode`, `is_accelerating`
- `services/swarm_engine.py`, `simulation/ensemble_runner.py`: 12-agent Groq swarm
- `services/flow_store.py`: `_bus_signal_listener()` → `flow_episodes` persistence
- `services/signal_store.py`: `signal_history` persistence, swarm fields
- `routers/history.py`: `GET /api/signals/history`
- Migration 003 (`signal_history`), 005 (schema repair), 006–009 (incremental patches)

---

## [C-019 — Dedup + Sweep Overhaul] — 2026-04-24

### Summary
5 bugs in Layer 4 dedup fixed: TTL too tight, time-bucket boundary gap, fill precision too
tight, dedup singleton never imported, sweep exchange field never passed.

### Changed
- `utils/dedup.py`: TTL 2s → 5s, sweep window 5s → 8s, eliminated `int(ts//2)` bucket,
  fill key `.2f → .1f`, added `dedup_stats()`, `get_exchange_count()`
- `tradier_stream.py`: imported `flow_dedup`, wired `is_duplicate()`, sweep upgrade,
  `_stats["deduped"]`
- `demo_engine.py`: `exch` primary field, inter-exchange delay 50–300ms

---

## [C-018 — Synthetic Quote Flag] — 2026-04-24

### Added
- `is_synthetic_quote: bool` field on `OptionsFlowEvent` + `flow_events` table
  (migration 009). Set `True` when `bid=0 AND ask=0`. Backtest queries should
  filter `WHERE is_synthetic_quote = false`.

---

## [C-017 — Duplicate flow_episodes Rows] — 2026-04-24

### Fixed
- `_bus_signal_listener` wrote `flow_episodes` on both `signal` and `composite_signal`
  events → 2× rows per episode. Now writes only on `composite_signal`.

---

## [C-016 — UnboundLocalError After 100-Row Buffer] — 2026-04-24

### Fixed
- Missing `global _flow_event_buffer` in `persist_flow_event()` caused
  `UnboundLocalError` on buffer flush. Every flow event write crashed after 100 rows.

---

## [C-015 — filter=trade → filter=timesale] — 2026-04-23

### Fixed
- Tradier stream `filter=trade` delivers equity ticks. Changed to `filter=timesale`
  for option contract ticks with OCC symbol and real NBBO.

---

## [C-013 — Stream Envelope Unwrap] — 2026-04-23

### Fixed
- `_process_trade()` now unwraps `raw[event_type]` inner dict before passing to parser.

---

## [C-010 — flow_episodes RLS 401 / Service Role Key] — 2026-04-23

### Fixed
- `flow_store.py` silent fallback to anon key (`SUPABASE_KEY`) when
  `SUPABASE_SERVICE_ROLE_KEY` absent. Anon key subject to RLS → 42501 on every insert.
  Removed fallback. `SUPABASE_SERVICE_ROLE_KEY` required; explicit startup warning if missing.

---

## [Phase 4] — 2026-04-10

### Added
- `routers/history.py`: `GET /api/signals/history`
- `services/signal_store.py`: async write loop + `start_signal_writer()`
- Migration 003: `signal_history` table
- Migration 005: schema repair for signal history
