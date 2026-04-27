"""
conftest.py — shared pytest fixtures.

Key fix (2026-04-27):
  Python 3.10+ deprecated asyncio.get_event_loop() auto-creation and
  Python 3.12 removed it entirely.  Many legacy test files call
  asyncio.get_event_loop().run_until_complete(...) as a _run() helper.
  To fix all of them without rewriting every file, we:
    1. Create a new event loop for every test session and install it as
       the current loop BEFORE any test module is imported.
    2. Provide an `event_loop` fixture that pytest-asyncio uses for all
       async tests, scoped to the session so the loop is never closed
       between tests.
"""
import asyncio
import pytest


# ---------------------------------------------------------------------------
# Install a running event loop immediately at import time so that any
# module-level `asyncio.get_event_loop()` call in test files resolves
# correctly rather than raising RuntimeError.
# ---------------------------------------------------------------------------
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped event loop fixture consumed by pytest-asyncio.

    Using a session scope means the same loop is alive for the entire
    test run.  asyncio.get_event_loop() therefore always returns a live
    loop, which is what legacy _run = get_event_loop().run_until_complete
    helpers rely on.
    """
    loop = asyncio.get_event_loop_policy().get_event_loop()
    yield loop
    # Do NOT close the loop here; pytest-asyncio handles cleanup.


@pytest.fixture(autouse=True)
def _reset_supabase_env(monkeypatch):
    """Ensure Supabase env vars are absent unless a test sets them."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    yield
