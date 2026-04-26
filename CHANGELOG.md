# Changelog

All notable changes to the Cipher backend are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

---

## [Phase 5B — Regression Test Suite + CI Gate] — 2026-04-25

### Summary
Comprehensive automated regression test suite built across 5 phases (P1–P5), covering
the full backend and frontend codebase. GitHub Actions CI now enforces a hard coverage
gate of 90% (backend) and 75% lines/functions (frontend) on every push and PR.
Nothing deploys to Railway or Vercel unless all regression tests pass.

### Added

#### Test Files (backend/tests/)
| File | Cases | Coverage Area |
|---|---|---|
| `test_auth_router.py` | ~15 | JWT register/login/me, expired token, missing header |
| `test_admin_router.py` | ~12 | Tier-threshold CRUD, admin role guard, 403 on non-admin |
| `test_config.py` | ~10 | Settings field types, defaults, SUPABASE_SERVICE_KEY presence |
| `test_demo_engine.py` | ~14 | Demo mode signals, deterministic mock, fallback path |
| `test_ingestion_config.py` | ~12 | Ingestion toggle, config validation, env overrides |
| `test_midcap_screener.py` | ~10 | Mid-cap filter thresholds, pass/fail boundaries |
| `test_ensemble_runner.py` | ~18 | Majority vote, tie-breaking, per-agent name field |
| `test_dedup.py` | ~22 | 2s TTL dedup, sweep detection (3+ exchanges/5s), singleton |
| `test_swarm_engine.py` | ~25 | All 12 agent roles, HOLD fallback (no API key), confidence bounds |
| `test_trade_executor.py` | ~14 | place_option_order (market/limit/error/network), get_positions |
| `test_simulation_router.py` | ~12 | n_agents/n_runs validation, 422 boundaries, flow_events serialised |
| `test_smart_signals_router.py` | ~16 | DB hit/miss, live/mock source, filters, _row_to_composite defaults |
| `test_main_app.py` | ~15 | /health, /root, all 7 routers mounted, _JsonFormatter, _stamp_oi |

#### CI Configuration
- `backend/pytest.ini` — test discovery, `asyncio_mode=auto`, `--cov-fail-under=90`, XML + HTML + terminal reports
- `backend/.coveragerc` — omit rules (tests/, migrations/, venv/), `exclude_lines` for pragmas/abstracts/TYPE_CHECKING
- `backend/requirements-dev.txt` — added `pytest-cov`, `fastapi[all]`
- `frontend/jest.config.ts` — explicit Jest config with `coverageThreshold` (global 75%, useAuth.ts 90%, useFlow.ts 85%)
- `frontend/__mocks__/styleMock.ts` + `fileMock.ts` — CSS/asset stubs
- `.github/workflows/backend.yml` — rebuilt: `lint` → `regression` (sequential); dummy env vars injected; pip cache; coverage XML artifact; PR coverage comment via `orgoro/coverage@v3.2`
- `.github/workflows/frontend.yml` — rebuilt: `typecheck` → `regression` → `build` → `deploy` (sequential); coverage artifact uploaded

### Changed
- `.github/workflows/backend.yml`: added `regression` job after `lint`
- `.github/workflows/frontend.yml`: `regression` job inserted before `build`; deploy now blocked until regression passes
- `backend/requirements-dev.txt`: added `pytest-cov` and `fastapi[all]`

### CI Gate Behaviour
| Trigger | Gate |
|---|---|
| Push to `main` (backend/**) | lint → regression (≥90% coverage) → Railway deploys |
| Push to `main` (frontend/**) | typecheck → regression (≥75% coverage) → build → Vercel deploys |
| Pull request (any) | Same gates + PR coverage comment posted automatically |

---

## [Feature 4A-OI] — 2026-04-25

### Summary
Average chain open interest is now computed during `symbol_registry.build()` and
used as a hard gate for T1/T2 tier classification. Symbols with zero or unavailable
OI can no longer be promoted to T1 or T2 regardless of volume or price.

### Added
- `SymbolRegistry.get_oi_map()` — returns a `{symbol: avg_oi}` copy after `build()`
- `main._stamp_oi(quotes, oi_map)` — stamps avg chain OI onto `SymbolQuote` objects in-place
- Two-pass tier assignment in `lifespan()`: preliminary pass (OI=0) → registry build → OI stamp → final re-classification
- Same two-pass logic in `_universe_refresh_loop()` for background 24h refresh
- 28 new tests across 3 test files

### Changed
- `tier_engine._classify()`: removed OI grace path — all 3 conditions (vol + price + OI) required for T1/T2
- `universe_store._sync_upsert_symbol_quotes()`: `open_interest` field now included in every upsert row

### No migration required
> Migration 010 already added the `open_interest` column.

---

## [Feature 4A] — 2026-04-24

### Summary
Dynamic tier classification system. Symbols are classified into T1/T2/T3 based on
runtime thresholds stored in a `tier_thresholds` DB table, editable via the admin API.

### Added
- `services/tier_engine.py`: `assign_tiers()`, `_classify()`, `_fetch_thresholds()`, `invalidate_thresholds_cache()`
- `services/symbol_registry.py`: `_TierParams` dataclass, `ContractMeta.tier` field
- `universe_store.load_tier_map()` / `upsert_symbol_quotes(tier_map=...)`
- `routers/admin.py`: `GET /api/admin/tier-thresholds`, `PATCH /api/admin/tier-thresholds`, `GET /api/admin/tier-distribution`
- Migrations 010, 011, 012
- Tests: TE-01–22 in `test_4a_tier_engine.py`, TR-01–05 in `test_universe_store.py`

---

## [B-019] — 2026-04-23

### Summary
Admin tier-thresholds cache visibility and `updated_at` trigger.

### Added
- `GET /api/admin/tier-thresholds` returns `cache_age_seconds` and `cache_ttl_seconds`
- Migration 012: `updated_at` auto-trigger on `tier_thresholds`
- RLS SELECT policy for `authenticated` role on `tier_thresholds`

---

## [B-008] — 2026-04-20

### Summary
Stream health monitoring endpoint.

### Added
- `routers/health.py`: `GET /api/health/stream`
- `tests/test_health_stream.py`: 8 tests

---

## [C-018] — 2026-04-18

### Summary
Synthetic quote flag on flow events.

### Added
- `is_synthetic_quote` boolean column on `flow_events` (migration 009)

---

## [Phase 5B Admin Foundation] — 2026-04-15

### Added
- `routers/admin.py` skeleton with demo toggle endpoint
- `core/auth.py` admin role guard

---

## [Phase 4] — 2026-04-10

### Summary
Signal history persistence and history endpoint.

### Added
- `routers/history.py`: `GET /api/signals/history`
- `services/signal_store.py`: async write loop + `start_signal_writer()`
- Migration 003: `signal_history` table
- Migration 005: schema repair for signal history
