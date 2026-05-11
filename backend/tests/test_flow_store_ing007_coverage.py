"""
Coverage tests for ING-007 additions in services/flow_store.py:
  - enqueue_lookback() -- normal path, queue-full overflow path
  - get_lookback_stats() -- returns dict copy with correct keys
  - start_lookback_worker() -- processes a key, handles get_lookback error,
    handles CancelledError shutdown, dte_tiers absent, dte_tiers traversal
  - _update_episode_multiday() -- GET 200+rows -> PATCH 200/204, GET non-200,
    GET empty rows, GET row with no id, PATCH non-200, exception path,
    not-configured short-circuit

All Supabase / httpx calls are mocked -- no live network required.

FIX (2026-05-05): _lookback_queue is a module-level asyncio.Queue created at
import time. When pytest-asyncio creates a new event loop per test, the queue
is still bound to the OLD loop causing RuntimeError. Fix: reset the queue
module attribute to a fresh Queue inside each async test that calls
start_lookback_worker, using asyncio.get_event_loop() to bind it correctly.

FIX (2026-05-11): assert_called_once_with for _update_episode_multiday must
use keyword-arg form to match the implementation which calls the function
with all keyword arguments.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# enqueue_lookback
# ---------------------------------------------------------------------------

def test_enqueue_lookback_increments_queued_counter():
    import services.flow_store as fs
    from utils.contract_day_cache import ContractKey

    fs._lookback_stats["lookback_queued"] = 0
    fs._lookback_stats["lookback_queue_overflow"] = 0
    while not fs._lookback_queue.empty():
        fs._lookback_queue.get_nowait()

    key = ContractKey("AAPL", "CALL", 150.0, "2026-06-20")
    fs.enqueue_lookback(key)
    assert fs._lookback_stats["lookback_queued"] >= 1


def test_enqueue_lookback_overflow_increments_overflow_counter():
    import services.flow_store as fs
    from utils.contract_day_cache import ContractKey

    fs._lookback_stats["lookback_queue_overflow"] = 0

    key = ContractKey("SPY", "PUT", 440.0, "2026-05-17")
    with patch.object(fs._lookback_queue, "put_nowait", side_effect=asyncio.QueueFull()):
        fs.enqueue_lookback(key)

    assert fs._lookback_stats["lookback_queue_overflow"] >= 1


def test_enqueue_lookback_never_raises():
    """enqueue_lookback must be fire-and-forget -- no exception propagated."""
    import services.flow_store as fs
    from utils.contract_day_cache import ContractKey

    key = ContractKey("TSLA", "CALL", 200.0, "2026-07-18")
    with patch.object(fs._lookback_queue, "put_nowait", side_effect=asyncio.QueueFull()):
        fs.enqueue_lookback(key)  # must not raise


# ---------------------------------------------------------------------------
# get_lookback_stats
# ---------------------------------------------------------------------------

def test_get_lookback_stats_returns_dict():
    import services.flow_store as fs
    stats = fs.get_lookback_stats()
    assert isinstance(stats, dict)
    assert "lookback_queued" in stats
    assert "lookback_queue_overflow" in stats


def test_get_lookback_stats_returns_copy():
    import services.flow_store as fs
    stats1 = fs.get_lookback_stats()
    stats2 = fs.get_lookback_stats()
    assert stats1 == stats2
    assert stats1 is not stats2


# ---------------------------------------------------------------------------
# Helper: reset _lookback_queue to the current event loop
# ---------------------------------------------------------------------------

def _reset_lookback_queue(fs, maxsize=5000):
    """
    Replace fs._lookback_queue with a fresh asyncio.Queue bound to the
    currently running event loop.  Required because the module-level queue
    is created at import time on whatever loop existed then.
    """
    fs._lookback_queue = asyncio.Queue(maxsize=maxsize)


# ---------------------------------------------------------------------------
# start_lookback_worker -- success path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_lookback_worker_processes_key_and_calls_update():
    import services.flow_store as fs
    from utils.contract_day_cache import ContractKey, LookbackResult

    _reset_lookback_queue(fs)

    key = ContractKey("NVDA", "CALL", 900.0, "2026-06-20")

    acc = MagicMock()
    acc._multi_day_min_days = 2
    acc._dte_tiers = [(30, {50_000: 50_000.0}), (60, {25_000: 25_000.0})]

    fake_result = LookbackResult(prior_days_active=3, prior_days_aggressive=1, fetched_at=1.0)

    await fs._lookback_queue.put(key)

    async def fake_get_lookback(k, min_prem):
        return fake_result

    with patch("services.flow_store._update_episode_multiday", new_callable=AsyncMock) as mock_update, \
         patch("utils.contract_day_cache.get_lookback", side_effect=fake_get_lookback):

        async def run_once():
            task = asyncio.create_task(fs.start_lookback_worker(acc))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run_once()

    # Implementation calls with keyword args — assert must match
    mock_update.assert_called_once_with(
        ticker="NVDA",
        contract_type="CALL",
        strike=900.0,
        expiry="2026-06-20",
        is_multi_day_repeat=True,
    )


@pytest.mark.asyncio
async def test_start_lookback_worker_is_repeat_false_when_below_min_days():
    import services.flow_store as fs
    from utils.contract_day_cache import ContractKey, LookbackResult

    _reset_lookback_queue(fs)

    key = ContractKey("AMD", "PUT", 100.0, "2026-05-17")

    acc = MagicMock()
    acc._multi_day_min_days = 2
    acc._dte_tiers = []

    fake_result = LookbackResult(prior_days_active=1, prior_days_aggressive=0, fetched_at=1.0)

    await fs._lookback_queue.put(key)

    async def fake_get_lookback(k, min_prem):
        return fake_result

    with patch("services.flow_store._update_episode_multiday", new_callable=AsyncMock) as mock_update, \
         patch("utils.contract_day_cache.get_lookback", side_effect=fake_get_lookback):

        async def run_once():
            task = asyncio.create_task(fs.start_lookback_worker(acc))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run_once()

    # Implementation calls with keyword args — assert must match
    mock_update.assert_called_once_with(
        ticker="AMD",
        contract_type="PUT",
        strike=100.0,
        expiry="2026-05-17",
        is_multi_day_repeat=False,
    )


@pytest.mark.asyncio
async def test_start_lookback_worker_handles_get_lookback_exception():
    """get_lookback raising must not kill the worker loop."""
    import services.flow_store as fs
    from utils.contract_day_cache import ContractKey

    _reset_lookback_queue(fs)

    key = ContractKey("META", "CALL", 550.0, "2026-06-20")

    acc = MagicMock()
    acc._multi_day_min_days = 2
    acc._dte_tiers = []

    await fs._lookback_queue.put(key)

    async def boom(k, min_prem):
        raise RuntimeError("DB exploded")

    with patch("services.flow_store._update_episode_multiday", new_callable=AsyncMock) as mock_update, \
         patch("utils.contract_day_cache.get_lookback", side_effect=boom):

        async def run_once():
            task = asyncio.create_task(fs.start_lookback_worker(acc))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run_once()  # must not raise

    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_start_lookback_worker_dte_tiers_absent_uses_default_floor():
    """When accumulator has no _dte_tiers, min_premium defaults to 10_000."""
    import services.flow_store as fs
    from utils.contract_day_cache import ContractKey, LookbackResult

    _reset_lookback_queue(fs)

    key = ContractKey("MSFT", "CALL", 420.0, "2026-07-18")

    acc = MagicMock(spec=["_multi_day_min_days"])  # no _dte_tiers
    acc._multi_day_min_days = 2

    captured = {}
    fake_result = LookbackResult(prior_days_active=0, prior_days_aggressive=0, fetched_at=1.0)

    async def fake_get_lookback(k, min_prem):
        captured["min_prem"] = min_prem
        return fake_result

    await fs._lookback_queue.put(key)

    with patch("services.flow_store._update_episode_multiday", new_callable=AsyncMock), \
         patch("utils.contract_day_cache.get_lookback", side_effect=fake_get_lookback):

        async def run_once():
            task = asyncio.create_task(fs.start_lookback_worker(acc))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run_once()

    assert captured.get("min_prem") == 10_000.0


# ---------------------------------------------------------------------------
# _update_episode_multiday -- all branches
# ---------------------------------------------------------------------------

def _make_http_mock(get_status=200, get_json=None, patch_status=204):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    get_resp = MagicMock()
    get_resp.status_code = get_status
    get_resp.text = ""
    get_resp.json = MagicMock(return_value=get_json if get_json is not None else [{"id": 42}])

    patch_resp = MagicMock()
    patch_resp.status_code = patch_status
    patch_resp.text = ""

    mock_client.get = AsyncMock(return_value=get_resp)
    mock_client.patch = AsyncMock(return_value=patch_resp)
    return mock_client


@pytest.mark.asyncio
async def test_update_episode_multiday_success_204():
    import services.flow_store as fs

    mock_client = _make_http_mock(get_status=200, get_json=[{"id": 7}], patch_status=204)

    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        await fs._update_episode_multiday("AAPL", "CALL", 150.0, "2026-06-20", True)

    mock_client.get.assert_called_once()
    mock_client.patch.assert_called_once()


@pytest.mark.asyncio
async def test_update_episode_multiday_success_200():
    import services.flow_store as fs

    mock_client = _make_http_mock(get_status=200, get_json=[{"id": 99}], patch_status=200)

    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        await fs._update_episode_multiday("SPY", "PUT", 440.0, "2026-05-17", False)

    mock_client.patch.assert_called_once()


@pytest.mark.asyncio
async def test_update_episode_multiday_get_non200_returns_early():
    import services.flow_store as fs

    mock_client = _make_http_mock(get_status=500)

    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        await fs._update_episode_multiday("TSLA", "CALL", 200.0, "2026-06-20", True)

    mock_client.patch.assert_not_called()


@pytest.mark.asyncio
async def test_update_episode_multiday_empty_rows_returns_early():
    import services.flow_store as fs

    mock_client = _make_http_mock(get_status=200, get_json=[])

    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        await fs._update_episode_multiday("NVDA", "CALL", 900.0, "2026-06-20", False)

    mock_client.patch.assert_not_called()


@pytest.mark.asyncio
async def test_update_episode_multiday_row_with_no_id_skips_patch():
    import services.flow_store as fs

    mock_client = _make_http_mock(get_status=200, get_json=[{}])

    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        await fs._update_episode_multiday("QQQ", "PUT", 350.0, "2026-05-17", True)

    mock_client.patch.assert_not_called()


@pytest.mark.asyncio
async def test_update_episode_multiday_patch_non200_logs_warning():
    import services.flow_store as fs

    mock_client = _make_http_mock(get_status=200, get_json=[{"id": 1}], patch_status=400)

    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        await fs._update_episode_multiday("AMD", "CALL", 100.0, "2026-07-18", True)

    mock_client.patch.assert_called_once()


@pytest.mark.asyncio
async def test_update_episode_multiday_exception_is_swallowed():
    import services.flow_store as fs

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=Exception("network down"))

    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        await fs._update_episode_multiday("META", "PUT", 550.0, "2026-06-20", False)


@pytest.mark.asyncio
async def test_update_episode_multiday_not_configured_returns_immediately():
    import services.flow_store as fs

    with patch("services.flow_store._SUPABASE_URL", None):
        with patch("httpx.AsyncClient") as mock_cls:
            await fs._update_episode_multiday("AAPL", "CALL", 150.0, "2026-06-20", True)
        mock_cls.assert_not_called()
