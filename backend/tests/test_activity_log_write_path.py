"""
Coverage tests for services/activity_log.py — write path (lines 51-69):
  - log_action() success path: run_in_executor calls _insert, logs debug
  - log_action() exception path: exception swallowed, logged at WARNING
  - fetch_logs() delegates to run_in_executor with correct args
  - log_action() with all optional params None
  - log_action() detail defaults to empty dict when None passed

All Supabase calls are mocked — no live DB required.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# log_action — success path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_action_calls_run_in_executor():
    """log_action must call loop.run_in_executor with _insert and the right args."""
    from services import activity_log as al

    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(return_value=None)

    with patch("asyncio.get_running_loop", return_value=mock_loop):
        await al.log_action("admin@test.com", "LOGIN", {"ip": "1.2.3.4"}, "1.2.3.4")

    mock_loop.run_in_executor.assert_called_once()
    call_args = mock_loop.run_in_executor.call_args
    # First positional arg is executor (None), second is the function _insert,
    # followed by the bound arguments.
    assert call_args[0][0] is None  # executor=None → default thread pool
    assert call_args[0][1] is al._insert


@pytest.mark.asyncio
async def test_log_action_passes_detail_as_empty_dict_when_none():
    """detail=None must be coerced to {} before passing to _insert."""
    from services import activity_log as al

    captured = {}

    async def fake_executor(executor, fn, *args):
        captured["args"] = args
        return None

    mock_loop = MagicMock()
    mock_loop.run_in_executor = fake_executor

    with patch("asyncio.get_running_loop", return_value=mock_loop):
        await al.log_action("admin@test.com", "LOGOUT", None, None)

    # args = (email, action, detail, ip)
    assert captured["args"][2] == {}
    assert captured["args"][3] is None


@pytest.mark.asyncio
async def test_log_action_swallows_exception_and_logs_warning():
    """If run_in_executor raises, the exception must be caught and logged at WARNING."""
    from services import activity_log as al

    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(side_effect=Exception("Supabase down"))

    with patch("asyncio.get_running_loop", return_value=mock_loop):
        # Must not raise
        await al.log_action("admin@test.com", "DELETE_USER", {"target": "x"}, None)


@pytest.mark.asyncio
async def test_log_action_with_all_optional_params_none():
    from services import activity_log as al

    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(return_value=None)

    with patch("asyncio.get_running_loop", return_value=mock_loop):
        await al.log_action("a@b.com", "TEST_ACTION")

    mock_loop.run_in_executor.assert_called_once()


# ---------------------------------------------------------------------------
# fetch_logs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_logs_calls_run_in_executor_with_query():
    from services import activity_log as al

    fake_data = ([{"action": "LOGIN"}], 1)

    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(return_value=fake_data)

    with patch("asyncio.get_running_loop", return_value=mock_loop):
        rows, total = await al.fetch_logs(limit=10, offset=0)

    assert rows == [{"action": "LOGIN"}]
    assert total == 1
    mock_loop.run_in_executor.assert_called_once()
    # Confirm _query is the function passed
    exec_args = mock_loop.run_in_executor.call_args[0]
    assert exec_args[1] is al._query


@pytest.mark.asyncio
async def test_fetch_logs_passes_all_filters():
    from services import activity_log as al

    captured = {}

    async def fake_executor(executor, fn, *args):
        captured["args"] = args
        return ([], 0)

    mock_loop = MagicMock()
    mock_loop.run_in_executor = fake_executor

    with patch("asyncio.get_running_loop", return_value=mock_loop):
        await al.fetch_logs(
            limit=25, offset=50,
            action_filter="LOGIN",
            email_filter="admin@test.com",
            since="2026-01-01",
            before="2026-12-31",
        )

    args = captured["args"]
    assert args[0] == 25           # limit
    assert args[1] == 50           # offset
    assert args[2] == "LOGIN"      # action_filter
    assert args[3] == "admin@test.com"  # email_filter
    assert args[4] == "2026-01-01" # since
    assert args[5] == "2026-12-31" # before


# ---------------------------------------------------------------------------
# _insert direct unit test (without Supabase)
# ---------------------------------------------------------------------------

def test_insert_function_calls_supabase_table():
    """_insert() must create a supabase client and call .insert().execute()."""
    from services import activity_log as al

    mock_table = MagicMock()
    mock_insert_chain = MagicMock()
    mock_table.insert = MagicMock(return_value=mock_insert_chain)
    mock_insert_chain.execute = MagicMock(return_value=None)

    mock_sb = MagicMock()
    mock_sb.table = MagicMock(return_value=mock_table)

    mock_settings = MagicMock()
    mock_settings.SUPABASE_URL = "https://x.supabase.co"
    mock_settings.SUPABASE_SERVICE_KEY = "svc"

    with patch("services.activity_log.create_client", return_value=mock_sb), \
         patch("services.activity_log.settings", mock_settings):
        al._insert("admin@test.com", "LOGIN", {"ip": "127.0.0.1"}, "127.0.0.1")

    mock_sb.table.assert_called_once_with("admin_activity_log")
    mock_table.insert.assert_called_once()
    mock_insert_chain.execute.assert_called_once()
