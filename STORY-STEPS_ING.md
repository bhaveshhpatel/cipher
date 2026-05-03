# Cipher — ING Sprint Story Execution Protocol

> This document is the canonical checklist for executing every story in the **WSJ Ingestion Alignment (ING) sprint**.
> Read this file in full before starting any ING story. No exceptions.
> This file is the ING-specific analogue of the root `STORY-STEPS.md`.

---

## Rule 0 — Before Answering "What's Next / What's Remaining / What's the Order"

**Both files must be consulted together. Neither alone is sufficient.**

1. Read **`docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`** first
   - This is the canonical spec for the ING sprint
   - Contains: story scope, acceptance criteria, 3-way deliberation outcomes, QA test matrices, implementation code, dependency graph
   - Defines *what* a story requires, *why*, and *what was already decided*

2. Read **`docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`** second
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
| ING-004 | Nothing | ✅ Deliberation complete — IN PROGRESS (branch `ing/s4-underlying-price-fallback`) |
| ING-005 | ING-004 | 🔴 After ING-004 merges |
| ING-006 | ING-002 | After deliberation |
| ING-007 | ING-002, ING-003 | After deliberation |
| ING-008 | ING-004, ING-005 | After both merge + deliberation |

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
| ING-004 | ✅ COMPLETE (2026-05-03) — all decisions recorded in sprint doc. **IN PROGRESS — branch `ing/s4-underlying-price-fallback` commit `327300d`** |
| ING-005 | 🔴 NOT STARTED — deliberation required before implementation |
| ING-006 | 🔴 NOT STARTED — deliberation required before implementation |
| ING-007 | 🔴 NOT STARTED — deliberation required before implementation |
| ING-008 | 🔴 NOT STARTED — deliberation required before implementation |

> For ING-002: deliberation is complete and story is merged. Do not re-litigate any decisions.
> For ING-003: deliberation is complete and story is merged. Do not re-litigate any decisions.
> For ING-004: deliberation is complete. The decisions are law. Do not re-litigate SA-Q1/2/3, PBE-Q1/2/3/4, or QA-Q1/2/3/4.
> For all other stories: read the open deliberation questions in the sprint doc before writing a single line of code.

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
- `ing/s6-directional-aggression` → ING-006

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
7. **ING-007 Supabase migration is a hard prerequisite.** Run `EXPLAIN ANALYZE` and confirm index hit before writing any Python for the lookback query.
8. **ING-008 chain API verification is a hard prerequisite.** Document OI quality findings in `docs/FIXES.md` under ING-008 before writing any gate logic.

---

## Rule 7 — Post-Merge Cleanup

Immediately after an ING PR merges:

1. **Close the GitHub Issue** (state: `completed`)
2. **Update `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`**: mark story ✅, update Quick Reference, add any new post-merge stories
3. **Update `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`**: mark the story row ✅ in the Sprint Order table, update `Last updated` line
4. **Update `docs/FIXES.md`**: add Option decision record if story required a choice (e.g. ING-005 Option A/B/C)
5. If new GitHub Issues were filed from panel findings — confirm they are open and linked in `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`
6. If story adds new gates — update `docs/ARCHITECTURE.md` gate structure section

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
☐  Mark story row ✅ in docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md Sprint Order table
☐  Update Quick Reference section in docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md
☐  Add any new post-merge issue rows to docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md
☐  Update docs/FIXES.md with Option decisions if applicable
☐  Update docs/ARCHITECTURE.md if new gates were added
☐  Bump docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md version + Last updated line
☐  Bump docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md Last updated line
```

---

## ING Story Quick Reference

| Story | Title | Status | Branch Name |
|---|---|---|---|
| ING-002 | Hard $10k premium floor at parser | ✅ MERGED 2026-05-03 — PR #58 commit `a38f837` | `ing/s2-premium-floor` |
| ING-003 | Wire DTE premium tiers at accumulator init | ✅ MERGED 2026-05-03 — PR #59 commit `62b159f` | `ing/s3-dte-tiers-init` |
| ING-004 | Fallback `underlying_price` from registry | 🟡 IN PROGRESS — branch `ing/s4-underlying-price-fallback` commit `327300d` | `ing/s4-underlying-price-fallback` |
| ING-005 | Align OTM band thresholds | 🔴 Blocked on ING-004 + deliberation | `ing/s5-otm-threshold-align` |
| ING-006 | Directional aggression weighting | 🔴 Deliberation required (ING-002 ✅) | `ing/s6-directional-aggression` |
| ING-007 | Multi-day repeat window lookback | 🔴 Deliberation required (ING-002 ✅ ING-003 ✅) | `ing/s7-multiday-repeat` |
| ING-008 | Volume vs. OI gate | 🔴 Blocked on ING-004 + ING-005 + deliberation | `ing/s8-vol-oi-gate` |

---

*Created: 2026-05-03 | Last updated: 2026-05-03 (ING-004 deliberation complete, branch `ing/s4-underlying-price-fallback` in progress) | Sprint: WSJ Ingestion Alignment (P0) | Owner: Dhruv Patel*
*Template: derived from root `STORY-STEPS.md` — ING-specific constraints, branch naming, deliberation state, and reference table added*
