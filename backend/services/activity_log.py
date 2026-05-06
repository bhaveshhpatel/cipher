"""
services/activity_log.py  [STORY-BE-001]

Two public coroutines:
  log_action()  — insert one audit row into admin_activity_log.
                  Errors are swallowed at WARNING level so no admin route
                  ever fails due to a logging hiccup.
                  The insert *is* awaited before the response returns
                  (safe trade-off for low-frequency admin routes).

  fetch_logs()  — paginated read used by GET /api/admin/activity-log.
                  Returns (rows, total) so callers know the full result-set
                  size without a separate COUNT query.
"""
import asyncio
import logging
from typing import Any

from supabase import create_client
from config import settings

log = logging.getLogger("activity_log")


# ---------------------------------------------------------------------------
# Internal sync helpers (run in thread-pool via run_in_executor)
# ---------------------------------------------------------------------------

def _insert(email: str, action: str, detail: dict, ip: str | None) -> None:
    sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    sb.table("admin_activity_log").insert({
        "admin_email": email,
        "action":      action,
        "detail":      detail,
        "ip_address":  ip,
    }).execute()


def _query(
    limit: int,
    offset: int,
    action_filter: str | None,
    email_filter: str | None,
    since: str | None,
    before: str | None,
) -> tuple[list[dict], int]:
    """
    Return (rows, total_matching_rows).
    Uses PostgREST count='exact' so the client can paginate without a
    separate COUNT(*) round-trip.
    """
    sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    q = (
        sb.table("admin_activity_log")
        .select("*", count="exact")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if action_filter:
        q = q.eq("action", action_filter)
    if email_filter:
        q = q.eq("admin_email", email_filter)
    if since:
        q = q.gte("created_at", since)
    if before:
        q = q.lte("created_at", before)
    result = q.execute()
    return result.data or [], result.count or 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def log_action(
    email: str,
    action: str,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """
    Persist one audit row.  Errors are swallowed at WARNING level — the
    insert is awaited before the response returns but will never propagate
    an exception to the caller.
    """
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _insert, email, action, detail or {}, ip)
        log.debug("[activity_log] logged action=%s by %s", action, email)
    except Exception as exc:
        log.warning("[activity_log] Failed to persist log entry: %s", exc)


async def fetch_logs(
    limit: int = 50,
    offset: int = 0,
    action_filter: str | None = None,
    email_filter: str | None = None,
    since: str | None = None,
    before: str | None = None,
) -> tuple[list[dict], int]:
    """
    Return (rows, total) — rows are the current page, total is the full
    matching row count across all pages.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _query, limit, offset, action_filter, email_filter, since, before
    )
