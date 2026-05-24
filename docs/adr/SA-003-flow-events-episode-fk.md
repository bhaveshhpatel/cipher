# ADR SA-003 — FK gap: `flow_events` → `flow_episodes`

| Field | Value |
|---|---|
| **ID** | SA-003 |
| **Status** | Accepted |
| **Deciders** | Backend Eng, Software Architect |
| **Date** | 2026-05-24 |
| **Ticket** | DB Audit — SA-3 |
| **Branch** | `fix/sa-3-flow-events-episode-fk` |

---

## Context

`flow_events` (UUID PK, created migration 006) and `flow_episodes` (BIGSERIAL PK, created migration 006) were written independently from the start. The ingestion path (`flow_store.py`) always persisted episodes first and events second, but no FK column ever connected the two tables. The result:

- No relational integrity enforcement — a `flow_event` row could reference a non-existent episode silently.
- No join path from an event back to its episode without a full cross-table scan on `(ticker, contract_type, strike, expiry, created_at)`.
- Query patterns on the frontend (grouping events by episode, episode drill-down) rely on application-level filtering instead of indexed FK lookup.

Rows written before ING-009 (~2026-05-06) were emitted _before_ episode upsert was stabilised. Those rows have no reliable episode match and must remain `NULL` after backfill — this is acceptable and documented below.

---

## Decision

**Add `flow_events.episode_id BIGINT REFERENCES flow_episodes(id) ON DELETE SET NULL`, nullable, with a partial index and a backfill migration.**

The FK is nullable (not `NOT NULL`) because:

1. Pre-ING-009 rows cannot be backfilled reliably — ambiguous candidates exist.
2. Rare ingest edge cases (episode lock timeout, DB transient error) may produce an event with no episode; dropping the event is worse than storing it without an episode link.

The column was **not** made `NOT NULL DEFAULT NULL` with a deferred enforcement date — that path creates a hidden time bomb when the ingest path is under load. Nullable FK with `ON DELETE SET NULL` is the correct semantic for an append-only audit table.

---

## Alternatives Rejected

### A. Intentional Denormalization (no FK, document only)

Rejected. The data model already carries all merge-key fields on both tables (`ticker`, `contract_type`, `strike`, `expiry`). Keeping no FK means every join is a 5-column equality check with no PG constraint to catch referential drift. The only benefit — avoiding a migration — is outweighed by long-term query complexity.

### B. Strict `NOT NULL` FK from day one of this migration

Rejected. Approximately 80–90% of existing `flow_events` rows pre-date ING-009 and cannot be backfilled without fabricating episode rows. A `NOT NULL` constraint would require either:
- Fabricating stub episode rows (violates data integrity).
- Deleting old event rows (violates audit requirements).
- A phased migration with a deferred `NOT NULL` constraint (adds risk, no meaningful gain given the NULL policy below).

### C. Store `episode_id` only in `_episode_in_flight` cache, never persist on event row

Rejected. In-flight cache is lost on worker restart. The FK is the only durable link.

---

## FK Semantics

| Property | Value | Rationale |
|---|---|---|
| Column type | `BIGINT` | Matches `flow_episodes.id` (BIGSERIAL) |
| Nullable | Yes | Pre-ING-009 rows + rare ingest edge cases |
| `ON DELETE` | `SET NULL` | Episode deletion preserves event audit trail |
| `ON UPDATE` | (default `NO ACTION`) | `flow_episodes.id` is immutable bigserial |
| Deferred | `DEFERRABLE INITIALLY DEFERRED` | Allows same-transaction insert ordering |
| Index | Partial on `episode_id WHERE episode_id IS NOT NULL` | NULL rows excluded; non-NULL rows indexed for join |

---

## Backfill Plan

The backfill is executed inside migration `027_sa3_flow_events_episode_fk.sql` as a single-pass `UPDATE … FROM CTE`:

1. For each `flow_event` with `episode_id IS NULL`, the CTE finds all `flow_episodes` matching on `(ticker, contract_type, strike, expiry)` within a ±30-minute window of the event's `created_at`.
2. Only **unambiguous matches** (exactly one candidate episode) are written. If zero or two or more candidates match, the event row retains `NULL`.
3. The backfill is idempotent — rows already set are excluded by the `WHERE episode_id IS NULL` guard.

### Expected coverage

| Row cohort | Expected outcome |
|---|---|
| Pre-ING-009 rows (before ~2026-05-06) | Mostly `NULL` — episode rows did not exist or are unreliable |
| Post-ING-009 single-episode ticks | ~95% matched (unambiguous window) |
| Post-ING-009 multi-episode overlap | `NULL` (ambiguous — correct behavior) |

---

## NULL Policy (Formal)

`flow_events.episode_id IS NULL` is **not an error condition**. It means one of:

- The event was written before ING-009 (expected, permanent).
- The episode insert failed transiently (event preserved; episode link deferred to next run).
- The event belongs to an ambiguous window where two episodes overlapped for the same contract.

Callers must treat `NULL episode_id` as "episode unknown" and handle gracefully. **Never filter out `NULL` rows as invalid.**

---

## Application Changes

### `flow_store.py`

`persist_flow_event()` gains an `episode_id: Optional[int]` parameter. When present, it is written into the `flow_events` row dict. The value is sourced from `_episode_in_flight` (populated by `_insert_rows_with_episode_id()` reading the PostgREST `return=representation` response).

Call order within `persist_flow_episode()` / `_process_trade()` is unchanged — episode is persisted first, episode id is stored in `_episode_in_flight`, then `persist_flow_event()` is called with that id.

### Query patterns enabled post-migration

```sql
-- All events for a specific episode
SELECT * FROM flow_events WHERE episode_id = $1;

-- Episode with its event count
SELECT ep.*, COUNT(fe.id) AS event_count
FROM flow_episodes ep
LEFT JOIN flow_events fe ON fe.episode_id = ep.id
WHERE ep.id = $1
GROUP BY ep.id;
```

---

## Consequences

**Positive**
- Relational integrity between the two core flow tables is enforced by the DB engine.
- Frontend episode drill-down queries go from 5-column equality scan to indexed `episode_id` lookup.
- Monitoring can trivially flag events with no episode link (leading indicator of ingest health).

**Negative / Accepted**
- ~50–70% of historical rows will remain `NULL` on `episode_id` permanently. Dashboards and queries must handle this.
- `persist_flow_event()` signature change: callers that construct the dict manually (tests) must be updated to not pass unknown keys, or must pass `episode_id=None` explicitly.
- The `DEFERRABLE INITIALLY DEFERRED` FK adds a small per-transaction deferred constraint check. Not measurable at current row rates.

---

## Migration Execution

```bash
# Run via the project migration runner
python backend/migrations/run_migrations.py --file 027_sa3_flow_events_episode_fk.sql

# Or directly in Supabase SQL editor — paste contents of 027_sa3_flow_events_episode_fk.sql
# Safe to run multiple times (idempotent ADD COLUMN guard + IF NOT EXISTS index)
```

**Pre-run checklist**
- [ ] Confirm no active ingest worker is running (or run during maintenance window; the migration is non-blocking for reads)
- [ ] Run `SELECT COUNT(*) FROM flow_events WHERE episode_id IS NOT NULL` before and after to validate backfill coverage
- [ ] Run `EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM flow_events WHERE episode_id = 1` to confirm index is used
