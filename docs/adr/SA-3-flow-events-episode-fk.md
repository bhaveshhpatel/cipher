# ADR SA-3 — FK Gap: flow_events → flow_episodes

**Status:** Accepted  
**Date:** 2026-05-24  
**Deciders:** Software Architect (SA), Principal Backend Engineer (PBE)  
**Branch:** `fix/sa-3-flow-events-episode-fk`  
**Migration:** `supabase/migrations/035_sa3_flow_events_episode_fk.sql`

---

## Context

The DB audit (sprint `fix/db-audit-remediation`) identified that `flow_events` has no foreign key referencing `flow_episodes`, despite every episode being conceptually composed of one or more flow events. This gap means:

- An `episode_id` stored on a `flow_events` row (if it existed) could reference a deleted or non-existent episode with no DB-level enforcement.
- Query joins between the two tables rely entirely on application-layer correctness — a ticker/contract match by convention, not constraint.
- Orphaned episodes (no contributing events) and orphaned events (below-gate ticks with no episode) are both valid states but are not formally declared anywhere.

### Write Path Architecture

The two tables are written in different async coroutines with different lifecycle rules:

```
Tradier tick
    │
    ▼
_process_trade()
    ├─► persist_flow_event()      ← writes EVERY classified tick
    │     (batched, 500 ms flush)
    │
    └─► [Signal Gate check]
          │
          ├─ PASS ─► persist_flow_episode()   ← writes/merges episode
          │            (ING-009 upsert logic)
          │
          └─ FAIL ─► (no episode written)
```

Key implication: **a flow_event row can legitimately exist with no corresponding flow_episodes row.** Below-gate ticks are persisted for audit and backtest purposes but never trigger an episode. A hard NOT NULL FK would make every below-gate tick insert fail.

### episode_id Availability at Write Time

Even for above-gate ticks, `episode_id` is not available synchronously:

- `persist_flow_event()` is called first, in a batched flush, with no knowledge of whether an episode will be created.
- `persist_flow_episode()` is called after, and the Postgres-generated `id` (bigserial) is only known after the INSERT returns.
- The ING-009-RACE fix (per-contract asyncio.Lock) ensures only one episode INSERT occurs per merge window, but the event flush may have already committed before the episode row's `id` is known.

A synchronous write order that resolves the ID before the event flush would require restructuring the hot path — rejected as too invasive for a constraint-only fix.

---

## Decision

**Add `flow_events.episode_id` as a nullable UUID FK with `ON DELETE SET NULL` and `ON UPDATE CASCADE`.** Do not enforce NOT NULL. Do not add an application-layer pre-flight to resolve the ID synchronously.

This is **intentional partial denormalization**: the relationship is declared and enforced where it exists, but the absence of a link (NULL) is a first-class valid state, not a data quality problem.

### Why Not: Hard FK (NOT NULL)

| Concern | Impact |
|---|---|
| Below-gate ticks have no episode | Every below-gate `persist_flow_event()` call would raise a FK violation — massive ingest breakage |
| Episode ID not known at event flush time | Would require synchronous episode-first write ordering — restructures the ING-009 hot path |
| Pre-existing rows (all of them) have no episode_id | Migration itself would fail unless ALL existing rows were backfilled first |

### Why Not: ON DELETE CASCADE

Cascading deletes would silently destroy raw tick records when an episode is pruned by a retention policy. `flow_events` is the primary audit log — it must survive episode lifecycle events. `ON DELETE SET NULL` preserves the event row and sets `episode_id = NULL`, making the unlink explicit and queryable.

### Why Not: Document as Pure Denormalization (No FK)

Not adding the FK at all leaves future writers free to store any UUID in `episode_id` with no enforcement. The FK buys:
- Referential integrity for the linked subset (above-gate ticks that do have an episode)
- Automatic NULL-out when an episode is deleted (vs. dangling UUID)
- Query planner statistics on the join path

The cost (nullable column, partial index) is low. The FK is worth adding.

---

## Implementation

### Migration 035

Four steps, all idempotent (`IF NOT EXISTS` guards):

1. `ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS episode_id UUID` — nullable, no default.
2. FK constraint `fk_flow_events_episode_id` with `ON DELETE SET NULL ON UPDATE CASCADE`.
3. Partial index `idx_flow_events_episode_id WHERE episode_id IS NOT NULL` — keeps index lean during pre-backfill window.
4. Composite index `idx_flow_events_contract_episode_lookup` on `(ticker, contract_type, strike, expiry, created_at DESC) WHERE episode_id IS NULL` — supports the offline backfill query and future join scans.

### Application Layer (flow_store.py)

`persist_flow_event()` is updated to write `episode_id` when it is present in the event dict. The column is omitted from the row dict if not present (PostgREST ignores missing keys). No structural change to the write path — the FK column is additive.

`persist_flow_episode()` is **not** modified. The episode INSERT already returns the generated `id` via `Prefer: return=representation`. A future enhancement (ING-010 or similar) can plumb the returned `id` back into subsequent event rows for the same tick, but that is out of scope for SA-3.

### Offline Backfill Plan

Run during a maintenance window (post-market hours), on the production DB:

```sql
-- Match events to their nearest episode by contract identity within 30 min.
UPDATE flow_events fe
SET episode_id = (
  SELECT ep.id
  FROM flow_episodes ep
  WHERE ep.ticker        = fe.ticker
    AND ep.contract_type = fe.contract_type
    AND ep.strike        = fe.strike
    AND ep.expiry        = fe.expiry
    AND ep.created_at >= fe.created_at
    AND ep.created_at <= fe.created_at + INTERVAL '30 minutes'
  ORDER BY ep.created_at ASC
  LIMIT 1
)
WHERE fe.episode_id IS NULL;
```

Rows that remain NULL after this pass are intentionally unlinked (below-gate ticks). Do not delete them. Run `ANALYZE flow_events` after the backfill to refresh planner stats.

**Estimated backfill scope:** All rows in `flow_events` as of migration deployment. The query is a correlated subquery — run in batches of 50,000 rows if the table is large to avoid long-running transaction lock conflicts:

```sql
-- Batched version (run repeatedly until 0 rows updated)
UPDATE flow_events fe
SET episode_id = (
  SELECT ep.id
  FROM flow_episodes ep
  WHERE ep.ticker        = fe.ticker
    AND ep.contract_type = fe.contract_type
    AND ep.strike        = fe.strike
    AND ep.expiry        = fe.expiry
    AND ep.created_at >= fe.created_at
    AND ep.created_at <= fe.created_at + INTERVAL '30 minutes'
  ORDER BY ep.created_at ASC
  LIMIT 1
)
WHERE fe.episode_id IS NULL
  AND fe.id IN (
    SELECT id FROM flow_events
    WHERE episode_id IS NULL
    LIMIT 50000
  );
```

---

## Invariants (Post-Migration)

| Invariant | Enforcement |
|---|---|
| `flow_events.episode_id` is NULL or references a valid `flow_episodes.id` | FK constraint |
| Deleting an episode sets `episode_id = NULL` on child events, not deletes them | `ON DELETE SET NULL` |
| Below-gate ticks always have `episode_id IS NULL` | Application convention (not enforced by DB) |
| Above-gate ticks have `episode_id` set if the episode INSERT completed before the event flush | Application best-effort; NULL is valid if the timing race is lost |
| `episode_id` is immutable once set | Application convention — no UPDATE path exists in `persist_flow_event()` |

---

## Out of Scope (Follow-up)

- **ING-010:** Plumb `episode_id` returned from `persist_flow_episode()` back into the event row for the triggering tick (synchronous two-phase write). Would increase the percentage of non-NULL `episode_id` rows from the application side.
- **Retention policy:** Define when episodes (and by extension their event links) are pruned. A future ADR should specify the `ON DELETE SET NULL` behavior in the context of that policy.
- **Reverse link:** `flow_episodes` has no array/aggregate of its constituent `flow_events`. Consider a DB view or materialized query for analytics — out of scope here.
