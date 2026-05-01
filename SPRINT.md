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
> deliberate on the diff. Small fixes go inline. Anything needing a separate PR gets a numbered
> story added here AND filed as a GitHub issue. No story lives only in conversation.
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

### Pre-S3 Hard Gates — ALL must merge before S3 starts 🔴

| Story | Description | Issue | Status |
|---|---|---|---|
| S2-POST-5 | `_refresh_tier_map` test coverage — 5 tests (happy path, None registry, assign_tiers failure, ts update) | [#26](https://github.com/bhaveshhpatel/cipher/issues/26) | 🔴 Open |
| S2-POST-6 | `_process_tick` registry avg_volume lookup path — 3 tests | [#27](https://github.com/bhaveshhpatel/cipher/issues/27) | 🔴 Open |
| S2.5 | Supabase migration: `order_side` (BUY/SELL/UNKNOWN + index) + `strong_sentiment` (bool) on `flow_events` | [#29](https://github.com/bhaveshhpatel/cipher/issues/29) | 🔴 Open |
| S0.5 | Delete deprecated `simulation/ensemble_runner.py` | [#20](https://github.com/bhaveshhpatel/cipher/issues/20) | 🔴 Open |

> **Recommended:** Run #26 + #27 as a single test-only PR. Then #29 (migration). Then #20 (deletion).

### Must Close Before S3 Merges (can work in parallel with S3 branch) 🟡

| Story | Description | Issue | Status |
|---|---|---|---|
| S2-POST-3 | Tier map refresh double-spawn race — add `asyncio.Lock` guard | [#24](https://github.com/bhaveshhpatel/cipher/issues/24) | 🟡 Open |
| S2-POST-4 | `CancelledError` not re-raised in `StreamWorker.run()` — pre-existing shutdown bug | [#25](https://github.com/bhaveshhpatel/cipher/issues/25) | 🟡 Open |
| S2-POST-2 | Flush loop task leak — add `.add_done_callback` to `create_task` dispatch | [#23](https://github.com/bhaveshhpatel/cipher/issues/23) | 🟡 Open |

### Low Priority / Anytime ⚪

| Story | Description | Issue | Status |
|---|---|---|---|
| S2-POST-1 | Hoist `get_registry` import out of `_process_tick` hot path | [#22](https://github.com/bhaveshhpatel/cipher/issues/22) | ⚪ Open |
| S2-POST-7 | Fix misleading `test_flush_loop_creates_task_not_blocking` name + intent | [#28](https://github.com/bhaveshhpatel/cipher/issues/28) | ⚪ Open |

---

## Sprint 2 — Apex L1 / L2 / L4 Signal Layers

> ⏳ **Blocked until all Sprint 1 🔴 gates are merged.**
> Full story definitions: [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md)

| Story | Description | Issue | Status |
|---|---|---|---|
| S3 | Apex L1: `signal_gate.py` — filter noise before accumulation; tier-aware premium floors, spread quality, direction-aware aggression | TBD | ⏳ Blocked |
| S4 | Apex L2: Dual-window accumulator — DTE-adjusted floors, OTM/ATM/deep-OTM classification, whale-conviction sweep bypass, LEAPS eligibility | TBD | ⏳ Blocked |
| S5 | Apex L4: Cross-contract ladder detection — multi-strike same-expiry coordination, wires `sector_score` into L3 composite | TBD | ⏳ Blocked |

---

## Sprint 3 — Apex L3 Composite + Swarm

> ⏳ **Blocked until S4 + S5 are merged.**
> Full story definitions: [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md)

| Story | Description | Issue | Status |
|---|---|---|---|
| S6 | Apex L3: Composite formula overhaul — remove fake backtest, episode-level influence tier, SELL PUT end-to-end, `composite_score_ceiling` field | TBD | ⏳ Blocked |
| S7 | Tiered swarm + circuit breaker — Apex-only, timeout-bounded, deterministic fallback. **Blocked pending stream worker concurrency review.** | TBD | ⏳ Blocked |

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
── SPRINT 1 ─────────────────────────────────────────────────────────────
1.  ✅  S0        — Swarm cleanup (PR #18)
2.  ✅  S1        — Alert level reconciliation + emit-cache flush (PR #19)
3.  ✅  S2        — Parser + detector + stream worker tier wiring (PR #21)

── PRE-S3 HARD GATES (all must merge before S3 starts) ──────────────────
4.  🔴  #26+#27   — _refresh_tier_map + _process_tick test coverage [single PR]
5.  🔴  #29       — S2.5: order_side + strong_sentiment DB migration
6.  🔴  #20       — S0.5: Delete ensemble_runner.py
─────────────────────────────────────────────────────────────────────────

── BEFORE S3 MERGES (parallel with S3 branch) ───────────────────────────
7.  🟡  #24       — Tier map refresh race fix
8.  🟡  #25       — CancelledError re-raise in StreamWorker.run()
9.  🟡  #23       — Flush loop done callback
─────────────────────────────────────────────────────────────────────────

── SPRINT 2 ─────────────────────────────────────────────────────────────
10. ⏳  S3        — Apex L1: signal_gate.py
11. ⏳  S4        — Apex L2: dual-window accumulator
12. ⏳  S5        — Apex L4: ladder detection
─────────────────────────────────────────────────────────────────────────

── SPRINT 3 ─────────────────────────────────────────────────────────────
13. ⏳  S6        — Apex L3: composite formula overhaul
14. ⏳  S7        — Tiered swarm + circuit breaker (pending concurrency review)
─────────────────────────────────────────────────────────────────────────

── FUTURE SPRINT ────────────────────────────────────────────────────────
15. ⏳  S8        — Real backtest score from flow_events
─────────────────────────────────────────────────────────────────────────

── PARALLEL / ANYTIME ───────────────────────────────────────────────────
16. 🟢  ING-1     — Ingestion rewrite + delta chain fetch (#6)
17. 🟢  C8        — Decouple persist/signal tier (#2)
18. ⚪  #22       — Hoist get_registry import
19. ⚪  #28       — Fix misleading flush loop test
─────────────────────────────────────────────────────────────────────────
```

---

## Quick Reference

- **"What is next?"** → First 🔴 item in the Pre-S3 Hard Gates block (step 4: #26 + #27).
- **"What is remaining?"** → Every row not marked ✅ — steps 4 through 19.
- **"What blocks S3?"** → Steps 4–6 (all 🔴 gates).
- **"What is the full plan?"** → Read [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md) for complete story definitions, then use this file for current status.
- **After every merge** → Mark row ✅, update version below.
- **After every panel review** → File issues for all findings, add rows to this file before merging.

---

*Last updated: 2026-05-01 — Updated to cross-reference canonical spec, added full S0–S8 sequence.*
*Version: 1.1*
