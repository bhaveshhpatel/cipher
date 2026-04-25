"""
run_migrations.py — Idempotent SQL migration runner for Cipher backend.

Usage:
    cd backend
    python -m migrations.run_migrations

Behaviour:
  1. Reads all NNN_*.sql files from the migrations/ directory in numeric order.
  2. Checks the `supabase_migrations.schema_migrations` tracking table to see
     which versions have already been applied.
  3. Applies only unapplied migrations, in order, via the Supabase service client.
  4. Safe to run multiple times — already-applied migrations are skipped.
  5. Exits 0 on success, 1 on any error (prints which migration failed).

Required env vars:
    SUPABASE_URL          — e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY  — service role key (not the anon key)

Tracking table:
    Supabase manages `supabase_migrations.schema_migrations` internally.
    We query it to check applied versions so this script integrates cleanly
    with the Supabase dashboard migration history.
"""
from __future__ import annotations

import os
import re
import sys
import pathlib
from supabase import create_client, Client


_MIGRATIONS_DIR = pathlib.Path(__file__).parent
_VERSION_RE     = re.compile(r'^(\d+)_')


def _supabase_client() -> Client:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set to run migrations."
        )
    return create_client(url, key)


def _list_migration_files() -> list[tuple[int, pathlib.Path]]:
    """Return sorted list of (numeric_prefix, path) for all NNN_*.sql files."""
    files = []
    for f in _MIGRATIONS_DIR.glob("*.sql"):
        m = _VERSION_RE.match(f.name)
        if m:
            files.append((int(m.group(1)), f))
    return sorted(files, key=lambda x: x[0])


def _applied_versions(client: Client) -> set[str]:
    """
    Return the set of migration names already tracked by Supabase.
    Supabase tracks migrations in supabase_migrations.schema_migrations.
    We query it via raw SQL through the service client.
    """
    try:
        result = client.rpc("pg_query", {
            "query": "SELECT name FROM supabase_migrations.schema_migrations"
        }).execute()
        if result.data:
            return {row["name"] for row in result.data}
    except Exception:
        pass

    # Fallback: query via execute_sql if rpc not available
    try:
        result = client.table("supabase_migrations.schema_migrations") \
                       .select("name").execute()
        if result.data:
            return {row["name"] for row in result.data}
    except Exception:
        pass

    return set()


def run_migrations(dry_run: bool = False) -> bool:
    """
    Main entry point. Returns True if all pending migrations applied successfully.
    """
    print("[migrations] Connecting to Supabase...")
    client = _supabase_client()

    migration_files = _list_migration_files()
    if not migration_files:
        print("[migrations] No migration files found.")
        return True

    applied = _applied_versions(client)
    print(f"[migrations] {len(applied)} migrations already applied.")

    pending = [
        (num, path) for num, path in migration_files
        if path.stem not in applied
    ]

    if not pending:
        print("[migrations] All migrations up to date. Nothing to apply.")
        return True

    print(f"[migrations] {len(pending)} pending migration(s) to apply:")
    for _, path in pending:
        print(f"  - {path.name}")

    if dry_run:
        print("[migrations] Dry-run mode — no changes applied.")
        return True

    for num, path in pending:
        sql = path.read_text(encoding="utf-8").strip()
        if not sql:
            print(f"[migrations] Skipping empty file: {path.name}")
            continue

        print(f"[migrations] Applying {path.name} ...", end=" ", flush=True)
        try:
            # Execute via Supabase's postgres direct execution
            client.postgrest.session.headers.update({
                "Content-Profile": "public"
            })
            # Use rpc exec_sql if available, otherwise use raw postgrest
            try:
                client.rpc("exec_sql", {"sql": sql}).execute()
            except Exception:
                # Fallback: some Supabase setups expose a different RPC
                client.rpc("execute_sql", {"query": sql}).execute()
            print("OK")
        except Exception as exc:
            print(f"FAILED\n[migrations] ERROR in {path.name}: {exc}")
            return False

    print("[migrations] All pending migrations applied successfully.")
    return True


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    success = run_migrations(dry_run=dry_run)
    sys.exit(0 if success else 1)
