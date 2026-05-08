# Cipher — ING Sprint Story Execution Protocol

> This document is the canonical checklist for executing every story in the **WSJ Ingestion Alignment (ING) sprint**.
> Read this file in full before starting any ING story. No exceptions.
> This file is the ING-specific analogue of the root `STORY-STEPS.md`.

---

## Rule 0 — Before Answering "What's Next / What's Remaining / What's the Order"

**Both files must be consulted together. Neither alone is sufficient.**

1. Read **`docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`** first
   - This is the canonical spec for the ING sprintxa
   - Contains: story scope, acceptance criteria, 3-way deliberation outcomes, QA test matrices, implementation code, dependency graph
   - Defines *what* a story requires, *why*, and *what was already decided*

2. Read **`STORY-STEPS_ING.md`** second
   - This is the execution state tracker across all sprints
   - Contains: what is merged (✅), what is blocked (🔴), what is queued (⏳), post-merge findings, exact build order
   - Defines *where things currently stand*

> GitHub Issues track execution state only. They are not the plan.
> Do not answer ordering or sequencing questions from Issues alone.

---

## Rule 1 — Check Dependency Gates Before Starting

ING stories have hard sequential dependencies. Before touching any story:

1. Open `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md` and locate the **Sprint Order table**
2. Confirm every story listed as a dependency is merged (✅) on `main`
3. If any hard dependency above is not merged — **stop**. Do not start. Report the blocker.
4. If the story is marked blocked (⏳) — **stop**. State exactly what must merge first.

### Dependency Chain (strict — do not violate)

| Story | Depends On | Can Start? |
|---|---|---|
| ING-002 | Nothing | ✅ MERGED |
| ING-003 | Nothing | ✅ MERGED |
| ING-004 | Nothing | ✅ MERGED |
| ING-005 | ING-004 | ✅ MERGED |
| ING-006 | ING-002 | ✅ MERGED |
| ING-009 | ING-002, ING-003, ING-006 | ✅ MERGED 2026-05-06 — PR #76 commit `9ceee35` |
| ING-007 | ING-002, ING-003, ING-006, ING-009 | ✅ MERGED 2026-05-06 — PR #74 commit `b70d9b0`. Issue [#70](https://github.com/bhaveshhpatel/cipher/issues/70) closed. |
| ING-011 | ING-006 ✅, ING-007 ✅ | ✅ MERGED 2026-05-07 — PR #81 commit `8d68ed1`. Issue [#77](https://github.com/bhaveshhpatel/cipher/issues/77) closed. |
| ING-011b | ING-011 ✅ | ✅ MERGED 2026-05-07 — PR #82 squash-merged. Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) closed. |
| ING-010 | ING-002 ✅, ING-003 ✅ | ✅ MERGED 2026-05-08 — PR #85 commit `a673697`. Issue [#78](https://github.com/bhaveshhpatel/cipher/issues/78) closed. |
| ING-008 | ING-004 ✅, ING-005 ✅, **ING-011 ✅** | ⏳ Ready for deliberation — ING-011 blocker cleared. Deliberation required before implementation. |
| **ING-012** | **ING-010 ✅ (tier system must exist), ING-008 ✅ (gate wiring must be stable)** | **⏳ ING-010 blocker cleared 2026-05-08. Still blocked on ING-008 ⏳. Deliberation complete — Issue [#84](https://github.com/bhaveshhpatel/cipher/issues/84). Do not begin implementation until ING-008 merges.** |
| **ADMIN-UI-001** | **ING-010 ✅ (gate control plane API must be live)** | **⏳ ING-010 blocker cleared 2026-05-08. Ready to implement. Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87). Can run in parallel with ING-008 and ING-012 — no backend changes required.** |

---

## Rule 2 — Deliberation Status Check

Every ING story requires a 3-way deliberation **before implementation begins**:
- **Senior Architect (SA)** — architectural impact, data flow, registry coupling, layer boundaries
- **Principal Backend Engineer (PBE)** — implementation correctness, hot-path safety, regression risk
- **Lead QA (QA)** — test coverage, observable stat counters, regression test additions

### Current Deliberation Status

| Story | Deliberation Status |
|---|---|
| ING-002 | ✅ COMPLETE (2026-05-03) — all decisions recorded in sprint doc. **MERGED PR #58 commit `a38f837`** |
| ING-003 | ✅ COMPLETE (2026-05-03) — all decisions recorded in sprint doc. **MERGED PR #59 commit `62b159f`** |
| ING-004 | ✅ COMPLETE (2026-05-03) — all decisions recorded in sprint doc. **MERGED PR #60 commit `d3c3f31`** |
| ING-005 | ✅ COMPLETE (2026-05-03) — all decisions recorded in sprint doc. **MERGED PR #61 commit `252d75f`** |
| ING-006 | ✅ COMPLETE (2026-05-03) — all decisions recorded in sprint doc. **MERGED PR #62 commit `501b170`** |
| ING-009 | ✅ COMPLETE (2026-05-05) — all decisions recorded in sprint doc. **MERGED PR #76 commit `9ceee35` (2026-05-06).** Issue [#75](https://github.com/bhaveshhpatel/cipher/issues/75) closed. |
| ING-007 | ✅ COMPLETE (2026-05-04) — pre-merge panel deliberation COMPLETE (2026-05-06). **MERGED PR #74 commit `b70d9b0` (2026-05-06).** Issue [#70](https://github.com/bhaveshhpatel/cipher/issues/70) closed. |
| ING-011 | ✅ COMPLETE (2026-05-06) — D1/D2/D3 resolved. Pre-merge panel deliberation COMPLETE (2026-05-06). All panel findings (SA-F1, QA-F1/QA-F3) resolved inline. **MERGED PR #81 commit `8d68ed1` (2026-05-07).** Issue [#77](https://github.com/bhaveshhpatel/cipher/issues/77) closed. |
| ING-011b | ✅ COMPLETE (2026-05-07) — D1–D5 resolved. Pre-merge panel deliberation COMPLETE (2026-05-07). All panel findings (SA-1, PBE-1, PBE-2 non-blocking; QA-1 resolved inline; QA-2 typo fixed commit `2bb1487`) resolved. **MERGED PR #82 squash-merged (2026-05-07).** Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) closed. |
| ING-010 | ✅ COMPLETE (2026-05-08) — pre-merge panel deliberation COMPLETE (2026-05-08). Findings SA-1/PBE-2/PBE-3/PBE-4 resolved inline on branch before merge. **MERGED PR #85 commit `a673697` (2026-05-08).** Issue [#78](https://github.com/bhaveshhpatel/cipher/issues/78) closed. |
| ING-008 | ⏳ Ready for deliberation — ING-011 blocker cleared (MERGED 2026-05-07). Deliberation required (SA · PBE · QA). D1/D2/D3 open questions in sprint doc. Do not begin implementation until deliberation is complete. |
| **ING-012** | **⏳ ING-010 blocker cleared 2026-05-08. Still blocked on ING-008 ⏳. Deliberation COMPLETE — D1 (singleton hot-reload), D2 (O(1) epoch versioning), D3 (hardcoded bounds + market-hours guard) all resolved in sprint doc. Issue [#84](https://github.com/bhaveshhpatel/cipher/issues/84). Do not begin implementation until ING-008 merges.** |
| **ADMIN-UI-001** | **✅ COMPLETE (2026-05-08) — 5-way deliberation (SA · UI · FE · PBE · QA) complete. All decisions recorded in Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87). No backend changes required — `GET /api/admin/gate-config` and `PATCH /api/admin/gate-config` from PR #85 are the full API surface. Ready to implement.** |

> For ING-002 through ING-007, ING-009, ING-010, ING-011, and ING-011b: deliberation is complete. Do not re-litigate any decisions.
> For ING-008 and ING-012: read the open deliberation questions in the sprint doc before writing a single line of code.

---

## Rule 3 — Read the Spec Before Writing Any Code

Before creating a branch for any ING story:

1. Read the full story section in `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`
2. Identify: scope, acceptance criteria, deliberation outcomes (if complete), implementation steps, QA test matrix
3. Read the open deliberation questions if status is 🔴 — internalize them before implementation
4. Cross-reference the story's GitHub Issue for any post-filing amendments
5. Note any **Critical** callouts (e.g. ING-002 SA-Q3 on caller update — these are mandatory, not optional)
6. Only then proceed to branch creation

### Key Reference Files for ING Stories

| File | Purpose |
|---|---|
| [`docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`](https://github.com/bhaveshhpatel/cipher/blob/main/docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md) | Canonical ING sprint spec — all story definitions, deliberations, AC, test matrices |
| [`docs/ORDER_SIDE_RESOLUTION.md`](https://github.com/bhaveshhpatel/cipher/blob/main/docs/ORDER_SIDE_RESOLUTION.md) | ING-001 resolution — why `order_side` is not available from Tradier; aggression proxy rationale |
| [`docs/FIXES.md`](https://github.com/bhaveshhpatel/cipher/blob/main/docs/FIXES.md) | Running fixes log — ING stories must document Option decisions and API findings here |
| [`docs/ARCHITECTURE.md`](https://github.com/bhaveshhpatel/cipher/blob/main/docs/ARCHITECTURE.md) | Gate structure — must be updated post-sprint to reflect new gates added by ING stories |
| [`STORY-STEPS.md`](https://github.com/bhaveshhpatel/cipher/blob/main/STORY-STEPS.md) | Root protocol — this file is the ING-specific extension of it |
| GitHub Issues | Execution tracking only |

---

## Rule 4 — Branch + PR Always

```
NEVER push ING story work directly to main.
Always: branch → commits → PR → deliberation → merge.
```

### Branch Naming Convention for ING Stories

| Story type | Branch name format |
|---|---|
| ING sprint story | `ing/s{N}-{short-description}` |
| Post-merge fix on ING story | `ing/s{N}-post-{number}-{short-description}` |
| ING hotfix | `hotfix/ing-{short-description}` |

**Examples:**
- `ing/s2-premium-floor` → ING-002
- `ing/s3-dte-tiers-init` → ING-003
- `ing/s4-underlying-price-fallback` → ING-004
- `ing/s5-otm-threshold-align` → ING-005
- `ing/s6-directional-aggression` → ING-006
- `ing/s9-episode-upsert` → ING-009
- `ing/s7-multiday-repeat` → ING-007 ✅ MERGED PR #74
- `ing/s11-itm-classification` → ING-011 ✅ MERGED PR #81
- `ing/s11b-itm-aggression-weight` → ING-011b ✅ MERGED PR #82
- `ing/s10-tiered-gate-control-plane` → ING-010 ✅ MERGED PR #85
- `ing/s8-vol-oi-gate` → ING-008
- **`ing/s12-tier-gate-config` → ING-012**
- **`admin/ing-gate-control-panel` → ADMIN-UI-001**

### PR Body Must Include

1. Story reference (e.g. "Closes #57" for ING-002)
2. Scope summary — what this PR does in plain English
3. Files changed (list all modified files)
4. Acceptance criteria checklist — every checkbox from the sprint doc
5. QA test matrix — all required test cases from the sprint doc, with pass/fail
6. Panel deliberation notes (see Rule 5)
7. Stats counter verification — confirm all new `_stats` keys are in the `/health/stream` output

---

## Rule 5 — Panel Deliberation on Every PR

Before any ING PR merges, all three roles must deliberate on the diff.

### Senior Architect Review
- Is scope correct per `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`?
- Does the implementation respect the gate order: `dedup → parse → accumulate → persist`?
- Are layer boundaries respected (e.g. no parser importing from services unless deliberation approved it)?
- Does the PR correctly address any Critical callouts in the story?
- Does the implementation match the deliberation decisions already recorded in the sprint doc?

### Principal Backend Engineer Review
- Is the implementation correct? Logic errors, edge cases, unsafe defaults?
- Are sentinel returns, type annotations, and return type unions exactly right?
- Are all callers of modified functions updated (not just the primary caller)?
- Are `_stats` counters initialised at module level — not conditionally on first use?
- Are hot-path changes safe for async execution with no blocking IO introduced?
- Does the implementation match the PBE decisions recorded in the sprint doc?

### Lead QA Review
- Are all acceptance criteria checkboxes from the sprint doc covered?
- Is every test case in the story's QA test matrix present and passing?
- Are boundary value tests present (floor-1, floor, floor+1)?
- Are counter separation tests present?
- Are cold-start safety tests present?
- Are regression tests for existing behaviour present and green?

### Findings Resolution

| Finding type | Resolution |
|---|---|
| Typo, comment, non-logic change | Fix inline on the PR before merge |
| Logic fix that fits in the same PR scope | Fix inline on the PR before merge |
| New work requiring a separate PR | File a numbered GitHub Issue AND add a row to `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md` AND note in sprint doc before merging |

> No finding lives only in conversation. If it needs tracking, it gets a GitHub Issue AND a docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md row.
> Only after all findings are resolved does the PR merge.

---

## Rule 6 — ING-Specific Implementation Constraints

These apply to every ING story. Violating any of these is a blocker at deliberation.

1. **Gate order is fixed.** `dedup → parse → accumulate → persist`. No story may reorder these gates.
2. **Sentinels are not exceptions.** `"below_premium"` is a clean filter drop — never increment `parse_failed` for it. Maintain strict counter separation.
3. **No TODO comments in implementation code.** Follow-up work goes in a GitHub Issue and `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md` row. The code itself must be clean.
4. **Every new `_stats` key must be in the module-level init block.** No `KeyError` on `/health/stream` from cold start.
5. **No DB reads on the hot path.** Registry lookups must be non-blocking dict reads. DB-backed config is fine at module init with a hardcoded fallback — not inline per-tick.
6. **Hardcoded floors are safe defaults, not tech debt.** ING-002's `_MIN_EVENT_PREMIUM = 10_000` is intentional architecture. Do not move to DB config until ING-002-CONFIG is in scope.
7. **ING-007 Supabase migration is a hard prerequisite.** ✅ SHIPPED — S2.5 migration applied (index + `order_side` + `is_aggressive` columns on `flow_events`). ING-007 MERGED PR #74 commit `b70d9b0` 2026-05-06.
8. **ING-008 chain API verification is a hard prerequisite.** Document OI quality findings in `docs/FIXES.md` under ING-008 before writing any gate logic.
9. **ING-009 merged 2026-05-06 (PR #76 commit `9ceee35`).** `flow_episodes` is correctly aggregated.
10. **ING-011 merged 2026-05-07 (PR #81 commit `8d68ed1`).** ✅ `_classify_moneyness_band()` is live on main. `otm_band` TEXT column extended to cover `ITM | DEEP_ITM`. ING-008 deliberation is now unblocked.
11. **ING-010 merged 2026-05-08 (PR #85 commit `a673697`).** ✅ `GateConfigStore` singleton is live on main. 5 gates × 3 tiers hot-reloadable from `gate_configs` DB table. `gate_config_store.load()` runs at lifespan step 0. ING-012 ING-010 blocker now cleared — still waiting on ING-008. ADMIN-UI-001 ING-010 blocker also cleared — ready to implement (Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87)).
12. **ING-011b merged 2026-05-07 (PR #82 squash-merged).** ✅ D1 Option B live on main — `get_weighted_premium()` now applies `_AGGRESSION_DISCOUNT` to ITM/DEEP_ITM PUT AT_BID/BELOW_BID fills regardless of `is_aggressive` flag. `_classify_moneyness_band()` promoted to module-level function (D3). `_ITM_BANDS = frozenset({"ITM","DEEP_ITM"})` exported (D4). UNKNOWN band → full weight safe-by-default (D5). Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) closed.
13. **ING-012 depends on ING-010 ✅ and ING-008 ⏳ both merged.** ING-010 cleared 2026-05-08. Do not begin ING-012 implementation until ING-008 merges. Three-way deliberation is complete and recorded in the sprint doc and Issue [#84](https://github.com/bhaveshhpatel/cipher/issues/84) — do not re-litigate D1 (singleton hot-reload), D2 (O(1) tier lookup + epoch versioning), or D3 (hardcoded bounds + market-hours guard).
14. **ING-012: No DB reads on the gate hot-path.** `GateConfigStore.get_threshold()` is a dict lookup only. The singleton is updated async via admin endpoint; workers read from it synchronously per tick. This is a hard constraint — no awaiting config store inside gate evaluation.
15. **ING-012: `confirm_market_hours: true` is required for any gate config change between 09:30–16:00 ET.** Admin endpoint returns HTTP 428 without it. This is a safety gate, not optional.
16. **ADMIN-UI-001 depends on ING-010 ✅ only.** No backend changes required — `GET /api/admin/gate-config` and `PATCH /api/admin/gate-config` from PR #85 are the complete API surface. Can be implemented in parallel with ING-008 and ING-012. Branch: `admin/ing-gate-control-panel`. Full 5-way deliberation (SA · UI Engineer · FE Engineer · PBE · QA) complete in Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87). Key constraints: (a) UI is fully data-driven from GET response — no hardcoded gate names or bounds; (b) `_ms` gates display/accept in seconds, convert to/from ms at the API boundary; (c) `exclude_indices` is a single toggle, always PATCHed with `tier: 1`; (d) `require_oi` renders as toggle (0.0/1.0), not numeric input; (e) 428 market-hours guard requires explicit inline user confirmation before re-submit; (f) epoch stored in component state — mismatch triggers stale-read warning + re-fetch; (g) section lazy-loaded (dynamic import).

---

## Rule 7 — Post-Merge Cleanup

Immediately after an ING PR merges:

1. **Close the GitHub Issue** (state: `completed`)
2. **Update `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`**: mark story ✅, update Quick Reference, add any new post-merge stories
3. **Update `STORY-STEPS_ING.md`**: mark the story row ✅ in the Sprint Order table, update `Last updated` line
4. **Update `docs/FIXES.md`**: add Option decision record if story required a choice (e.g. ING-005 Option A/B/C)
5. If new GitHub Issues were filed from panel findings — confirm they are open and linked in `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`
6. If story adds new gates — update `docs/ARCHITECTURE.md` gate structure section

**ING-007 specific post-merge steps — ✅ COMPLETE (2026-05-06):**
- ✅ Issue [#70](https://github.com/bhaveshhpatel/cipher/issues/70) closed
- ✅ Sprint doc updated — ING-007 marked MERGED, AC boxes checked, panel verdicts recorded, Post-ING-007 findings section added
- ✅ `STORY-STEPS_ING.md` updated — dependency chain and deliberation tables marked ✅
- Post-merge findings (SA-F1, SA-F2, PBE-F4, QA-F1, QA-F2) logged in sprint doc — confirm `docs/FIXES.md` updated if applicable
- Confirm `docs/ARCHITECTURE.md` updated if new gates were added by ING-007

**ING-011 specific post-merge steps:**
- ✅ Issue [#77](https://github.com/bhaveshhpatel/cipher/issues/77) closed (state: completed)
- ✅ `STORY-STEPS_ING.md` updated — dependency chain, deliberation table, Rule 6 constraint 10, Quick Reference all marked ✅. ING-008 unblocked to ⏳.
- ☐ `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md` — mark ING-011 ✅ MERGED, update Quick Reference, record D1/D2/D3 decisions and panel verdicts
- ☐ `docs/FIXES.md` — add D1/D2/D3 option decisions (ITM threshold=0.02, override PUT-only, otm_band extended in-place)
- ☐ `docs/ARCHITECTURE.md` — confirm `otm_band` enum extension (`ITM | DEEP_ITM` added) is documented
- ☐ Confirm TMDX $105P scenario re-run produces correct `BEARISH` direction on next live session
- ☐ Confirm existing OTM put `AT_BID` → `REPEAT_SELL` (bullish) behaviour is unchanged — no OTM regression
- ☐ Confirm ING-011b (#80) is still open and flagged as next deliberation item

**ING-011b specific post-merge steps — ✅ COMPLETE (2026-05-07):**
- ✅ Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) closed (state: completed)
- ✅ `STORY-STEPS_ING.md` updated — dependency chain, deliberation table, Rule 6 constraint 12, Quick Reference all marked ✅
- ✅ `docs/FIXES.md` — ING-011b entry added with D1 Option B + D3 + D4 + D5 decisions recorded
- ✅ `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md` — ING-011b marked ✅ MERGED, Quick Reference updated, panel verdicts recorded
- ✅ `docs/ARCHITECTURE.md` — `get_weighted_premium()` D1 Option B discount logic documented; `_classify_moneyness_band()` module-level promotion (D3) noted
- ☐ Confirm `weighted_premium` for a 3-event ITM PUT AT_BID episode at $200k total premium clears Gate 2 at the same rate as a $100k OTM PUT AT_ASK episode (discount correctly applied) — verify on next live session
- ☐ Confirm no regression on ING-006 test suite (`test_ing006_*.py`) — run CI
- ☐ Confirm no regression on ING-007 multi-day lookback tests — run CI

**ING-010 specific post-merge steps — ✅ MERGED 2026-05-08 (PR #85 commit `a673697`):**
- ✅ Issue [#78](https://github.com/bhaveshhpatel/cipher/issues/78) closed (state: completed)
- ✅ `STORY-STEPS_ING.md` updated — dependency chain, deliberation table, Rule 6 constraint 11, Quick Reference all marked ✅. ING-012 ING-010 blocker cleared. ADMIN-UI-001 ING-010 blocker cleared — Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87) filed, deliberation complete, ready to implement.
- ☐ `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md` — mark ING-010 ✅ MERGED, update Quick Reference, record panel verdicts (SA-1/PBE-2/PBE-3/PBE-4 resolved inline)
- ☐ `docs/FIXES.md` — record ING-010 option decisions (D1/D2/D3) and ING-010-ACC accumulator tier_map sync fix
- ☐ `docs/ARCHITECTURE.md` — document `GateConfigStore` singleton, `gate_configs` DB table, 5-gate × 3-tier threshold matrix, `gate_config_store.load()` at lifespan step 0, admin GET/PATCH API contract
- ☐ Confirm GDYN and PENG flow events are now appearing in `flow_events` on the next live session (manual spot-check)
- ☐ Confirm Tier 1/2 signal counts are unchanged (no noise regression on NVDA/SPY/AMD/AAPL)

**ADMIN-UI-001 specific post-merge steps (once merged):**
- ☐ Close Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87) (state: completed)
- ☐ `STORY-STEPS_ING.md` — mark ADMIN-UI-001 ✅ MERGED in dependency chain, deliberation table, Quick Reference
- ☐ `docs/ARCHITECTURE.md` — document the Ingestion Gate Control Panel as the admin UI surface for `GateConfigStore`; note gate name → human-readable mapping, seconds↔ms conversion convention, `exclude_indices` tier-independent toggle behaviour
- ☐ Confirm `GET /api/admin/gate-config` returns 21 rows (7 gates × 3 tiers) and section renders all 7 cards
- ☐ Confirm `exclude_indices` card shows single toggle only (no tier rows)
- ☐ Confirm `require_oi` renders as toggle, not numeric input
- ☐ Confirm `_ms` gates display and accept in seconds (5000ms → input shows 5, typing 10 → PATCHes 10000)
- ☐ Confirm 428 market-hours guard shows inline confirm prompt (not modal, not silent retry)
- ☐ Confirm epoch indicator updates in section header after each successful save
- ☐ Confirm section is lazy-loaded — does not appear in initial admin page JS bundle
- ☐ Confirm no regression on existing Admin page sections (Demo, Tier Thresholds, Registry Prewarm)
- ☐ All QA-1 through QA-9 test cases from Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87) passing

**ING-012 specific post-merge steps (once merged):**
- ☐ Close Issue [#84](https://github.com/bhaveshhpatel/cipher/issues/84) (state: completed)
- ☐ `STORY-STEPS_ING.md` — mark ING-012 ✅ MERGED in dependency chain, deliberation table, Quick Reference
- ☐ `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md` — mark ING-012 ✅ MERGED, update Quick Reference, record panel verdicts
- ☐ `docs/FIXES.md` — record D1 (singleton hot-reload), D2 (epoch versioning), D3 (hardcoded bounds + market-hours guard) decisions
- ☐ `docs/ARCHITECTURE.md` — document `GateConfigStore` singleton, `gate_configs` DB table, per-tier threshold matrix, admin API contract
- ☐ Confirm `GET /api/admin/gate-config` returns full config matrix with bounds embedded
- ☐ Confirm config change during market hours without `confirm_market_hours: true` returns HTTP 428
- ☐ Confirm gate threshold change propagates to all workers within 5 seconds (no restart)
- ☐ Confirm audit trail row written to `gate_configs` for every successful PATCH
- ☐ Verify `gate_config_store.get_threshold()` benchmarks at O(1) under load

---

## Full ING Story Execution Checklist

Run through this list top-to-bottom for every ING story, without skipping steps.

```
PRE-FLIGHT
☐  Read the full story section in docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md
☐  Confirm deliberation status — if 🔴 NOT STARTED, run deliberation first
☐  Confirm all hard dependency stories above this one are ✅ merged
☐  Read GitHub Issue for any post-filing amendments
☐  Confirm story is not ⏳ blocked
☐  Review ING-specific constraints in Rule 6 above

DELIBERATION (if not already complete)
☐  Work through every open SA question for this story
☐  Work through every open PBE question for this story
☐  Work through every open QA question for this story
☐  Record all decisions inline in docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md before writing code
☐  Update deliberation status table in this file

BRANCH
☐  Create branch off main (naming convention: ing/s{N}-{short-description})
☐  Confirm branch is off latest main SHA

IMPLEMENTATION
☐  Follow implementation steps in exact order from the sprint doc
☐  Address all acceptance criteria checkboxes from the sprint doc
☐  Implement all QA test cases from the story's test matrix
☐  Confirm all new _stats keys are in module-level init block
☐  Confirm no TODO comments left in code
☐  Confirm no circular imports introduced

PR
☐  Open PR from branch → main
☐  PR body includes: story ref, scope, files changed, AC checklist, QA test matrix, stats verification
☐  Wait for green CI build before starting deliberation

DELIBERATION (on PR diff)
☐  Senior Architect reviews diff — gate order, layer boundaries, Critical callout resolution, deliberation alignment
☐  Principal Backend Engineer reviews diff — correctness, caller updates, stats init, hot-path safety
☐  Lead QA reviews diff — all AC covered, full QA matrix present, boundary values, counter separation, cold-start
☐  Resolve all findings inline or file GitHub Issues + docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md rows for new work
☐  Zero unresolved findings remaining
☐  Display full deliberation and wait for approval before merge

MERGE
☐  Include this checklist in the PR comment with every step marked
☐  Squash merge into main

POST-MERGE
☐  Close GitHub Issue (state: completed)
☐  Mark story row ✅ in docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md
☐  Mark story row ✅ in STORY-STEPS_ING.md Sprint Order table
☐  Update Quick Reference section in docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md
☐  Add any new post-merge issue rows to docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md
☐  Update docs/FIXES.md with Option decisions if applicable
☐  Update docs/ARCHITECTURE.md if new gates were added
☐  Bump docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md Last updated line
☐  Bump STORY-STEPS_ING.md Last updated line
```

---

## ING Story Quick Reference

| Story | Title | Status | Branch Name |
|---|---|---|---|
| ING-002 | Hard $10k premium floor at parser | ✅ MERGED 2026-05-03 — PR #58 commit `a38f837` | `ing/s2-premium-floor` |
| ING-003 | Wire DTE premium tiers at accumulator init | ✅ MERGED 2026-05-03 — PR #59 commit `62b159f` | `ing/s3-dte-tiers-init` |
| ING-004 | Fallback `underlying_price` from registry | ✅ MERGED 2026-05-03 — PR #60 commit `d3c3f31` | `ing/s4-underlying-price-fallback` |
| ING-005 | Align OTM band thresholds | ✅ MERGED 2026-05-03 — PR #61 commit `252d75f` | `ing/s5-otm-threshold-align` |
| ING-006 | Directional aggression weighting on premium floor | ✅ MERGED 2026-05-04 — PR #62 commit `501b170` | `ing/s6-directional-aggression` |
| ING-009 | Same-session flow episode upsert/merge | ✅ MERGED 2026-05-06 — PR #76 commit `9ceee35` — Issue [#75](https://github.com/bhaveshhpatel/cipher/issues/75) closed | `ing/s9-episode-upsert` |
| ING-007 | Multi-day repeat window lookback + is_aggressive DB column | ✅ MERGED 2026-05-06 — PR #74 commit `b70d9b0` — Issue [#70](https://github.com/bhaveshhpatel/cipher/issues/70) closed | `ing/s7-multiday-repeat` |
| ING-011 | ITM put/call moneyness classification + direction override | ✅ MERGED 2026-05-07 — PR #81 commit `8d68ed1` — Issue [#77](https://github.com/bhaveshhpatel/cipher/issues/77) closed | `ing/s11-itm-classification` |
| ING-011b | `is_aggressive` moneyness-blindness fix — ITM PUT AT_BID `weighted_premium` discount | ✅ MERGED 2026-05-07 — PR #82 squash-merged — Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) closed | `ing/s11b-itm-aggression-weight` |
| ING-010 | Tiered gate control plane — per-tier configurable ingestion gates | ✅ MERGED 2026-05-08 — PR #85 commit `a673697` — Issue [#78](https://github.com/bhaveshhpatel/cipher/issues/78) closed | `ing/s10-tiered-gate-control-plane` |
| ING-008 | Volume vs. OI gate | ⏳ Ready for deliberation — ING-011 blocker cleared 2026-05-07 | `ing/s8-vol-oi-gate` |
| **ING-012** | **Tier-aware configurable gate system with hot-reload admin control** | **⏳ ING-010 blocker cleared 2026-05-08 — still blocked on ING-008 ⏳ — Issue [#84](https://github.com/bhaveshhpatel/cipher/issues/84) — deliberation complete** | **`ing/s12-tier-gate-config`** |
| **ADMIN-UI-001** | **Ingestion Gate Control Panel — per-tier gate editor + exclude_indices toggle on Admin page** | **⏳ ING-010 blocker cleared 2026-05-08 — ready to implement — Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87) — 5-way deliberation complete — can run in parallel with ING-008 / ING-012** | **`admin/ing-gate-control-panel`** |

---

*Created: 2026-05-03 | Last updated: 2026-05-08 (ADMIN-UI-001 added — Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87) — 5-way deliberation complete; ING-010 post-merge checklist updated with ADMIN-UI-001 reference; Rule 6 constraint 16 added; dependency chain, deliberation table, Quick Reference all updated) | Sprint: WSJ Ingestion Alignment (P0) | Owner: Dhruv Patel*
*Template: derived from root `STORY-STEPS.md` — ING-specific constraints, branch naming, deliberation state, and reference table added*
