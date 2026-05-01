# Cipher — APEX Sprint Plan

> **Single source of truth.** This file is updated after every PR merge and every panel review.
> Never let a story live only in conversation. If it was deliberated, it lives here.
>
> **Process rule:** Before every merge — Senior Architect + Principal Backend Engineer + Lead QA
> deliberate on the PR diff. Fixes go inline if small. Anything requiring a separate PR gets a
> numbered story added to this file and filed as a GitHub issue immediately.

---

## Legend

| Symbol | Meaning |
|---|---|
| ✅ | Merged to `main` |
| 🔴 | Hard gate — next story cannot start until this is closed |
| 🟡 | Must close before current phase merges |
| 🟢 | Queued — no current blocker |
| ⚪ | Low priority / quality / anytime |

---

## Phase 0 — Foundation (Complete)

| # | Story | Issue | Status |
|---|---|---|---|
| S0 | Swarm cleanup — deprecate `ensemble_runner.py`, wire `CompositeEngine` | PR #18 | ✅ |
| S1 | `ThresholdReconciler` — breach types, tier thresholds, reconcile loop | PR #19 | ✅ |

---

## Phase 1 — APEX Stream Wiring (Active)

### S2 — Tier Engine → Stream Worker → ThresholdReconciler

| # | Story | Issue | Status |
|---|---|---|---|
| S2 | Wire `tier_engine → stream_worker → ThresholdReconciler` hot path | PR #21 | ✅ |

**S2 post-merge panel findings (PR #21 review):**

| # | Story | Issue | Gate | Status |
|---|---|---|---|---|
| S2-POST-5 | `_refresh_tier_map` test coverage (5 tests) | [#26](https://github.com/bhaveshhpatel/cipher/issues/26) | 🔴 Pre-S3 | 🟢 Open |
| S2-POST-6 | `_process_tick` registry lookup path tests (3 tests) | [#27](https://github.com/bhaveshhpatel/cipher/issues/27) | 🔴 Pre-S3 | 🟢 Open |
| S2-POST-3 | Tier map refresh double-spawn race (`asyncio.Lock`) | [#24](https://github.com/bhaveshhpatel/cipher/issues/24) | 🟡 Pre-S3 merge | 🟢 Open |
| S2-POST-4 | `CancelledError` not re-raised in `StreamWorker.run()` | [#25](https://github.com/bhaveshhpatel/cipher/issues/25) | 🟡 Pre-S3 merge | 🟢 Open |
| S2-POST-2 | Flush loop task leak — add `.add_done_callback` | [#23](https://github.com/bhaveshhpatel/cipher/issues/23) | 🟡 Pre-S3 merge | 🟢 Open |
| S2-POST-1 | Hoist `get_registry` import out of hot path | [#22](https://github.com/bhaveshhpatel/cipher/issues/22) | ⚪ Anytime | 🟢 Open |
| S2-POST-7 | Fix misleading `test_flush_loop_creates_task_not_blocking` | [#28](https://github.com/bhaveshhpatel/cipher/issues/28) | ⚪ Anytime | 🟢 Open |

---

### S0.5 — `ensemble_runner.py` Deletion *(pre-S3 hard gate)*

| # | Story | Issue | Gate | Status |
|---|---|---|---|---|
| S0.5 | Delete deprecated `simulation/ensemble_runner.py` | [#20](https://github.com/bhaveshhpatel/cipher/issues/20) | 🔴 Pre-S3 | 🟢 Open |

---

### S2.5 — Supabase Migration: `order_side` + `strong_sentiment`

| # | Story | Issue | Gate | Status |
|---|---|---|---|---|
| S2.5 | Add `order_side` (BUY/SELL/UNKNOWN + index) + `strong_sentiment` (bool) to `flow_events` | [#29](https://github.com/bhaveshhpatel/cipher/issues/29) | 🔴 Pre-S3 | 🟢 Open |

---

## Phase 2 — S3: OI Enrichment (Blocked until gates above clear)

> **S3 cannot start until ALL 🔴 gates are merged:**
> - [#26](https://github.com/bhaveshhpatel/cipher/issues/26) `_refresh_tier_map` tests
> - [#27](https://github.com/bhaveshhpatel/cipher/issues/27) `_process_tick` registry tests
> - [#20](https://github.com/bhaveshhpatel/cipher/issues/20) `ensemble_runner.py` deletion
> - [#29](https://github.com/bhaveshhpatel/cipher/issues/29) S2.5 DB migration

| # | Story | Issue | Status |
|---|---|---|---|
| S3 | OI enrichment — wire `oi_delta` from `chain_store` into `_process_tick`; activate `OI_SPIKE` + `OI_COLLAPSE` breach types | TBD | ⏳ Blocked |

**S3 scope (to be detailed when gates clear):**
- `chain_store` lookup per symbol on each 5s flush window
- `oi_delta` = current OI − previous OI snapshot
- `OI_SPIKE` / `OI_COLLAPSE` breach types activated in `ThresholdReconciler`
- `order_side` derived from signal direction and written to `flow_events` (uses S2.5 schema)
- `strong_sentiment` set based on combined breach signal strength

---

## Phase 3 — Ingestion Rewrite (Parallel track, not blocked by APEX)

| # | Story | Issue | Status |
|---|---|---|---|
| ING-1 | Ingestion architecture rewrite + delta chain fetch optimization | [#6](https://github.com/bhaveshhpatel/cipher/issues/6) | 🟢 Open |

**ING-1 scope:**
- Eliminate duplicate Tradier quotes call on startup
- Fix `upsert_symbol_quotes()` not running on HIT path
- Fix tiers never written back to `options_universe_symbols`
- Add `stream_eligible=true` filter to `_load_symbols()`
- Delta chain fetch: cut HIT path rebuild from ~6 min → ~30s

---

## Separate Track — Signal Architecture

| # | Story | Issue | Status |
|---|---|---|---|
| C8 | Decouple persist tier from signal tier in `_process_trade` | [#2](https://github.com/bhaveshhpatel/cipher/issues/2) | 🟢 Open |

---

## Fully Ordered Build Sequence

This is the exact order stories should be executed. Do not skip, reorder, or start a story
until everything above it that shares the same gate is merged.

```
1.  ✅  S0       — Swarm cleanup (PR #18)
2.  ✅  S1       — ThresholdReconciler (PR #19)
3.  ✅  S2       — Stream worker tier wiring (PR #21)

── PRE-S3 GATE BLOCK (all must merge before S3 starts) ──────────────────
4.  🟢  #26      — _refresh_tier_map test coverage          [run as single PR with #27]
5.  🟢  #27      — _process_tick registry lookup tests      [same PR as #26]
6.  🟢  #29      — S2.5: order_side + strong_sentiment migration
7.  🟢  #20      — S0.5: Delete ensemble_runner.py
─────────────────────────────────────────────────────────────────────────

── BEFORE S3 MERGES (can work in parallel with S3 branch) ───────────────
8.  🟢  #24      — Tier map refresh race fix (asyncio.Lock)
9.  🟢  #25      — CancelledError re-raise in StreamWorker.run()
10. 🟢  #23      — Flush loop done callback
─────────────────────────────────────────────────────────────────────────

11. ⏳  S3       — OI enrichment (chain_store → oi_delta → breach types)

── PARALLEL / ANYTIME ───────────────────────────────────────────────────
12. 🟢  #6       — Ingestion rewrite + delta chain fetch
13. 🟢  #2       — C8: Decouple persist/signal tier
14. ⚪  #22      — Hoist get_registry import
15. ⚪  #28      — Fix misleading flush loop test
─────────────────────────────────────────────────────────────────────────
```

---

## How to Use This File

- **"What is next?"** → Find the first 🟢 item in the ordered sequence above.
- **"What is remaining?"** → Every row not marked ✅.
- **"What is blocking S3?"** → All 🔴 gates in the pre-S3 block (steps 4–7).
- **After every PR merge** → Update the relevant row to ✅, move any panel-flagged stories into
  the appropriate gate block, increment version at the bottom.
- **After every panel review** → Log all findings as issues, add them to this file before merging.

---

*Last updated: 2026-05-01 after PR #21 merge and panel review.*  
*Version: 1.0*
