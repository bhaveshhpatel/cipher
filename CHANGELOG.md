# Changelog

All notable changes to the Cipher backend are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

---

## [Feature 4A-OI] — 2026-04-25

### Summary
Average chain open interest is now computed during `symbol_registry.build()` and
used as a hard gate for T1/T2 tier classification. Symbols with zero or unavailable
OI can no longer be promoted to T1 or T2 regardless of volume or price.

### Added
- `SymbolRegistry.get_oi_map()` — returns a `{symbol: avg_oi}` copy after `build()`
- `main._stamp_oi(quotes, oi_map)` — stamps avg chain OI onto `SymbolQuote` objects in-place
- Two-pass tier assignment in `lifespan()`: preliminary pass (OI=0) → registry build →
  OI stamp → final OI-informed re-classification
- Same two-pass logic in `_universe_refresh_loop()` for background 24h refresh
- 28 new tests across 3 test files (see test map in `docs/features/4a-oi-pipeline.md`)

### Changed
- `tier_engine._classify()`: removed OI grace path — all 3 conditions (vol + price + OI)
  are now required for T1 and T2. `oi=0` always yields T3.
- `universe_store._sync_upsert_symbol_quotes()`: `open_interest` field now included in
  every upsert row (was missing before, column remained NULL post-010 migration)
- `migrations/README.md`: updated schema-state table, added backfill note and 4A-OI
  implementation summary

### No migration required
> Migration 010 (`010_add_tier_and_oi_to_universe.sql`) already added the `open_interest`
> column. The 4A-OI feature only changes application-layer code.

---

## [Feature 4A] — 2026-04-24

### Summary
Dynamic tier classification system. Symbols are classified into T1/T2/T3 based on
runtime thresholds stored in a `tier_thresholds` DB table, editable via the admin API.

### Added
- `services/tier_engine.py`: `assign_tiers()`, `_classify()`, `_fetch_thresholds()`,
  `invalidate_thresholds_cache()`
- `services/symbol_registry.py`: `_TierParams` dataclass, `ContractMeta.tier` field
- `universe_store.load_tier_map()` / `upsert_symbol_quotes(tier_map=...)`
- `routers/admin.py`: `GET /api/admin/tier-thresholds`, `PATCH /api/admin/tier-thresholds`,
  `GET /api/admin/tier-distribution`
- Migrations 010, 011, 012 (tier + OI columns, tier_thresholds table, RLS)
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
- `routers/health.py`: `GET /api/health/stream` — reports stream liveness, event rate,
  last event age, and per-symbol backpressure metrics
- `tests/test_health_stream.py`: 8 tests

---

## [C-018] — 2026-04-18

### Summary
Synthetic quote flag on flow events to distinguish real Tradier quotes from
back-filled values.

### Added
- `is_synthetic_quote` boolean column on `flow_events` (migration 009)
- Parser sets flag based on whether quote data was live or reconstructed

---

## [Phase 5B] — 2026-04-15

### Summary
Admin demo-mode toggle and admin router foundation.

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
