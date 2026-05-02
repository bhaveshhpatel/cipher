# Cipher — APEX Sprint Execution Tracker

> ## ⚠️ How to read this file
>
> **Story definitions live in:** [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md)
> That file is the canonical spec — acceptance criteria, scope, test requirements, architectural
> deliberation notes. Read it before starting any story.
>
> **This file tracks:** execution state — what is merged, what is open, what is blocked, exact
> build order, gate status, and dynamically added post-merge stories from panel reviews.
>
> **Rule:** Before every PR merge — Senior Architect + Principal Backend Engineer + Lead QA
> deliberate on the diff. Small fixes go inline on the PR. Anything needing a separate PR gets a
> numbered story added here AND filed as a GitHub issue. No story lives only in conversation.
> **Work must never be pushed directly to `main`. Always branch + PR.**
>
> **When answering "What is next?" or "What is remaining?"** — read this file AND
> `docs/cipher_apex_story_and_sprint_plan.md` together. Issues alone are insufficient.

---

## Legend

| Symbol | Meaning |
|---|---|
| ✅ | Merged to `main` |
| 🔴 | Hard gate — next story cannot start until this is closed |
| 🟡 | Must close before current phase merges |
| 🟢 | Queued — no current blocker |
| ⏳ | Blocked — waiting on gates above |
| ⚪ | Low priority / quality / anytime |

---

## Sprint 1 — Foundation + Parser + Stream Wiring

### Completed

| Story | Description | PR | Status |
|---|---|---|---|
| S0 | Swarm cleanup — deprecate `ensemble_runner.py`, wire `CompositeEngine` | [#18](https://github.com/bhaveshhpatel/cipher/pull/18) | ✅ |
| S1 | Alert level threshold reconciliation + emit-cache flush | [#19](https://github.com/bhaveshhpatel/cipher/pull/19) | ✅ |
| S2 | Parser + detector layer fixes — direction inference, `order_side_classifier`, tier wiring into stream worker hot path | [#21](https://github.com/bhaveshhpatel/cipher/pull/21) | ✅ |
| S2-POST-5 | `_refresh_tier_map` test coverage — 5 tests | [#26](https://github.com/bhaveshhpatel/cipher/issues/26) | ✅ |
| S2-POST-6 | `_process_tick` registry avg_volume lookup path — 3 tests | [#27](https://github.com/bhaveshhpatel/cipher/issues/27) | ✅ |
| S2-POST-inline | Inline fixes 2, 3, 6 — patch comment, dead params, `is_ready` mock | [#36](https://github.com/bhaveshhpatel/cipher/pull/36) | ✅ |
| S2-POST-8 | Test isolation — `reset_tier_map_globals` autouse fixture; saves/restores `_tier_map_cache`, `_tier_map_ts`, `_tier_map_refresh_task` | [#37](https://github.com/bhaveshhpatel/cipher/pull/37) | ✅ |
| S2.5 | DB migration: `order_side` (BUY/SELL/UNKNOWN + index) + `strong_sentiment` (bool) + `execution_mechanic` (6-value enum) on `flow_events` | [#38](https://github.com/bhaveshhpatel/cipher/pull/38) | ✅ |
| S0.5 | Delete deprecated `simulation/ensemble_runner.py` | [#39](https://github.com/bhaveshhpatel/cipher/pull/39) | ✅ |
| S2-POST-2 | Flush loop done callback — `.add_done_callback` + `_on_flush_done` error logger | [#23](https://github.com/bhaveshhpatel/cipher/issues/23) | ✅ |
| S2-POST-3 | Tier map refresh double-spawn race — `_tier_map_refresh_in_progress` flag | [#24](https://github.com/bhaveshhpatel/cipher/issues/24) | ✅ |
| S2-POST-4 | `CancelledError` re-raised in `StreamWorker.run()` | [#25](https://github.com/bhaveshhpatel/cipher/issues/25) | ✅ |
| S2-POST-9 | Happy path `_refresh_tier_map` test asserts `_tier_map_refresh_task` state post-call | [#31](https://github.com/bhaveshhpatel/cipher/issues/31) | ✅ |
| S2-POST-10 | Exception test asserts `log.warning` emitted when `assign_tiers` raises | [#32](https://github.com/bhaveshhpatel/cipher/issues/32) | ✅ |
| S2-POST-11 | Test: `_get_tier_map` when refresh task already running (not done) | [#33](https://github.com/bhaveshhpatel/cipher/issues/33) | ✅ |
| S2-POST-12 | Test: inner registry exception path in `_process_tick` (`_avg_volume_by_ticker.get()` raises) | [#34](https://github.com/bhaveshhpatel/cipher/issues/34) | ✅ |
| S2-POST-13 | Test: `assign_tiers` returns empty dict `{}` in `_refresh_tier_map` | [#35](https://github.com/bhaveshhpatel/cipher/issues/35) | ✅ |
| S2-POST (PR #40) | `ensemble_runner.py` deprecated stub + `_tier_map_refresh_in_progress` save/restore in fixture + concurrent stale call test; caplog logger fix (inline) | [#40](https://github.com/bhaveshhpatel/cipher/pull/40) | ✅ |

### Low Priority / Anytime ⚪

| Story | Description | Issue | Status |
|---|---|---|---|
| S2-POST-1 | Hoist `get_registry` import out of `_process_tick` hot path | [#22](https://github.com/bhaveshhpatel/cipher/issues/22) | ⚪ Open |
| S2-POST-7 | Fix misleading `test_flush_loop_creates_task_not_blocking` name + intent | [#28](https://github.com/bhaveshhpatel/cipher/issues/28) | ⚪ Open |
| S2-POST-14 | `_flush_loop` orphaned flush tasks — cancel-on-shutdown or document as intentional | [#41](https://github.com/bhaveshhpatel/cipher/issues/41) | ⚪ Open |
| S2-POST-15 | `_get_tier_map` double-guard redundancy — remove stale `task.done()` clause or document belt-and-suspenders | [#42](https://github.com/bhaveshhpatel/cipher/issues/42) | ⚪ Open |

---

## Sprint 2 — Apex L1 / L2 / L4 Signal Layers

> ✅ **S5 is merged. Sprint 2 signal-layer foundation is complete.**
> Full story definitions: [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md)

### Completed

| Story | Description | PR | Status |
|---|---|---|---|
| S3 | Apex L1: `signal_gate.py` — spread gate (uniform 50%) + tier-aware premium floors per trade type; direction-agnostic; 100% branch coverage | [#43](https://github.com/bhaveshhpatel/cipher/pull/43) | ✅ |
| S4 | Apex L2: Dual-window accumulator — DTE-adjusted floors, OTM/ATM/deep-OTM classification, whale-conviction sweep bypass, LEAPS eligibility | [#44](https://github.com/bhaveshhpatel/cipher/pull/44) | ✅ |
| S4-POST-1 | `deep_otm_multiplier=1.0` — add explicit reject-then-pass test pair to pin `> 1.0` branch (not coincidental floor pass) | [#45](https://github.com/bhaveshhpatel/cipher/issues/45) | ✅ |
| S4-POST-2 | `_max_dte_key=None` guard — test (+ optional runtime guard) for BE-1 cache when `dte_premium_tiers={}` | [#46](https://github.com/bhaveshhpatel/cipher/issues/46) | ✅ |
| S4-POST-3 (BE-3) | `_ev_attr` / `_make_key` dict-key bug — regression tests pinning key isolation and object/dict key parity; production-path confirmed as test-only | [#47](https://github.com/bhaveshhpatel/cipher/issues/47) / [#48](https://github.com/bhaveshhpatel/cipher/pull/48) | ✅ |
| BE-3 F2 | `asyncio.run()` fix — replace deprecated `get_event_loop().run_until_complete()` in test helper | [#49](https://github.com/bhaveshhpatel/cipher/issues/49) / [#51](https://github.com/bhaveshhpatel/cipher/pull/51) | ✅ |
| BE-3 F3 | `contract_type` isolation test — CALL vs PUT same ticker/strike/expiry must produce two distinct episode keys | [#50](https://github.com/bhaveshhpatel/cipher/issues/50) / [#51](https://github.com/bhaveshhpatel/cipher/pull/51) | ✅ |
| S5 | Apex L4: Cross-contract ladder detection — multi-strike same-expiry coordination, detection primitive for later `sector_score` wiring | [#52](https://github.com/bhaveshhpatel/cipher/pull/52) | ✅ |

### Low Priority / Anytime ⚪

| Story | Description | Issue | Status |
|---|---|---|---|
| S5-POST-1 | `detect_ladder()` — document or enforce deterministic group selection when multiple `(ticker, expiry)` groups qualify | [#53](https://github.com/bhaveshhpatel/cipher/issues/53) | ⚪ Open |

---

## Sprint 3 — Apex L3 Composite + Swarm

> ✅ **S6 merged via PR #54 (2026-05-01). S6-POST-1 filed as a 🟢 queued follow-up. S7 blocked pending stream worker concurrency review.**
> Full story definitions: [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md)

### Completed

| Story | Description | PR | Status |
|---|---|---|---|
| S6 | Apex L3: Composite formula overhaul — remove fake backtest, episode-level influence tier, `dominant_direction` hot-path fix (SELL PUT → REPEAT_BUY), `composite_score_ceiling` field, `order_side`/`strong_sentiment`/`execution_mechanic`/`premium_tier_score` in bus payload | [#54](https://github.com/bhaveshhpatel/cipher/pull/54) | ✅ |

### Queued 🟢

| Story | Description | Issue | Status |
|---|---|---|---|
| S6-POST-1 | Wire `detect_ladder()` into `_process_trade()` hot path; pass `sector_score` into `build_composite()`; remove `composite_score_ceiling` from bus payload; deliberation required on score shape (binary vs graded) and `composite_score_ceiling` removal atomicity before coding | [#55](https://github.com/bhaveshhpatel/cipher/issues/55) | 🟢 Queued |

### Blocked ⏳

| Story | Description | Issue | Status |
|---|---|---|---|
| S7 | Tiered swarm + circuit breaker — Apex-only, timeout-bounded, deterministic fallback. **Blocked: review `stream_worker.py` to confirm whether `_process_trade()` runs sequentially or via task scheduling before scoping S7.** | TBD | ⏳ Blocked |

---

## Future Sprint

| Story | Description | Issue | Status |
|---|---|---|---|
| S8 | Real backtest score from `flow_events` — 90-day win-rate by (ticker, contract_type, dte_bucket); cache-controlled; re-enables backtest weight | TBD | ⏳ Future |

---

## Parallel Tracks (not blocked by APEX sprint)

| Story | Description | Issue | Status |
|---|---|---|---|
| ING-1 | Ingestion rewrite + delta chain fetch — eliminate duplicate Tradier call, fix upsert on HIT path, fix tier write-back, cut rebuild time ~6 min → ~30s | [#6](https://github.com/bhaveshhpatel/cipher/issues/6) | 🟢 Open |
| C8 | Decouple persist tier from signal tier in `_process_trade` — full tick history in `flow_events` for backtesting | [#2](https://github.com/bhaveshhpatel/cipher/issues/2) | 🟢 Open |

---

## Fully Ordered Build Sequence

Exact execution order. Do not start a story until everything above it in the same gate block is merged.

```
── SPRINT 1 ──────────────────────────────────────────────────────────────────────────────────
1.  ✅  S0            — Swarm cleanup (PR #18)
2.  ✅  S1            — Alert level reconciliation + emit-cache flush (PR #19)
3.  ✅  S2            — Parser + detector + stream worker tier wiring (PR #21)
4.  ✅  #26+#27       — _refresh_tier_map + _process_tick test coverage
5.  ✅  #36           — Inline fixes 2/3/6 (PR #36)
6.  ✅  #30           — S2-POST-8: test isolation / module global teardown (PR #37)
7.  ✅  #29           — S2.5: order_side + strong_sentiment + execution_mechanic DB migration (PR #38)
8.  ✅  #20           — S0.5: Delete ensemble_runner.py (PR #39)
9.  ✅  #23+#24+#25   — S2-POST-2/3/4: flush done callback, tier map race fix, CancelledError re-raise
10. ✅  #31–35        — S2-POST-9–13: 5 test coverage additions (PR #40)
11. ✅  PR #40        — ensemble_runner.py stub, concurrent stale call test, caplog logger fix
───────────────────────────────────────────────────────────────────────────────────

── SPRINT 2 ──────────────────────────────────────────────────────────────────────────────────
12. ✅  S3            — Apex L1: signal_gate.py (PR #43)
13. ✅  S4            — Apex L2: dual-window accumulator (PR #44)
14. ✅  #45           — S4-POST-1: deep_otm_multiplier=1.0 reject-then-pass test pair
15. ✅  #46           — S4-POST-2: _max_dte_key=None guard
16. ✅  #47/#48       — S4-POST-3 (BE-3): _ev_attr dict-key bug regression tests
17. ✅  #49+#50/#51   — BE-3 F2+F3: asyncio.run() fix + contract_type isolation test
18. ✅  S5            — Apex L4: ladder detection (PR #52)
───────────────────────────────────────────────────────────────────────────────────

── SPRINT 3 ──────────────────────────────────────────────────────────────────────────────────
19. ✅  S6            — Apex L3: composite formula overhaul (PR #54)
20. 🟢  S6-POST-1     — Wire detect_ladder() + sector_score into hot path; remove composite_score_ceiling from payload (#55)  ← QUEUED
21. ⏳  S7            — Tiered swarm + circuit breaker  ← BLOCKED (stream worker review first)
───────────────────────────────────────────────────────────────────────────────────

── FUTURE SPRINT ───────────────────────────────────────────────────────────────────────────
22. ⏳  S8            — Real backtest score from flow_events
───────────────────────────────────────────────────────────────────────────────────

── PARALLEL / ANYTIME ─────────────────────────────────────────────────────────────────
23. 🟢  ING-1         — Ingestion rewrite + delta chain fetch (#6)
24. 🟢  C8            — Decouple persist/signal tier (#2)
25. ⚪  #22           — Hoist get_registry import
26. ⚪  #28           — Fix misleading flush loop test
27. ⚪  #41           — _flush_loop orphaned flush tasks (cancel-on-shutdown or document)
28. ⚪  #42           — _get_tier_map double-guard redundancy (clean up or document)
29. ⚪  #53           — detect_ladder() deterministic group selection when multiple groups qualify
───────────────────────────────────────────────────────────────────────────────────
```

---

## Quick Reference

- **"What is next?"** → S6-POST-1 (step 20, issue [#55](https://github.com/bhaveshhpatel/cipher/issues/55)) — wire `detect_ladder()` + `sector_score` into hot path. Deliberation on score shape required before coding. Can run in parallel with S7 prep.
- **"What must close before S7 starts?"** → Stream worker `_process_trade()` concurrency review. S6-POST-1 does not block S7.
- **"What is remaining?"** → Every row not marked ✅ — steps 20 through 29.
- **"What blocks S7 start?"** → Stream worker concurrency review. If `_process_trade()` is sequential, swarm scope changes.
- **"What is the full plan?"** → Read [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md) for story definitions, then this file for current status.
- **After every merge** → Mark row ✅, update version below.
- **After every panel review** → File issues for all findings, add rows to this file before merging.
- **Workflow rule** → Branch + PR always. Never push directly to `main`.

---

*Last updated: 2026-05-01 — S6 completed via PR #54. S6-POST-1 filed (issue #55): wire detect_ladder() + sector_score into hot path, remove composite_score_ceiling from bus payload. S7 blocked pending stream worker concurrency review.*
*Version: 2.9*
