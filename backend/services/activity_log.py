"""
services/activity_log.py  [STORY-BE-001]

Two public coroutines:
  log_action()  — fire-and-forget insert into admin_activity_log.
                  Never raises; failures are logged at WARNING level only,
                  so a DB hiccup never breaks the calling admin route.

  fetch_logs()  — paginated read used by GET /api/admin/activity-log.
"""
import asyncio
import logging
from typing import Any

log = logging.getLogger("activity_log")


# ---------------------------------------------------------------------------
# Internal sync helpers (run in thread-pool via run_in_executor)
# ---------------------------------------------------------------------------

def _insert(email: str, action: str, detail: dict, ip: str | None) -> None:
    from supabase import create_client
    from config import settings
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
) -> list[dict]:
    from supabase import create_client
    from config import settings
    sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    q = (
        sb.table("admin_activity_log")
        .select("*")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if action_filter:
        q = q.eq("action", action_filter)
    if email_filter:
        q = q.eq("admin_email", email_filter)
    return q.execute().data or []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def log_action(
    email: str,
    action: str,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Persist one audit row.  Never raises — failures are silently swallowed."""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _insert, email, action, detail or {}, ip)
        log.debug("[activity_log] logged action=%s by %s", action, email)
    except Exception as exc:
        log.warning("[activity_log] Failed to persist log entry: %s", exc)


async def fetch_logs(
    limit: int = 50,
    offset: int = 0,
    action_filter: str | None = None,
    email_filter: str | None = None,
) -> list[dict]:
    """Return paginated activity log rows, newest first."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _query, limit, offset, action_filter, email_filter)
