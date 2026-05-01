# Cipher — Story Execution Protocol

> This document is the canonical checklist for executing every story in the Cipher APEX sprint.
> Read this file in full before starting any story. No exceptions.

---

## Rule 0 — Before Answering "What’s Next / What’s Remaining / What’s the Order"

**Both files must be consulted together. Neither alone is sufficient.**

1. Read **`docs/cipher_apex_story_and_sprint_plan.md`** first
   - This is the canonical spec
   - Contains: story scope, acceptance criteria, test requirements, architectural deliberation notes
   - Defines *what* a story requires and *why*

2. Read **`SPRINT.md`** second
   - This is the execution state tracker
   - Contains: what is merged (✅), what is blocked (🔴), what is queued (⏳), post-merge findings, exact build order
   - Defines *where things currently stand*

> GitHub Issues track execution state only. They are not the plan.
> Do not answer ordering or sequencing questions from Issues alone.

---

## Rule 1 — Check Gates Before Starting

Before touching any story:

1. Open `SPRINT.md` and identify the story’s position in the **Fully Ordered Build Sequence**
2. Confirm every story above it in the same gate block is marked ✅
3. If any 🔴 hard gate above is not merged — **stop**. Do not start the story. Report the blocker.
4. If the story is ⏳ (blocked) — **stop**. State what must merge first.

---

## Rule 2 — Read the Spec Before Writing Any Code

Before creating a branch:

1. Read the full story definition in `docs/cipher_apex_story_and_sprint_plan.md`
2. Identify: scope, acceptance criteria, test requirements, architectural notes
3. If the story has a panel deliberation note in the spec — internalize it; it was written for a reason
4. Cross-reference the story’s GitHub Issue for any post-filing amendments
5. Only then proceed to branch creation

---

## Rule 3 — Branch + PR Always

```
NEVER push story work directly to main.
Always: branch → commits → PR → deliberation → merge.
```

Branch naming convention:

| Story type | Branch name format |
|---|---|
| APEX sprint story | `apex/s{N}-{short-description}` |
| Post-merge fix | `apex/s{N}-post-{number}-{short-description}` |
| Parallel track | `track/{id}-{short-description}` |
| Hotfix | `hotfix/{short-description}` |

Steps:

1. `Create branch off main` — name per convention above
2. Make all story changes on that branch
3. Open a PR from the branch into `main`
4. PR body must include:
   - Story reference (e.g. "Closes #N")
   - What the PR does (scope summary)
   - Files changed
   - Acceptance criteria checklist
   - Test coverage checklist
   - Panel deliberation notes (see Rule 4)

---

## Rule 4 — Panel Deliberation on Every PR

Before any PR merges, all three roles must deliberate on the diff:

### Senior Architect
- Is the scope correct and complete per the spec?
- Are there missing columns, missing files, or scope gaps vs. `cipher_apex_story_and_sprint_plan.md`?
- Are architectural constraints respected (e.g. columns that cannot be backfilled retroactively)?
- Does the implementation match the deliberation notes already in the spec?

### Principal Backend Engineer
- Is the implementation correct? Any logic errors, edge cases, or unsafe defaults?
- Are constraints, types, and defaults exactly right?
- Will existing code break? Are all callers updated?
- Are there performance concerns (hot paths, missing indexes, unnecessary work)?

### Lead QA
- Are all acceptance criteria from the issue and spec covered?
- Is test coverage complete — happy path, default path, constraint violations, idempotency?
- Are there missing test cases that a future regression could silently break?
- Are integration tests gated on schema/state that doesn’t exist yet?

### Findings Resolution

| Finding type | Resolution |
|---|---|
| Small fix (typo, comment, non-logic change) | Fix inline on the PR before merge |
| Logic fix that fits in the same PR scope | Fix inline on the PR before merge |
| New work requiring a separate PR | File a **numbered GitHub Issue** AND add a row to `SPRINT.md` before merging |

> No finding lives only in conversation. If it needs tracking, it gets an issue AND a SPRINT.md row.
> Only after all findings are resolved does the PR merge.

---

## Rule 5 — Post-Merge Cleanup

Immediately after a PR merges:

1. **Close the GitHub Issue** that the story was tracking (state: `completed`)
2. **Update `SPRINT.md`**:
   - Mark the story row ✅ in the Completed table
   - Move it out of the gate block if applicable
   - Update the **Quick Reference** section ("What is next?", "What blocks S3?", etc.)
   - Add any new post-merge stories to the relevant gate block
   - Bump the version number and update the `Last updated` line
3. If new GitHub Issues were filed from panel findings — confirm they are open and linked in `SPRINT.md`

---

## Full Story Execution Checklist

Run through this list top-to-bottom for every story, without skipping steps.

```
PRE-FLIGHT
☐  Read docs/cipher_apex_story_and_sprint_plan.md for this story
☐  Read SPRINT.md — confirm all hard gates above this story are ✅
☐  Read the GitHub Issue for any post-filing amendments
☐  Confirm story is not ⏳ blocked

BRANCH
☐  Create branch off main (naming convention: apex/s{N}-{short-description})
☐  Confirm branch is off latest main SHA

IMPLEMENTATION
☐  Make all changes on the branch
☐  All acceptance criteria from the issue are addressed
☐  All test coverage requirements from the issue are addressed

PR
☐  Open PR from branch → main
☐  PR body includes: story ref, scope, files changed, AC checklist, test checklist

DELIBERATION
☐  Senior Architect reviews diff — scope, completeness, architectural constraints
☐  Principal Backend Engineer reviews diff — correctness, safety, performance
☐  Lead QA reviews diff — test coverage, constraint tests, regression gaps
☐  All small fixes applied inline on the PR
☐  All new-work findings filed as GitHub Issues AND added to SPRINT.md
☐  Zero unresolved findings remaining

MERGE
☐  Squash merge into main

POST-MERGE
☐  Close the GitHub Issue (state: completed)
☐  Mark row ✅ in SPRINT.md
☐  Update Quick Reference section in SPRINT.md
☐  Add any new post-merge issue rows to SPRINT.md
☐  Bump SPRINT.md version + Last updated line
```

---

## Reference

| File | Purpose |
|---|---|
| `docs/cipher_apex_story_and_sprint_plan.md` | Canonical spec — story definitions, AC, test requirements, architecture notes |
| `SPRINT.md` | Execution state — merged/open/blocked status, build order, gate tracking |
| `STORY-STEPS.md` | This file — the protocol every story execution must follow |
| GitHub Issues | Execution tracking only — not the plan, not the source of truth for ordering |
