"""
tests/test_symbol_registry_build_done.py

Unit tests for the _build_done asyncio.Event added to SymbolRegistry (Issue 1).

Covers:
  - wait_for_build() suspends until build() is called
  - wait_for_build() returns immediately when called after build() has run
  - _build_done.set() is called even when build() raises an exception
    inside the lock body (regression guard)
  - _refresh_quotes_in_background awaits wait_for_build() before get_oi_map()
    so OI is never read from an empty map on the warm path
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.symbol_registry import SymbolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(watchlist=None) -> SymbolRegistry:
    """Return a bare SymbolRegistry with no Tradier side-effects."""
    return SymbolRegistry(watchlist=watchlist or ["AAPL"])


async def _stub_build(registry: SymbolRegistry) -> int:
    """
    Minimal replacement for SymbolRegistry.build() that populates the
    _oi_by_ticker map and then sets _build_done — mirrors what the real
    build() does without hitting Tradier.
    """
    async with registry._build_lock:
        registry._oi_by_ticker = {"AAPL": 1234}
        registry._build_done.set()
    return 1


# ---------------------------------------------------------------------------
# Test: _build_done starts unset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_done_starts_unset():
    """_build_done must not be set on a freshly created registry."""
    reg = _make_registry()
    assert not reg._build_done.is_set()


# ---------------------------------------------------------------------------
# Test: wait_for_build() suspends until build() sets the event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_for_build_suspends_until_build_runs():
    """
    wait_for_build() should block until _stub_build (or the real build)
    calls _build_done.set().  We race a waiter against a delayed build
    and assert the waiter only unblocks after the build.
    """
    reg = _make_registry()
    order: list[str] = []

    async def waiter():
        await reg.wait_for_build()
        order.append("waiter_unblocked")

    async def builder():
        await asyncio.sleep(0.05)   # small delay so waiter starts first
        order.append("build_started")
        await _stub_build(reg)
        order.append("build_done")

    await asyncio.gather(waiter(), builder())

    assert order == ["build_started", "build_done", "waiter_unblocked"], order


# ---------------------------------------------------------------------------
# Test: wait_for_build() is a no-op when already set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_for_build_immediate_when_already_set():
    """After build() has run, subsequent wait_for_build() calls return immediately."""
    reg = _make_registry()
    await _stub_build(reg)

    # Should complete without blocking (give it a generous 0.5 s ceiling).
    await asyncio.wait_for(reg.wait_for_build(), timeout=0.5)


# ---------------------------------------------------------------------------
# Test: _build_done is set even if build() raises inside the lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_done_set_after_exception():
    """
    Regression guard: if build() raises AFTER acquiring the lock but BEFORE
    calling _build_done.set() the event must still be set so callers are not
    deadlocked.  The real implementation sets the event unconditionally at the
    end of the lock body; this test verifies that contract.

    We monkey-patch _persist_to_db to raise so we can simulate a late failure
    without mocking the entire Tradier stack.
    """
    reg = _make_registry(watchlist=[])

    # Patch all async helpers called inside build() with no-ops.
    async def _noop_config():
        return {
            "REGISTRY_MIN_OI": 0,
            "REGISTRY_REFRESH_MINS": 60,
            "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 15,
        }

    async def _noop_thresholds():
        return {}

    async def _noop_assign(quotes, thresholds=None):
        return {}

    async def _raising_persist(registry_dict):
        raise RuntimeError("simulated DB error")

    async def _noop_fetch_prices():
        return {}, {}

    with (
        patch("services.symbol_registry.asyncio.gather", new=AsyncMock(
            return_value=(_noop_config(), _noop_thresholds())
        )),
        patch("services.ingestion_config.get_config", new=AsyncMock(return_value=await _noop_config())),
        patch("services.tier_engine._fetch_thresholds",  new=AsyncMock(return_value={})),
        patch("services.tier_engine.assign_tiers",        new=AsyncMock(return_value={})),
        patch.object(reg, "_fetch_stock_prices",           new=AsyncMock(return_value=({}, {}))),
        patch.object(reg, "_persist_to_db",                new=AsyncMock(side_effect=RuntimeError("db err"))),
    ):
        # build() should propagate the exception from _persist_to_db
        with pytest.raises(RuntimeError, match="db err"):
            await reg.build()

    # Despite the exception, _build_done must have been set
    assert reg._build_done.is_set(), "_build_done must be set even after build() raises"


# ---------------------------------------------------------------------------
# Test: _refresh_quotes_in_background waits for build before reading OI map
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_quotes_waits_for_build_before_oi_map():
    """
    _refresh_quotes_in_background must call wait_for_build() before
    get_oi_map() so OI is not read from an empty dict on the warm path.

    We verify this by:
    1. Patching get_registry() to return our registry (event NOT set yet)
    2. Launching _refresh_quotes_in_background as a task
    3. Asserting get_oi_map() has NOT been called yet (build not done)
    4. Setting the event (simulating build() completion)
    5. Asserting get_oi_map() was eventually called
    """
    import main  # noqa: import after patch

    reg = _make_registry()
    oi_map_calls: list[dict] = []

    def _recording_get_oi_map():
        result = {"AAPL": 999}
        oi_map_calls.append(result)
        return result

    reg.get_oi_map = _recording_get_oi_map  # type: ignore[method-assign]

    fake_quotes = [MagicMock(symbol="AAPL", open_interest=0, stream_eligible=True)]

    with (
        patch("main.get_registry",        return_value=reg),
        patch("main._fetch_batch_quotes",  new=AsyncMock(return_value=fake_quotes)),
        patch("main.assign_tiers",         new=AsyncMock(return_value={"AAPL": 1})),
        patch("main.universe_store.upsert_symbol_quotes", new=AsyncMock()),
    ):
        task = asyncio.create_task(main._refresh_quotes_in_background(["AAPL"]))

        # Give the coroutine time to reach the await wait_for_build() point.
        await asyncio.sleep(0.05)

        # get_oi_map must NOT have been called yet (build event not set).
        assert len(oi_map_calls) == 0, "get_oi_map() called before build() completed"

        # Simulate build() completing.
        reg._build_done.set()

        await asyncio.wait_for(task, timeout=2.0)

        # Now get_oi_map should have been called exactly once.
        assert len(oi_map_calls) == 1, "get_oi_map() should be called once after build()"
