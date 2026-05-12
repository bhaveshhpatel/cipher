# Cipher — Steamroom Signal Engine Re-Architecture Roadmap

> **Aggregation Branch:** `cipher-rearch`  
> **Merge Target (when stable):** `main`  
> **Strategy:** Each story gets its own feature branch (`feat/rearch-NNN-*`), merged into the aggregation branch via PR. Never merge directly to `main` or `admin` until the full suite is stable and integration tests pass.

---

## ⛔ Streaming Boundary — Frozen, Out of Scope for All Re-Architecture Work

**The re-architecture scope begins exactly here:**

```
[Tradier WebSocket] ──► [Streaming Worker] ──► _process_trade(raw_event)
                                                        │
                                              ══════════╪══════════════
                                              REARCH     │  STARTS HERE
                                              ══════════╪══════════════
                                                        │
                                              flow_events write
                                              episode accumulation
                                              signal engine
                                              swarm annotation
                                              signal_history write
```

Everything **above** the `_process_trade()` entry point is **permanently frozen and excluded from all re-architecture work.** These systems are working correctly and must not be touched:

| Frozen Component | What It Does | Why It's Frozen |
|---|---|---|
| **Tradier WebSocket connection** | Establishes and maintains the real-time options flow stream | Working correctly. Any disruption breaks live data ingest entirely. |
| **Streaming worker / event loop** | Receives raw Tradier trade messages, dispatches to `_process_trade()` | Working correctly. Reconnect logic, heartbeat, and backpressure handling are stable. |
| **OCC symbol lookup** | Parses OCC option symbols into structured fields (underlying, strike, expiry, type) | Working correctly. No changes to symbol parsing or OCC format handling. |
| **Option chain cache** | Caches chain-level data (open interest, bid/ask) used for Vol>OI and bid/ask classification | Working correctly. Cache generation, TTL, and invalidation are stable. |
| **Registry sync / universe workers** | Syncs `options_universe_symbols` (T1/T2/T3 tiers) from external sources | Working correctly. Worker process, scheduling, and sync contract are stable. |
| **`_process_trade()` signature** | Entry point called by the streaming worker with a raw Tradier event | Signature and call contract are frozen. Internal logic downstream of this call is in scope. |

### What "In Scope" Means

The re-architecture touches **only** the logic that runs after `_process_trade()` has received a raw event:

1. **Quality filtering** — which events clear the ingestion floor and get written to `flow_events`
2. **Event tagging** — Steamroom dimension tags (`is_ask_side`, `vol_oi_signal`, `bid_ask_class`, etc.) computed and stored on `flow_events`
3. **Episode accumulation** — how events are merged into `flow_episodes` (merge window, repetition detection)
4. **Signal engine** — what gates an episode must pass before a `signal_history` row is emitted
5. **Swarm annotation** — explicit-invocation-only enrichment on already-emitted signals
6. **DB schema** — columns, tables, and constraints in `flow_events`, `flow_episodes`, `signal_history`
7. **Admin UI** — ingestion config, signal strategy, episode management panels
8. **Dashboard UI** — signal feed, episode panel, Steamroom scoring display

### Enforcement Rules

- **No PR touching streaming files will be approved** as part of this re-architecture. If a streaming file change is needed for an independent reason, it must go through a separate PR on `main` with its own review cycle, completely decoupled from this branch.
- **REARCH-009 integration tests must never mock or stub the streaming worker.** Tests drive the pipeline from `_process_trade()` downward, using synthetic raw event payloads that match the exact Tradier format — not from a mock WebSocket.
- **If any story's implementation requires a change upstream of `_process_trade()`**, that is a design error. The story must be redesigned so that all changes land downstream.

---

## Architecture Decision: Permissive Ingestion + Signal-Layer Conviction

The re-architecture is built on one core principle: **ingestion captures and tags; signals filter and score.**

| Layer | Responsibility | WSJ Steamroom Role |
|---|---|---|
| **Ingestion** | Capture every qualifying trade, apply sanity floors, tag every event with Steamroom dimension indicators | Minimum quality floor only (not conviction filter) |
| **Episode Accumulation** | Merge same-session prints per contract; aggregate dimension tags across constituent events | Repetition/clustering detection |
| **Signal Engine** | Apply all 5 Steamroom conviction dimensions as configurable gates; emit alert | Full Steamroom strategy, parameterized |
| **Swarm Engine** | Optional, explicit-invocation-only supplemental annotation; never gates or delays signal emission | Post-hoc BULLISH/BEARISH/NEUTRAL enrichment on already-emitted signals |
| **Backtest Engine** | In-memory deterministic replay of `flow_events` for a given date using candidate `signal_config`; no writes | Threshold tuning and strategy validation surface |
| **Admin UI** | Expose all ingestion floors, signal knobs, swarm invocation, and backtest as live-editable configuration | Research/tuning surface |

**Why this shape:** Keeping ingestion permissive (above sanity floors) means the `flow_events` + `flow_episodes` tables can be used for backtesting alternative strategies by varying signal-layer parameters without re-ingesting. The signal engine becomes a parameterized query over enriched episode data. The swarm engine is a separate, explicit-invocation-only enrichment path that never touches the hot path.

> **Note on "ingestion" language:** Throughout this document, "ingestion" refers exclusively to the logic inside `_process_trade()` and the downstream pipeline — quality filtering, DB writes, episode accumulation. It does **not** refer to the Tradier streaming layer, which is frozen per the boundary above.

---

## Index Symbol Policy

**All index tickers are permanently excluded.** No index symbol (SPX, NDX, VIX, RUT, DJX, any `$`-prefixed ticker) will ever appear in `flow_events`, `flow_episodes`, or `signal_history`. Index options have fundamentally different settlement mechanics (cash-settled, AM/PM ambiguity, no share-equivalent underlying) that corrupt every flow quality metric. REARCH-001 is the mandatory first story.

> **Boundary note:** Index exclusion is enforced inside `_process_trade()` as an early-return check — not at the streaming layer. The streaming worker continues to receive and dispatch all Tradier events; the filter lives in the application layer where it belongs.

---

## WSJ Steamroom: 5-Dimension Conviction Model

| Dimension | Ingestion Role | Signal Role | Admin Knob |
|---|---|---|---|
| **1. Premium Threshold** | Sanity floor (T1=$25K, T2=$15K, T3=$5K) | Alert tier (WATCH/NOTEWORTHY/BLOCK/GOLDEN) via tier-multiplied thresholds | `sig.golden_sweep_premium`, `sig.block_premium`, `sig.noteworthy_premium` + `sig.*_t2_mult` / `sig.*_t3_mult` |
| **2. Ask-Side Execution** | Tag `is_ask_side`, `bid_ask_class` | Gate: `ask_side_pct >= sig.ask_side_pct_floor` | `sig.require_ask_side`, `sig.ask_side_pct_floor` |
| **3. Vol > OI** | Capture `vol_oi_signal` from chain_store | Gate: `vol_oi_signal = true` OR `volume_oi_ratio > 1.0` | `sig.require_vol_gt_oi` |
| **4. DTE Quality** | Hard floor min_dte=1, ceiling max_dte=90 | Gate: DTE BETWEEN `sig.min_dte` AND `sig.max_dte` (default 5-60) | `sig.min_dte`, `sig.max_dte` |
| **5. Repetition/Clustering** | Episode merge (30-min window, ING-009) | Gate: `trade_count >= sig.min_trade_count` | `sig.min_trade_count` |

### Dimension-1 Effective Threshold Matrix (live defaults)

Tier multipliers scale the base dollar threshold down for smaller-cap names so that a $200K flow on a $2 stock is treated as BLOCK-level rather than WATCH.

| Alert Level | Tier-1 base | Tier-2 (×0.5) | Tier-3 (×0.2) |
|---|---|---|---|
| **GOLDEN** | $1,000,000 | $500,000 | $200,000 |
| **BLOCK** | $500,000 | $250,000 | $100,000 |
| **NOTEWORTHY** | $50,000 | $25,000 | $10,000 |

> **Future enhancement (REARCH-015):** Dollar thresholds will be replaced by an ADV-normalized gate (`sig.normalized_premium_floor`, default 2.0×). Dollar thresholds are retained as soft label classifiers. Depends on `adv_cache` table + nightly refresh job. **Post-launch, does not block REARCH-006.**

---

## Alert Level Vocabulary (REARCH)

All pre-REARCH alert level values (`CONVICTION`, `WHALE`, `INSTITUTIONAL`, `LARGE`, `RETAIL`) are retired. The canonical vocabulary across all tables, APIs, and UI is:

| Level | Notional Threshold | Description |
|---|---|---|
| `WATCH` | < $50K | Minimum qualifying premium |
| `NOTEWORTHY` | $50K – $500K | Institutional-scale flow |
| `BLOCK` | $500K – $1M | Block-trade-level flow |
| `GOLDEN` | ≥ $1M | Golden sweep — all 5 Steamroom dimensions pass at max tier |

All direction values use `BULLISH / BEARISH / NEUTRAL` (replaces `BUY / SELL / HOLD`).

---

## Story Sequence and Status

| # | Story | GitHub Issue | Branch | Status | Deliberation | Dependencies |
|---|---|---|---|---|---|---|
| 1 | **REARCH-001** — Index Symbol Purge | [#102](https://github.com/bhaveshhpatel/cipher/issues/102) | `feat/rearch-001-index-purge` | ✅ Merged to aggregation branch | SA · PBE · QA | None |
| 2 | **REARCH-002** — Ingestion Quality Floors | [#103](https://github.com/bhaveshhpatel/cipher/issues/103) | `feat/rearch-002-ingestion-floors` | ✅ Merged to aggregation branch | SA · PBE · QA | REARCH-001 |
| 3 | **REARCH-003** — Flow Event Quality Tagging | [#104](https://github.com/bhaveshhpatel/cipher/issues/104) | `feat/rearch-003-event-quality-tags` | ✅ Merged to aggregation branch | SA · PBE · QA | REARCH-002 |
| 4 | **REARCH-004** — Episode Quality Enrichment | [#105](https://github.com/bhaveshhpatel/cipher/issues/105) | `feat/rearch-004-episode-quality-enrichment` | ✅ Merged to aggregation branch | SA · PBE · QA | REARCH-003 |
| 5 | **REARCH-005** — Signal Config Store | [#106](https://github.com/bhaveshhpatel/cipher/issues/106) | `feat/rearch-005-signal-config-store` | ✅ Merged to aggregation branch | SA · PBE · QA | REARCH-002, REARCH-004 |
| 6 | **REARCH-006** — Signal Engine Rewrite | [#107](https://github.com/bhaveshhpatel/cipher/issues/107) | `feat/rearch-006-signal-engine-rewrite` | 🔲 Not Started | SA · PBE · QA | REARCH-003, REARCH-004, REARCH-005 |
| 7 | **REARCH-007** — Admin UI: Ingestion Config Panel | [#108](https://github.com/bhaveshhpatel/cipher/issues/108) | `feat/rearch-007-admin-ingestion-panel` | 🔲 Not Started | SA · PUX · PFE · PBF · QA | REARCH-002 |
| 8 | **REARCH-008** — Admin UI: Signal Strategy Panel | [#109](https://github.com/bhaveshhpatel/cipher/issues/109) | `feat/rearch-008-admin-signal-panel` | 🔲 Not Started | SA · PUX · PFE · PBF · QA | REARCH-005, REARCH-006 |
| 9 | **REARCH-009** — Integration Test Suite | [#110](https://github.com/bhaveshhpatel/cipher/issues/110) | `feat/rearch-009-integration-tests` | 🔲 Not Started | SA · PBE · QA | REARCH-001 through REARCH-008, REARCH-013, REARCH-014 |
| 10 | **REARCH-010** — DB Schema Purge | [#111](https://github.com/bhaveshhpatel/cipher/issues/111) | `feat/rearch-010-db-schema-purge` | ✅ Merged to aggregation branch | SA · PBE · QA | None (prerequisite for REARCH-003, REARCH-004, REARCH-006) |
| 11 | **REARCH-011** — Dashboard Frontend Overhaul | [#112](https://github.com/bhaveshhpatel/cipher/issues/112) | `feat/rearch-011-dashboard-overhaul` | 🔲 Not Started | SA · PUX · PFE · PBF · QA | REARCH-010, REARCH-006, REARCH-004 |
| 12 | **REARCH-012** — Admin Page Overhaul | [#113](https://github.com/bhaveshhpatel/cipher/issues/113) | `feat/rearch-012-admin-page-overhaul` | 🔲 Not Started | SA · PUX · PFE · PBF · QA | REARCH-010, REARCH-007, REARCH-008 |
| 13 | **REARCH-013** — S7: Tiered Swarm Engine + Circuit Breaker | [#115](https://github.com/bhaveshhpatel/cipher/issues/115) | `feat/rearch-013-tiered-swarm-circuit-breaker` | 🔲 Not Started | SA · PBE · QA | REARCH-010, REARCH-006, REARCH-004 |
| 14 | **REARCH-014** — S8: Backtest Engine from `flow_events` Replay | [#116](https://github.com/bhaveshhpatel/cipher/issues/116) | `feat/rearch-014-backtest-engine` | 🔲 Not Started | SA · PBF · QA | REARCH-006, REARCH-005, REARCH-004, REARCH-003 |
| 15 | **REARCH-015** — ADV-Normalized Premium Gate | [#129](https://github.com/bhaveshhpatel/cipher/issues/129) | `feat/rearch-015-adv-normalized-premium` | 🔲 Not Started (post-launch) | SA · PBE · QA | REARCH-006 (must be live first) |

### Status Legend
| Icon | Meaning |
|---|---|
| 🔲 | Not Started |
| 🔵 | In Deliberation |
| 🟡 | In Development |
| 🟠 | In Review (PR open) |
| ✅ | Merged to aggregation branch |
| 🚀 | Merged to main |

---

## Dependency Graph

```
REARCH-001 (Index Purge) ✅
    └── REARCH-002 (Ingestion Floors) ✅
            ├── REARCH-003 (Event Tagging) ✅
            │       └── REARCH-004 (Episode Enrichment) ✅
            │               └── REARCH-005 (Signal Config Store) ✅
            │                       └── REARCH-006 (Signal Engine Rewrite)
            │                               ├── REARCH-008 (Admin: Signal Panel)
            │                               │       └── REARCH-009 (Integration Tests) ◄─┐
            │                               ├── REARCH-011 (Dashboard Overhaul) ──────────┤
            │                               ├── REARCH-013 (Tiered Swarm + CB) ───────────┤
            │                               ├── REARCH-014 (Backtest Engine) ─────────────┘
            │                               └── REARCH-015 (ADV-Normalized Gate) ← POST-LAUNCH
            └── REARCH-007 (Admin: Ingestion Panel)
                        └── REARCH-012 (Admin Page Overhaul)

REARCH-010 (DB Schema Purge) ✅ Applied — unblocks REARCH-003, REARCH-004,
    ├── REARCH-011 (Dashboard Overhaul)      REARCH-006 column reads and all UI work
    ├── REARCH-012 (Admin Page Overhaul)
    └── REARCH-013 (Tiered Swarm)           ← signal_history.detail JSONB is the swarm write target

REARCH-013 (Tiered Swarm) ← blocks REARCH-014
    └── defines swarm annotation contract that backtest engine must know to exclude from replay

[Tradier WebSocket → Streaming Worker → _process_trade()]
    ✗ NOT IN THIS GRAPH — frozen, out of scope, no stories touch these components
```

---

## Merge Notes

> **REARCH-001 note:** ✅ Merged 2026-05-10 via [PR #121](https://github.com/bhaveshhpatel/cipher/pull/121). Index symbol purge complete: `validate_symbol()` now rejects all 11 index tickers + `$`-prefixed symbols via `is_index_symbol()` frozenset gate. `chk_options_universe_symbols_no_index` CHECK constraint applied to Supabase production. 33 unit tests passing. Follow-on items tracked: `apply_config()` admin-path bypass verification (resolved in REARCH-002), `flow_events`/`signal_history` index constraint coverage (candidate for REARCH-003 scope).

> **REARCH-002 note:** ✅ Merged 2026-05-11 via [PR #126](https://github.com/bhaveshhpatel/cipher/pull/126). `IngestionProcessor` 4-gate pipeline shipped: G1 DTE floor (min_dte=1) → G2 DTE ceiling (max_dte=90) → G3 tier-aware premium floor (T1=$25K / T2=$15K / T3=$5K) → G4 OI floor (min_oi=50). DB-backed `ingestion_config` table with 30s TTL cache and GIL-safe atomic reference swap on hot path. `GET /admin/ingestion-config` and `PATCH /admin/ingestion-config` endpoints live. `apply_config()` now routes through `validate_symbol()` (closes REARCH-001 follow-on). 16 boundary-value tests passing. Migrations `create_ingestion_config_table.sql` and `seed_ingestion_config_defaults.sql` applied to Supabase production prior to merge. Unblocks: REARCH-003, REARCH-005, REARCH-007.

> **REARCH-003 note:** ✅ Merged 2026-05-11 via [PR #127](https://github.com/bhaveshhpatel/cipher/pull/127). Three pure helpers shipped in `flow_store.py`: `classify_bid_ask()`, `compute_vol_oi_signal()`, `_compute_normalized_premium()` — all wired into `persist_flow_event()`. Five new quality tag columns on `flow_events`: `is_ask_side` (BOOLEAN NOT NULL), `bid_ask_class` (TEXT), `vol_oi_signal` (BOOLEAN DEFAULT NULL — cache-miss sentinel), `normalized_premium` (NUMERIC(18,4)), `normalized_oi` (NUMERIC(18,6)). Two partial indexes: `idx_flow_events_vol_oi_high`, `idx_flow_events_ask_side`. Blocker fixes: SA-1 (`vol_oi_signal` TEXT→BOOLEAN), SA-3 (backfill predicate `IS NULL` guards), bonus `normalized_oi` column missing from migration 026. 12 unit + integration tests (E1–E12) passing. Streaming boundary untouched. Unblocks: REARCH-004 (Signal Engine S1–S4 filters), REARCH-006 (Apex L1–L4 normalized column reads).

> **REARCH-004 note:** ✅ Merged 2026-05-11 via [PR #128](https://github.com/bhaveshhpatel/cipher/pull/128). Four aggregate quality columns added to `flow_episodes`: `ask_side_count` (INTEGER NOT NULL), `ask_side_pct` (NUMERIC(5,4)), `dte_bucket` (TEXT), `notional_tier` (TEXT). SA-3 seed-only contract enforced on both PATCH code branches (in-flight and DB-lookup) — `dte_bucket` and `notional_tier` are locked at episode open and never overwritten. PBE-1 NULL COALESCE handled for pre-REARCH rows (`ask_side_count=NULL → COALESCE(NULL,0)` before increment). QA-4 stale-cache guard added (E-9/E-10): `_load_episode_from_db()` path now refreshes `notional_tier` and `dte_bucket` from DB on cache miss rather than inheriting stale in-memory values. Migrations `027_add_episode_quality_columns.sql` and `028_backfill_episode_quality_columns.sql` applied to Supabase production. 14 unit + integration tests (E-1 through E-14) passing. Unblocks: REARCH-005 (notional_tier read for signal config store), REARCH-006 (all 4 columns are Signal Engine Dimension gate inputs).

> **REARCH-005 note:** ✅ Merged to aggregation branch 2026-05-11. `signal_config` DB-backed store shipped: `signal_config_store.py` with typed `get_int()`, `get_float()`, `get_bool()` accessors, 30s TTL cache, and `get_effective_premium_threshold(alert_level_key, notional_tier)` — the single Dimension-1 call point for REARCH-006. **16 config rows live in Supabase production** (10 base Steamroom knobs + 6 tier multipliers). Base knobs: `sig.golden_sweep_premium=$1M`, `sig.block_premium=$500K`, `sig.noteworthy_premium=$50K`, `sig.require_ask_side=true`, `sig.ask_side_pct_floor=0.6`, `sig.require_vol_gt_oi=true`, `sig.min_dte=5`, `sig.max_dte=60`, `sig.min_trade_count=2`, `sig.steamroom_score_floor=3`. Tier multipliers: `sig.*_t2_mult=0.5`, `sig.*_t3_mult=0.2` for GOLDEN, BLOCK, NOTEWORTHY. PBE extension: `_TIER_MULT_KEYS` dict centralises multiplier key naming; unrecognised tier falls back to base with WARNING log (never returns zero). Migrations `029_create_signal_config_table.sql`, `030_seed_signal_config_steamroom_defaults.sql`, `031_seed_signal_config_tier_multipliers.sql` applied. **Future REARCH-015** will add `sig.normalized_premium_floor` to demote dollar gates to soft labels in favour of ADV-normalized gate — post-launch, does not block this story or REARCH-006. Unblocks: REARCH-006 (full Signal Engine Rewrite).

> **REARCH-015 note:** 🔲 Not Started (post-launch). Tracked in [#129](https://github.com/bhaveshhpatel/cipher/issues/129). Adds `adv_cache` table (20-day rolling options premium ADV per ticker, computed from `flow_events` history — no external data provider needed), nightly refresh job, `sig.normalized_premium_floor` config key (default 2.0×), and changes Signal Engine Dimension-1 from a dollar gate to an ADV-normalized gate. Dollar thresholds (GOLDEN/BLOCK/NOTEWORTHY) are demoted to soft label classifiers only. Staleness fallback policy (F1: fall back to tier-multiplier dollar gate on cache miss) requires SA · PBE · QA deliberation before branch is cut. **Blocked on REARCH-006 merged and live.**
