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
| 012 | `012_tier_thresholds_rls.sql` | RLS enable + `authenticated` SELECT policy + `updated_at` trigger on `tier_thresholds` (B-019) |

## Running migrations

### Via Supabase MCP / dashboard (recommended for production)
Migrations 010–012 are already applied to `cipher-database` (verified 2026-04-24).
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

1. Create `NNN_description.sql` where `NNN` is the next number (e.g. `013_...`).
2. Use `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` to keep it idempotent.
3. Test locally with `--dry-run` first.
4. Apply via the runner or directly in the Supabase dashboard.
5. Commit both the `.sql` file and any corresponding model changes together.

## Schema state (as of 2026-04-25, post Feature 4A-OI)

### `options_universe_symbols` (key columns)
| Column | Type | Default | Added in | Notes |
|--------|------|---------|----------|-------|
| `tier` | `SMALLINT` | `3` | 010 | OI-informed classification since 4A-OI (2026-04-25) |
| `open_interest` | `INT` | `NULL` | 010 | Avg chain OI from `symbol_registry.build()` since 4A-OI |
| `average_volume` | `INT` | `NULL` | 010 | From Tradier quote at cold-start |

> **Backfill note (4A-OI, 2026-04-25):** The `open_interest` column was added in migration 010
> but was not populated until Feature 4A-OI shipped (2026-04-25). Any rows written before
> that date will have `open_interest = NULL`. The column will be populated on the next
> cold-start (tradier_validated path) or background 24h refresh. No manual backfill is
> required — the two-pass pipeline handles it automatically on the next startup.

### `tier_thresholds`
| Column | Description |
|--------|-------------|
| `t1_*` | Tier 1 (liquid large-caps): vol ≥ 20M, price ≥ $10, OI ≥ 1 000, ATM ±20%, DTE ≤ 90 |
| `t2_*` | Tier 2 (mid-cap): vol ≥ 2M, price ≥ $10, OI ≥ 500, ATM ±15%, DTE ≤ 60 |
| `t3_*` | Tier 3 (standard): vol ≥ 500K, price ≥ $1, OI ≥ 100, ATM ±10%, DTE ≤ 30 |
| `is_active` | Only the `true` row is read by the backend |
| `updated_at` | Auto-updated by trigger (migration 012) |

> **OI grace path removed (4A-OI):** Prior to 2026-04-25, `tier_engine._classify()` would
> promote symbols to T1/T2 based on vol+price alone when `open_interest == 0` (grace path).
> This path is permanently removed. All three conditions (vol + price + OI) must be satisfied
> for T1 or T2 classification. Symbols with `oi=0` always fall to T3.

RLS: service role has full access (Supabase default); `authenticated` users have SELECT (migration 012).

Admin endpoints:
- `GET  /api/admin/tier-thresholds` — read active row + cache metadata (B-019)
- `PATCH /api/admin/tier-thresholds` — update threshold columns, busts cache (Feature 4A)
- `GET  /api/admin/tier-distribution` — tier counts + samples for active snapshot (B-020)

## Feature 4A-OI implementation summary (2026-04-25)

No new migration was required for Feature 4A-OI. Migration 010 already created the
`open_interest` column. The feature work was entirely in application code:

| Chunk | File | Change |
|-------|------|--------|
| 1A | `services/symbol_registry.py` | `_oi_by_ticker` dict + `get_oi_map()` public method |
| 1B | `services/tier_engine.py` | Removed OI grace path from `_classify()` |
| 1C | `services/universe_store.py` | `open_interest` field in `_sync_upsert_symbol_quotes()` |
| 1D | `backend/main.py` | `_stamp_oi()` helper + two-pass OI re-tiering in `lifespan()` |
| 2A | `tests/test_4a_oi_pipeline.py` | 20 new tests: registry, classify, stamp, integration |
| 2B | `tests/test_4a_tier_engine.py` | TE-23–26: grace-path removal regression tests |
| 2C | `tests/test_universe_store.py` | US-OI-01–04: open_interest upsert assertion tests |
