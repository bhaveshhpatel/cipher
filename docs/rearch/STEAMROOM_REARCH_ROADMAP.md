# Cipher — Steamroom Signal Engine Re-Architecture Roadmap

> **Aggregation Branch:** `rearch/steamroom-signal-engine`  
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
| **1. Premium Threshold** | Sanity floor (T1=$25K, T2=$15K, T3=$5K) | Alert tier (WATCH/NOTEWORTHY/BLOCK/GOLDEN) | `sig.golden_sweep_premium`, `sig.block_premium`, `sig.noteworthy_premium` |
| **2. Ask-Side Execution** | Tag `is_ask_side`, `bid_ask_class` | Gate: `ask_side_pct >= sig.ask_side_pct_floor` | `sig.require_ask_side`, `sig.ask_side_pct_floor` |
| **3. Vol > OI** | Capture `vol_oi_signal` from chain_store | Gate: `vol_oi_signal = true` OR `volume_oi_ratio > 1.0` | `sig.require_vol_gt_oi` |
| **4. DTE Quality** | Hard floor min_dte=1, ceiling max_dte=90 | Gate: DTE BETWEEN `sig.min_dte` AND `sig.max_dte` (default 5-60) | `sig.min_dte`, `sig.max_dte` |
| **5. Repetition/Clustering** | Episode merge (30-min window, ING-009) | Gate: `trade_count >= sig.min_trade_count` | `sig.min_trade_count` |

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
| 1 | **REARCH-001** — Index Symbol Purge | [#102](https://github.com/bhaveshhpatel/cipher/issues/102) | `feat/rearch-001-index-purge` | 🔲 Not Started | SA · PBE · QA | None |
| 2 | **REARCH-002** — Ingestion Quality Floors | [#103](https://github.com/bhaveshhpatel/cipher/issues/103) | `feat/rearch-002-ingestion-floors` | 🔲 Not Started | SA · PBE · QA | REARCH-001 |
| 3 | **REARCH-003** — Flow Event Quality Tagging | [#104](https://github.com/bhaveshhpatel/cipher/issues/104) | `feat/rearch-003-event-quality-tags` | 🔲 Not Started | SA · PBE · QA | REARCH-002 |
| 4 | **REARCH-004** — Episode Quality Enrichment | [#105](https://github.com/bhaveshhpatel/cipher/issues/105) | `feat/rearch-004-episode-quality-enrichment` | 🔲 Not Started | SA · PBE · QA | REARCH-003 |
| 5 | **REARCH-005** — Signal Config Store | [#106](https://github.com/bhaveshhpatel/cipher/issues/106) | `feat/rearch-005-signal-config-store` | 🔲 Not Started | SA · PBE · QA | REARCH-002, REARCH-004 |
| 6 | **REARCH-006** — Signal Engine Rewrite | [#107](https://github.com/bhaveshhpatel/cipher/issues/107) | `feat/rearch-006-signal-engine-rewrite` | 🔲 Not Started | SA · PBE · QA | REARCH-003, REARCH-004, REARCH-005 |
| 7 | **REARCH-007** — Admin UI: Ingestion Config Panel | [#108](https://github.com/bhaveshhpatel/cipher/issues/108) | `feat/rearch-007-admin-ingestion-panel` | 🔲 Not Started | SA · PUX · PFE · PBF · QA | REARCH-002 |
| 8 | **REARCH-008** — Admin UI: Signal Strategy Panel | [#109](https://github.com/bhaveshhpatel/cipher/issues/109) | `feat/rearch-008-admin-signal-panel` | 🔲 Not Started | SA · PUX · PFE · PBF · QA | REARCH-005, REARCH-006 |
| 9 | **REARCH-009** — Integration Test Suite | [#110](https://github.com/bhaveshhpatel/cipher/issues/110) | `feat/rearch-009-integration-tests` | 🔲 Not Started | SA · PBE · QA | REARCH-001 through REARCH-008, REARCH-013, REARCH-014 |
| 10 | **REARCH-010** — DB Schema Purge | [#111](https://github.com/bhaveshhpatel/cipher/issues/111) | `feat/rearch-010-db-schema-purge` | ✅ Merged to aggregation branch | SA · PBE · QA | None (prerequisite for REARCH-003, REARCH-004, REARCH-006) |
| 11 | **REARCH-011** — Dashboard Frontend Overhaul | [#112](https://github.com/bhaveshhpatel/cipher/issues/112) | `feat/rearch-011-dashboard-overhaul` | 🔲 Not Started | SA · PUX · PFE · PBF · QA | REARCH-010, REARCH-006, REARCH-004 |
| 12 | **REARCH-012** — Admin Page Overhaul | [#113](https://github.com/bhaveshhpatel/cipher/issues/113) | `feat/rearch-012-admin-page-overhaul` | 🔲 Not Started | SA · PUX · PFE · PBF · QA | REARCH-010, REARCH-007, REARCH-008 |
| 13 | **REARCH-013** — S7: Tiered Swarm Engine + Circuit Breaker | [#115](https://github.com/bhaveshhpatel/cipher/issues/115) | `feat/rearch-013-tiered-swarm-circuit-breaker` | 🔲 Not Started | SA · PBE · QA | REARCH-010, REARCH-006, REARCH-004 |
| 14 | **REARCH-014** — S8: Backtest Engine from `flow_events` Replay | [#116](https://github.com/bhaveshhpatel/cipher/issues/116) | `feat/rearch-014-backtest-engine` | 🔲 Not Started | SA · PBF · QA | REARCH-006, REARCH-005, REARCH-004, REARCH-003 |

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
            │                               ├── REARCH-008 (Admin: Signal Panel)
            │                               │       └── REARCH-009 (Integration Tests) ◄─┐
            │                               ├── REARCH-011 (Dashboard Overhaul) ──────────┤
            │                               ├── REARCH-013 (Tiered Swarm + CB) ───────────┤
            │                               └── REARCH-014 (Backtest Engine) ─────────────┘
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

> **REARCH-010 note:** ✅ Merged 2026-05-09. Schema purge applied: 3 tables dropped (`backtest_results`, `gate_configs`, `gate_config_audit`), 11 pre-REARCH columns retired, CHECK constraints updated to REARCH vocabulary, 9 new Steamroom columns added. All downstream stories (REARCH-003, REARCH-004, REARCH-006, REARCH-011, REARCH-012, REARCH-013) are now unblocked.

> **REARCH-013 → REARCH-014 dependency note:** REARCH-014's backtest engine must know whether swarm is annotated on historical signals. The contract is: swarm is **always excluded from backtest replay scoring**. Deterministic Steamroom scoring only. REARCH-013 must be merged first so the `signal_history.detail['swarm']` shape is stable and REARCH-014 can explicitly exclude it.

---

## Story Summaries

### REARCH-010 — DB Schema Purge ([#111](https://github.com/bhaveshhpatel/cipher/issues/111))
✅ **Merged 2026-05-09.** Comprehensive schema cleanup against the live `cipher-database`. Three tables dropped (`backtest_results`, `gate_configs`, `gate_config_audit`), 11 columns retired across `flow_events` / `flow_episodes` / `signal_history` (all swarm columns, pre-REARCH tier/conviction columns), CHECK constraints updated to REARCH vocabulary (`WATCH/NOTEWORTHY/BLOCK/GOLDEN`, `BULLISH/BEARISH/NEUTRAL`), and 9 new Steamroom columns added to `flow_episodes` and `signal_history`. Full backfill strategy required for 28,504 existing `signal_history` rows before constraint swap.

> **Streaming boundary:** REARCH-010 only touches Supabase schema. No streaming files, no worker process, no Tradier client code.

### REARCH-011 — Dashboard Frontend Overhaul ([#112](https://github.com/bhaveshhpatel/cipher/issues/112))
Full audit and rebuild of the dashboard page against the REARCH data model. Audit-first: every current component is classified KEEP / REWORK / REMOVE. Key removals: swarm voting UI, `influence_tier` displays, raw event `conviction_score` gauge, `is_golden_sweep` badge. New components: Steamroom Score pip indicator (0–5), Ask-Side fill bar, Alert Level badge (4-tier), Vol>OI tag, DTE bucket label. New information hierarchy: Market Status → Golden/Block Banner → Live Signal Feed → Episode Activity Panel → Aggregate Stats Bar → Signal History Table.

> **Streaming boundary:** Dashboard reads from Supabase and existing WebSocket push endpoints only. No changes to the streaming worker, Tradier connection, or any upstream data pipeline.

### REARCH-012 — Admin Page Overhaul ([#113](https://github.com/bhaveshhpatel/cipher/issues/113))
Full audit and consolidation of the admin page. Removes Gate Config panel (backed by dropped `gate_configs` table), backtest results viewer, and any swarm monitoring UI. Integrates REARCH-007 and REARCH-008 panels into a clean 5-tab layout: Stream & Health / Ingestion Config / Signal Strategy / Universe Management / Demo Engine. Demo engine is explicitly preserved but must pass a 6-point audit checklist for retired field references. Persistent Activity Log footer drawer replaces any scattered log views. Swarm invocation surface (invoke button + tier selector + result display) is a contextual panel within the Episode detail view — not a standalone tab — added in this story.

> **Streaming boundary:** The Stream & Health tab displays read-only metrics from the existing streaming worker (uptime, event rate, reconnect count). It does not modify, restart, or reconfigure the streaming worker itself.

### REARCH-013 — S7: Tiered Swarm Engine + Circuit Breaker ([#115](https://github.com/bhaveshhpatel/cipher/issues/115))
Full rewrite of `backend/services/swarm_engine.py` and deprecation of `backend/simulation/ensemble_runner.py`. Replaces the always-on 12-agent flat swarm with an explicit-invocation-only `SwarmCoordinator` using a tiered model: FAST (3 agents, 2s timeout), STANDARD (6 agents, 5s timeout), DEEP (12 agents, 10s timeout). A sliding-window p95 `CircuitBreaker` guards the FAST tier — opens for 60s if p95 latency over last 20 calls exceeds 2000ms, returning `circuit_broken=True` immediately with no Groq calls. The swarm result is stored as a PATCH to `signal_history.detail['swarm']` JSONB after signal emission — it never gates or delays signal output. `build_composite()` (sync hot path) is completely unmodified. REARCH vocabulary throughout: `BULLISH/BEARISH/NEUTRAL` only. New `POST /admin/swarm/invoke` endpoint. 15-test matrix. `docs/SIGNAL_ENGINE.md` update required.

**Critical architectural invariants:**
- `build_composite()` sync path: **zero swarm involvement, must stay that way forever**
- `_process_trade()` hot path: never touches `SwarmCoordinator`
- Swarm result: supplemental annotation only, stored in `detail` JSONB, never a gate
- Circuit breaker: FAST tier only; STANDARD and DEEP have no breaker (human-invoked, latency-tolerant)
- Direction vocabulary: `BULLISH/BEARISH/NEUTRAL` — `BUY/SELL/HOLD` banned from all new code

> **Streaming boundary:** Swarm engine is called from `build_composite_async()` or `POST /admin/swarm/invoke` only. It has zero interaction with the streaming worker, Tradier connection, or OCC symbol lookup. The `_process_trade()` sync hot path is never modified.

**Key deliberations (3-way SA · PBE · QA):**
- **SA-1:** Circuit breaker state — in-process singleton vs. dependency-injected instance (DI is the correct answer; resolves both SA-1 and QA-5 simultaneously)
- **SA-2:** Does `build_composite_async` auto-invoke swarm on high-Steamroom episodes (`score >= 3`), or is it admin-panel-only? Must be documented in `SIGNAL_ENGINE.md` before callers are written
- **PBE-3:** `asyncio.gather(return_exceptions=True)` — add minimum quorum check (FAST: 2/3, STANDARD: 4/6, DEEP: 7/12) so all-agents-failed is distinguishable from genuine NEUTRAL
- **PBE-4:** Audit all `ensemble_runner` imports before deciding wrapper vs. immediate delete
- **QA-5:** Test circuit breaker via constructor parameterization (`window=5`, inject synthetic latencies) — no test-only branches needed if DI is adopted (SA-1/QA-5 are the same root decision)
- **QA-6:** Integration test for `POST /admin/swarm/invoke` must run against real Supabase test schema; hard ordering dependency on REARCH-010

### REARCH-014 — S8: Backtest Engine from `flow_events` Replay ([#116](https://github.com/bhaveshhpatel/cipher/issues/116))
Implements a fully in-memory, read-only, `dry_run=True`-hardcoded backtest engine in `backend/services/backtest_engine.py`. Loads `flow_events` for a target date, replays them chronologically through a fresh `RepetitionAccumulator` per symbol, scores each completed episode with `score_episode()` using a candidate `signal_config` (deep-merged config override, not full replacement), and returns a structured `BacktestResult` JSON. No writes to any table — `backtest_results` table was dropped in REARCH-010 and is not recreated. Swarm is explicitly excluded from replay (deterministic Steamroom scoring only; Groq calls cannot be replayed). `GET /admin/signal-config/backtest?date=YYYY-MM-DD&config_override={}` endpoint.

**`BacktestResult` shape:**
```json
{
  "date": "2026-05-08",
  "config_used": { ... },
  "config_source": "signal_config:42 + override",
  "events_replayed": 87432,
  "episodes_completed": 1847,
  "signals_emitted": 312,
  "signals_by_alert_level": { "WATCH": 198, "NOTEWORTHY": 89, "BLOCK": 21, "GOLDEN": 4 },
  "signals_per_hour": { "09": 0, "10": 47, "11": 38, ... },
  "top_episodes_blocked": [ ... ],
  "swarm_excluded": true,
  "replay_duration_ms": 1842
}
```

**Critical architectural invariants:**
- `dry_run=True` is hardcoded — the engine cannot emit real signals or write to DB
- Swarm is always excluded: `swarm_excluded: true` in every `BacktestResult`
- `config_override` uses deep merge (not full replacement) so a single threshold tweak doesn't wipe all other config fields
- Force-flush all open `RepetitionAccumulator` instances after event loop ends (captures late-day episodes that hadn't closed their merge window)
- 500,000 event hard guard: raise `BacktestTooLargeError` with a message pointing to date range narrowing

> **Streaming boundary:** Backtest engine reads exclusively from `flow_events` table in Supabase (already-captured historical data). It has no interaction with the streaming worker, Tradier connection, option chain cache, or any live data component. It is a pure read-from-DB → replay-in-memory → return-result pipeline.

**Key deliberations (3-way SA · PBF · QA):**
- **SA-1:** Bulk SELECT all events into memory vs. per-symbol sequential fetch — per-symbol is the correct long-term shape (2MB peak vs. 75MB), mitigated by `idx_flow_events_date_symbol` index; decide which ships in this story
- **SA-2:** Historical dates before REARCH-002 have looser ingestion quality — document in `config_source` field, accept the limitation for now; backtest-on-today is always clean
- **SA-3:** Synchronous endpoint vs. background task — ship synchronous first with 500k guard; promote to in-memory result store with TTL if Railway 30s timeout is hit in practice (likely by summer given T1 universe growth)
- **PBF-4:** Window-based flushing + mandatory force-flush of all open accumulators after event loop — non-negotiable; silent late-day episode drops skew `signals_per_hour` and under-count GOLDEN signals
- **PBF-5:** `config_override` deep merge must handle nested keys (e.g., `{"thresholds": {"golden_sweep_premium": 1200000}}`) without wiping sibling keys — use `deepmerge` or equivalent
- **PBF-6:** `top_episodes_blocked` requires a secondary pass over scored-but-not-emitted episodes; define "blocked" as `episode_steamroom_score >= 3 AND notional >= BLOCK threshold AND gate failed` — most actionable output for threshold tuning decisions
- **QA-7:** Test the force-flush explicitly: seed an event stream where the last episode's merge window is still open at end-of-loop; assert it appears in `BacktestResult.episodes_completed`
- **QA-8:** Test `config_override` deep merge: assert that passing `{"thresholds": {"golden_sweep_premium": 1500000}}` does not wipe `ask_side_pct_floor` or any other field
- **QA-9:** Test the 500k guard: assert `BacktestTooLargeError` raised before any replay logic runs when event count exceeds limit

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
        ├── feat/rearch-009-integration-tests
        ├── feat/rearch-010-db-schema-purge        ✅ merged
        ├── feat/rearch-011-dashboard-overhaul
        ├── feat/rearch-012-admin-page-overhaul
        ├── feat/rearch-013-tiered-swarm-circuit-breaker
        └── feat/rearch-014-backtest-engine
```

**Rules:**
- Feature branches are cut from `rearch/steamroom-signal-engine` (not `main`)
- PRs target `rearch/steamroom-signal-engine`
- No direct commits to `rearch/steamroom-signal-engine` except this roadmap doc and DB migration files that span multiple stories
- `rearch/steamroom-signal-engine` → `main` merge requires REARCH-009 integration tests passing green in CI
- `admin` branch is **never touched** by this re-architecture work
- **No PR modifying any streaming file** (`tradier_client.py`, `stream_worker.py`, `occ_parser.py`, `chain_cache.py`, `registry_sync.py`, or any file in the streaming worker process) will be accepted as part of this re-architecture

---

## Deliberation Protocol

Every story requires deliberation before work begins:

**Backend-only stories (3-way: SA · PBE · QA)**
- **SA** (Solution Architect): system design decisions, hot-reload safety, concurrency, migration strategy
- **PBE** (Principal Backend Engineer): implementation detail, DB schema, PostgREST contracts, performance
- **QA** (Quality Assurance): test matrix completeness, edge cases, regression risks

**Backend-only stories with API-contract focus (3-way: SA · PBF · QA)**
- **SA** (Solution Architect): system design, architectural invariants, scaling constraints
- **PBF** (Principal Backend for Frontend): API contract shape, endpoint design, response schema, rate limiting
- **QA** (Quality Assurance): test matrix, edge cases, integration test ordering dependencies

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
| REARCH-010 | `backfill_signal_history_alert_level.sql` | ☑ |
| REARCH-010 | `drop_tables_backtest_gate_configs.sql` | ☑ |
| REARCH-010 | `drop_columns_flow_events_pre_rearch.sql` | ☑ |
| REARCH-010 | `drop_columns_flow_episodes_pre_rearch.sql` | ☑ |
| REARCH-010 | `drop_columns_signal_history_swarm.sql` | ☑ |
| REARCH-010 | `alter_signal_history_constraints_rearch.sql` | ☑ |
| REARCH-010 | `add_steamroom_columns_flow_episodes.sql` | ☑ |
| REARCH-010 | `add_steamroom_snapshot_columns_signal_history.sql` | ☑ |
| REARCH-013 | `create_index_signal_history_detail_swarm.sql` (GIN index on `detail->'swarm'`) | ☐ |

> **REARCH-013 DB note:** No DDL changes to table structure — swarm result lives in the existing `signal_history.detail` JSONB column added in REARCH-010. The only migration is the GIN index to support admin queries over swarm-annotated signals.

> **REARCH-014 DB note:** No DDL changes. The dropped `backtest_results` table (REARCH-010) is not recreated. Backtest results are fully in-memory and returned in the API response only.

> **Streaming boundary:** No migration file in this list touches the streaming worker, Tradier client configuration, OCC symbol lookup tables, option chain cache, or registry sync tables. All migrations are purely within `flow_events`, `flow_episodes`, `signal_history`, and new config tables.

---

## Retired Vocabulary Reference

The following pre-REARCH terms must not appear in any new code, component, API response, or documentation:

| Retired Term | REARCH Replacement | Where It Appeared |
|---|---|---|
| `CONVICTION`, `WHALE`, `INSTITUTIONAL`, `LARGE`, `RETAIL` | `WATCH`, `NOTEWORTHY`, `BLOCK`, `GOLDEN` | `signal_history.alert_level`, UI badges |
| `BUY`, `SELL`, `HOLD` | `BULLISH`, `BEARISH`, `NEUTRAL` | `signal_history.direction`, swarm votes, UI labels |
| `conviction_score` (raw event) | Normalized 0-100 from REARCH-006 formula | `flow_events`, signal cards |
| `influence_tier` | `options_universe_symbols.tier` (T1/T2/T3) | `flow_events`, `signal_history`, UI |
| `is_golden_sweep` (boolean) | `alert_level = 'GOLDEN'` | `flow_events`, UI badges |
| `swarm_direction`, `swarm_confidence`, `swarm_agents`, `swarm_bull_votes`, `swarm_bear_votes`, `swarm_hold_votes` | `signal_history.detail['swarm']` JSONB object (REARCH-013) | `signal_history` dedicated columns (dropped), swarm monitoring UI |
| `volume_premium_factor` | `episode_steamroom_score` + conviction pipeline | `signal_history` |
| `backtest_results` (table) | `GET /admin/signal-config/backtest` in-memory replay (REARCH-014) | DB table, admin UI |
| `gate_configs` (table) | `ingestion_config` | DB table, admin Gate Config panel |
| `gate_config_audit` (table) | `admin_activity_log` | DB table |
| `seed_episode` (column) | Removed; no replacement | `flow_episodes` |
| `ensemble_runner.run_ensemble()` | `SwarmCoordinator.invoke()` with `SwarmTier.DEEP` (REARCH-013) | `backend/simulation/ensemble_runner.py` |

---

## Merge-to-Main Readiness Checklist

- [ ] REARCH-001 through REARCH-014 all status ✅ (merged to aggregation branch)
- [ ] REARCH-009 integration test suite: all scenarios green in CI (includes swarm and backtest integration tests)
- [x] REARCH-010 DB schema purge applied and verified with `get_advisors` scan
- [ ] No index ticker in `flow_events` (verified by query)
- [ ] No retired vocabulary in any API response, DB column, or UI component
- [ ] Admin UI: ingestion panel and signal panel both render and save correctly
- [ ] Dashboard: signal feed shows REARCH alert levels; no swarm UI present; Steamroom pip indicator renders correctly
- [ ] Signal engine: at least 5 trading session dry-runs with side-by-side comparison to old pipeline
- [ ] **Swarm:** `POST /admin/swarm/invoke` endpoint functional; `build_composite()` sync path verified unmodified; circuit breaker trips and resets correctly; `ensemble_runner.py` deprecated with `DeprecationWarning`
- [ ] **Backtest:** `GET /admin/signal-config/backtest` returns valid `BacktestResult`; `dry_run=True` hardcoded; no writes to any table; swarm excluded flag confirmed in all responses
- [ ] `docs/SIGNAL_ENGINE.md` updated: tiered swarm architecture, circuit breaker behavior table, backtest engine contract, explicit-invocation-only contract documented
- [ ] **Streaming boundary audit:** Confirm zero diff in any of the following files between `rearch/steamroom-signal-engine` and `main`: Tradier client, streaming worker, OCC parser, chain cache, registry sync workers. If any diff exists, it must be explained and approved as an intentional exception.
- [ ] `main` branch PR reviewed by SA + PBE + QA before merge
- [ ] Railway deployment: zero-downtime deploy confirmed (no restart-required config changes)
