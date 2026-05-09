# REARCH-010 Migration Runbooks

> **RUNBOOK ONLY** — These staged files are for **manual, step-by-step execution** against staging or production with intermediate verification between each stage.
>
> **The canonical CI pipeline migration is:**
> ```
> backend/migrations/024_rearch010_schema_purge.sql
> ```
> That file is what `supabase db push` (or the equivalent CI runner) applies automatically. **Do not add the staged files below to the CI pipeline.** Running both would be safe (all statements are `IF EXISTS` / `IF NOT EXISTS` guarded and the backfill UPDATEs are idempotent), but it would create unnecessary noise and confusion in the migration log.

---

## File Index

| File | Stage | What it does |
|---|---|---|
| `01_backfill_signal_history_alert_level.sql` | 1 | Backfills `alert_level` from old vocab (CONVICTION/WHALE/…) to REARCH vocab (WATCH/NOTEWORTHY/BLOCK/GOLDEN) |
| `02_backfill_signal_history_direction.sql` | 2 | Backfills `direction` from BUY/SELL/HOLD → BULLISH/BEARISH/NEUTRAL |
| `03_drop_deprecated_tables.sql` | 3 | Drops `gate_config_audit`, `gate_configs`, `backtest_results` |
| `04_drop_deprecated_columns.sql` | 4 | Drops retired columns from `flow_events`, `flow_episodes`, `signal_history` (including `flow_score`) |
| `05_update_check_constraints.sql` | 5 | Drops old `alert_level` / `direction` CHECK constraints, adds REARCH-vocab constraints |
| `06_add_steamroom_columns.sql` | 6 | Adds Steamroom + snapshot columns to `flow_episodes` and `signal_history` |

---

## When to use the staged files

Use these when you need to:
- Apply the migration to **production manually** with a verification pause between each stage
- Debug a failed CI migration by re-running only the affected stage
- Review the exact SQL for a single section in isolation without reading through the full 024 file

Each staged file is wrapped in its own `BEGIN; ... COMMIT;` transaction and includes a `POST-STAGE VERIFICATION` block at the end with advisory queries to confirm the stage completed correctly.

---

## Execution order

Stages **must be run in numeric order** (01 → 06). The backfills in stages 1–2 must complete before the CHECK constraints are added in stage 5, or the constraint addition will fail on rows with old-vocab values.

---

## D2 QA sign-off requirement

Before merging PR #119, PBE must run the migration against staging and post the output of the following queries as a QA comment on the PR:

```sql
-- After stages 1 & 2 complete:
SELECT alert_level, COUNT(*) FROM signal_history GROUP BY alert_level ORDER BY alert_level;
SELECT direction,   COUNT(*) FROM signal_history GROUP BY direction   ORDER BY direction;

-- Expected: only WATCH / NOTEWORTHY / BLOCK / GOLDEN in alert_level
-- Expected: only BULLISH / BEARISH / NEUTRAL in direction
-- Expected: 0 rows returned by:
SELECT COUNT(*) FROM signal_history
WHERE alert_level NOT IN ('WATCH','NOTEWORTHY','BLOCK','GOLDEN')
   OR direction   NOT IN ('BULLISH','BEARISH','NEUTRAL');
```

The QA comment output is the merge sign-off for D2.
