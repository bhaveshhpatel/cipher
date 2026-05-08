# Cipher — Steamroom Signal Engine Re-Architecture Roadmap

> **Aggregation Branch:** `rearch/steamroom-signal-engine`  
> **Merge Target (when stable):** `main`  
> **Strategy:** Each story gets its own feature branch (`feat/rearch-NNN-*`), merged into the aggregation branch via PR. Never merge directly to `main` or `admin` until the full suite is stable and integration tests pass.

---

## Architecture Decision: Permissive Ingestion + Signal-Layer Conviction

The re-architecture is built on one core principle: **ingestion captures and tags; signals filter and score.**

| Layer | Responsibility | WSJ Steamroom Role |
|---|---|---|
| **Ingestion** | Capture every qualifying trade, apply sanity floors, tag every event with Steamroom dimension indicators | Minimum quality floor only (not conviction filter) |
| **Episode Accumulation** | Merge same-session prints per contract; aggregate dimension tags across constituent events | Repetition/clustering detection |
| **Signal Engine** | Apply all 5 Steamroom conviction dimensions as configurable gates; emit alert | Full Steamroom strategy, parameterized |
| **Admin UI** | Expose all ingestion floors and signal knobs as live-editable configuration | Research/tuning surface |

**Why this shape:** Keeping ingestion permissive (above sanity floors) means the `flow_events` + `flow_episodes` tables can be used for backtesting alternative strategies by varying signal-layer parameters without re-ingesting. The signal engine becomes a parameterized query over enriched episode data.

---

## Index Symbol Policy

**All index tickers are permanently excluded.** No index symbol (SPX, NDX, VIX, RUT, DJX, any `$`-prefixed ticker) will ever appear in `flow_events`, `flow_episodes`, or `signal_history`. Index options have fundamentally different settlement mechanics (cash-settled, AM/PM ambiguity, no share-equivalent underlying) that corrupt every flow quality metric. REARCH-001 is the mandatory first story.

---

## WSJ Steamroom: 5-Dimension Conviction Model

| Dimension | Ingestion Role | Signal Role | Admin Knob |
|---|---|---|---|
| **1. Premium Threshold** | Sanity floor (T1=$25K, T2=$15K, T3=$5K) | Alert tier (WATCH/NOTEWORTHY/BLOCK/GOLDEN) | `sig.golden_sweep_premium`, `sig.block_premium`, `sig.noteworthy_premium` |
| **2. Ask-Side Execution** | Tag `is_ask_side`, `bid_ask_class` | Gate: `ask_side_pct >= sig.ask_side_pct_floor` | `sig.require_ask_side`, `sig.ask_side_pct_floor` |
| **3. Vol > OI** | Capture `vol_oi_signal` from chain_store | Gate: `vol_oi_signal = true` OR `volume_oi_ratio > 1.0` | `sig.require_vol_gt_oi` |
| **4. DTE Quality** | Hard floor min_dte=1, ceiling max_dte=90 | Gate: DTE BETWEEN `sig.min_dte` AND `sig.max_dte` (default 5-60) | `sig.min_dte`, `sig.max_dte` |
| **5. Repetition/Clustering** | Episode merge (30-min window, ING-009) | Gate: `trade_count >= sig.min_trade_count` | `sig.min_trade_count` |

---

## Story Sequence and Status

| # | Story | GitHub Issue | Branch | Status | Deliberation | Dependencies |
|---|---|---|---|---|---|---|
| 1 | **REARCH-001** — Index Symbol Purge | [#102](https://github.com/bhaveshhpatel/cipher/issues/102) | `feat/rearch-001-index-purge` | 🔲 Not Started | SA · PBE · QA | None |
| 2 | **REARCH-002** — Ingestion Quality Floors | [#103](https://github.com/bhaveshhpatel/cipher/issues/103) | `feat/rearch-002-ingestion-floors` | 🔲 Not Started | SA · PBE · QA | REARCH-001 |
| 3 | **REARCH-003** — Flow Event Quality Tagging | [#104](https://github.com/bhaveshhpatel/cipher/issues/104) | `feat/rearch-003-event-quality-tags` | 🔲 Not Started | SA · PBE · QA | REARCH-002 |
| 4 | **REARCH-004** — Episode Quality Enrichment | [#105](https://github.com/bhaveshhpatel/cipher/issues/105) | `feat/rearch-004-episode-quality-enrichment` | 🔲 Not Started | SA · PBE · QA | REARCH-003 |
| 5 | **REARCH-005** — Signal Config Store | [#106](https://github.com/bhaveshhpatel/cipher/issues/106) | `feat/rearch-005-signal-config-store` | 🔲 Not Started | SA · PBE · QA | REARCH-002, REARCH-004 |
| 6 | **REARCH-006** — Signal Engine Rewrite | [#107](https://github.com/bhaveshhpatel/cipher/issues/107) | `feat/rearch-006-signal-engine-rewrite` | 🔲 Not Started | SA · PBE · QA | REARCH-003, REARCH-004, REARCH-005 |
| 7 | **REARCH-007** — Admin UI: Ingestion Panel | [#108](https://github.com/bhaveshhpatel/cipher/issues/108) | `feat/rearch-007-admin-ingestion-panel` | 🔲 Not Started | SA · PUX · PFE · PBF · QA | REARCH-002 |
| 8 | **REARCH-008** — Admin UI: Signal Strategy Panel | [#109](https://github.com/bhaveshhpatel/cipher/issues/109) | `feat/rearch-008-admin-signal-panel` | 🔲 Not Started | SA · PUX · PFE · PBF · QA | REARCH-005, REARCH-006 |
| 9 | **REARCH-009** — Integration Test Suite | [#110](https://github.com/bhaveshhpatel/cipher/issues/110) | `feat/rearch-009-integration-tests` | 🔲 Not Started | SA · PBE · QA | REARCH-001 through REARCH-008 |

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
REARCH-001 (Index Purge)
    └── REARCH-002 (Ingestion Floors)
            ├── REARCH-003 (Event Tagging)
            │       └── REARCH-004 (Episode Enrichment)
            │               └── REARCH-005 (Signal Config Store)
            │                       └── REARCH-006 (Signal Engine Rewrite)
            │                               └── REARCH-008 (Admin: Signal Panel)
            │                                       └── REARCH-009 (Integration Tests)
            └── REARCH-007 (Admin: Ingestion Panel)
```

---

## Branch Strategy

```
main
  └── rearch/steamroom-signal-engine  ← aggregation branch (all merges land here)
        ├── feat/rearch-001-index-purge
        ├── feat/rearch-002-ingestion-floors
        ├── feat/rearch-003-event-quality-tags
        ├── feat/rearch-004-episode-quality-enrichment
        ├── feat/rearch-005-signal-config-store
        ├── feat/rearch-006-signal-engine-rewrite
        ├── feat/rearch-007-admin-ingestion-panel
        ├── feat/rearch-008-admin-signal-panel
        └── feat/rearch-009-integration-tests
```

**Rules:**
- Feature branches are cut from `rearch/steamroom-signal-engine` (not `main`)
- PRs target `rearch/steamroom-signal-engine`
- No direct commits to `rearch/steamroom-signal-engine` except this roadmap doc and DB migration files that span multiple stories
- `rearch/steamroom-signal-engine` → `main` merge requires REARCH-009 integration tests passing green in CI
- `admin` branch is **never touched** by this re-architecture work

---

## Deliberation Protocol

Every story requires deliberation before work begins:

**Backend-only stories (3-way: SA · PBE · QA)**
- **SA** (Solution Architect): system design decisions, hot-reload safety, concurrency, migration strategy
- **PBE** (Principal Backend Engineer): implementation detail, DB schema, PostgREST contracts, performance
- **QA** (Quality Assurance): test matrix completeness, edge cases, regression risks

**Frontend stories (5-way: SA · PUX · PFE · PBF · QA)**
- **PUX** (Principal UX): interaction design, information hierarchy, edit/confirm flows
- **PFE** (Principal Frontend Engineer): component architecture, state management, WebSocket handling
- **PBF** (Principal Backend for Frontend): API contract, auth middleware, rate limiting

Deliberation results must be documented as comments on the GitHub issue before the feature branch is cut.

---

## DB Migrations Checklist

| Story | Migration File | Applied |
|---|---|---|
| REARCH-001 | `add_index_blacklist_constraint.sql` | ☐ |
| REARCH-001 | `delete_index_tickers_from_tracked_symbols.sql` | ☐ |
| REARCH-002 | `create_ingestion_config_table.sql` | ☐ |
| REARCH-002 | `seed_ingestion_config_defaults.sql` | ☐ |
| REARCH-003 | `add_event_quality_tag_columns_to_flow_events.sql` | ☐ |
| REARCH-003 | `backfill_event_quality_tags.sql` (batched) | ☐ |
| REARCH-004 | `add_episode_quality_aggregate_columns.sql` | ☐ |
| REARCH-005 | `create_signal_config_table.sql` | ☐ |
| REARCH-005 | `seed_signal_config_steamroom_defaults.sql` | ☐ |

---

## Merge-to-Main Readiness Checklist

- [ ] REARCH-001 through REARCH-008 all status ✅ (merged to aggregation branch)
- [ ] REARCH-009 integration test suite: all 19 scenarios green in CI
- [ ] No index ticker in `flow_events` (verified by query)
- [ ] Admin UI: ingestion panel and signal panel both render and save correctly
- [ ] Signal engine: at least 5 trading session dry-runs with side-by-side comparison to old pipeline
- [ ] `main` branch PR reviewed by SA + PBE + QA before merge
- [ ] Railway deployment: zero-downtime deploy confirmed (no restart-required config changes)
