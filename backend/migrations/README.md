# Cipher — Database Migrations

All schema changes live here as numbered SQL files. They are applied in numeric order and are idempotent (every statement uses `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` where applicable).

## Migration files

| # | File | Description |
|---|------|-------------|
| 001 | `001_options_universe.sql` | Core options universe tables |
| 002 | `002_universe_symbols_quotes.sql` | Quotes cache for universe symbols |
| 003 | `003_signal_history.sql` | Signal history table |
| 004 | `004_swarm_fields.sql` | Swarm signal fields |
| 005 | `005_signal_history_repair.sql` | Signal history schema repair |
| 006 | `006_flow_tables_rls.sql` | Flow event tables + RLS policies |
| 007 | `007_seed_data.sql` | Seed / reference data |
| 008 | `008_flow_events_expiry_nullable.sql` | Make expiry column nullable |
| 009 | `009_flow_events_synthetic_quote.sql` | `is_synthetic_quote` column (C-018 fix) |
| 010 | `010_add_tier_and_oi_to_universe.sql` | `tier`, `open_interest`, `average_volume` on `options_universe_symbols` (Feature 4A) |
| 011 | `011_add_tier_thresholds.sql` | `tier_thresholds` admin table with default active row (Feature 4A) |

## Running migrations

### Via Supabase MCP / dashboard (recommended for production)
Migrations 010 and 011 are already applied to `cipher-database` (verified 2026-04-24).
The Supabase dashboard → Database → Migrations shows the full applied history.

### Via the migration runner script (local / CI)

```bash
# From repo root
cd backend

export SUPABASE_URL="https://<your-ref>.supabase.co"
export SUPABASE_SERVICE_KEY="<service-role-key>"

python -m migrations.run_migrations

# Dry-run (shows what would be applied, no DB changes)
python -m migrations.run_migrations --dry-run
```

The script:
1. Reads all `NNN_*.sql` files in this directory, sorted numerically.
2. Queries `supabase_migrations.schema_migrations` to find already-applied versions.
3. Applies only unapplied migrations in order.
4. Exits `0` on success, `1` on any failure.

### Adding a new migration

1. Create `NNN_description.sql` where `NNN` is the next number (e.g. `012_...`).
2. Use `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` to keep it idempotent.
3. Test locally with `--dry-run` first.
4. Apply via the runner or directly in the Supabase dashboard.
5. Commit both the `.sql` file and any corresponding model changes together.

## Schema state (as of 2026-04-24)

### `options_universe_symbols` (key columns)
| Column | Type | Default | Added in |
|--------|------|---------|----------|
| `tier` | `SMALLINT` | `3` | 010 |
| `open_interest` | `INT` | `NULL` | 010 |
| `average_volume` | `INT` | `NULL` | 010 |

### `tier_thresholds`
| Column | Description |
|--------|-------------|
| `t1_*` | Tier 1 (liquid large-caps): vol ≥ 20M, ATM ±20%, DTE ≤ 90 |
| `t2_*` | Tier 2 (mid-cap): vol ≥ 2M, ATM ±15%, DTE ≤ 60 |
| `t3_*` | Tier 3 (standard): vol ≥ 500K, ATM ±10%, DTE ≤ 30 |
| `is_active` | Only the `true` row is read by the backend |

Admin endpoints: `PATCH /admin/tier-thresholds`, `GET /admin/tier-distribution`
