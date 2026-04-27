"""
test_warm_path_single_tradier_call.py

Regression: on the warm (DB-hit) startup path, _fetch_stock_prices() inside
SymbolRegistry.build() must NEVER be called.  Instead _refresh_quotes_in_background
fetches quotes once and passes them into build(pre_fetched_quotes=...) so the
total number of Tradier /v1/markets/quotes round-trips is exactly 1.

Bug that was fixed
------------------
Previously lifespan() called:
    build_task = asyncio.create_task(registry.build())   # calls _fetch_stock_prices (1st)
    bg_quote_refresh_task = asyncio.create_task(_refresh_quotes_in_background(...))  # 2nd

Now on the warm path lifespan() does NOT call registry.build() at all;
_refresh_quotes_in_background does the single fetch + passes pre_fetched_quotes.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_quote(symbol: str, price: float = 100.0):
    q = MagicMock()
    q.symbol = symbol
    q.last_price = price
    q.volume = 1_000_000
    q.average_volume = 900_000
    q.open_interest = 0
    return q


@pytest.mark.asyncio
async def test_refresh_quotes_in_background_calls_build_with_pre_fetched():
    """
    _refresh_quotes_in_background must pass quotes into build(pre_fetched_quotes=...)
    so _fetch_stock_prices is never called.
    """
    from main import _refresh_quotes_in_background, _quote_refresh_lock

    quotes = [_make_quote("AAPL"), _make_quote("TSLA")]

    mock_registry = MagicMock()
    mock_registry.build = AsyncMock(return_value=42)
    mock_registry.get_oi_map = MagicMock(return_value={"AAPL": 500, "TSLA": 200})
    mock_registry.set_tier_map = MagicMock()

    build_calls = []

    async def capture_build(**kwargs):
        build_calls.append(kwargs)
        return 42

    mock_registry.build = AsyncMock(side_effect=capture_build)

    with (
        patch("main._fetch_batch_quotes", AsyncMock(return_value=quotes)),
        patch("main.get_registry", return_value=mock_registry),
        patch("main.assign_tiers", AsyncMock(return_value={"AAPL": 1, "TSLA": 2})),
        patch("main.universe_store.upsert_symbol_quotes", AsyncMock()),
    ):
        await _refresh_quotes_in_background(["AAPL", "TSLA"])

    assert len(build_calls) == 1, "build() should be called exactly once"
    assert "pre_fetched_quotes" in build_calls[0], (
        "build() must receive pre_fetched_quotes — _fetch_stock_prices must not be called"
    )
    pf = build_calls[0]["pre_fetched_quotes"]
    assert "AAPL" in pf and "TSLA" in pf, "pre_fetched_quotes must contain fetched symbols"


@pytest.mark.asyncio
async def test_refresh_quotes_in_background_skips_when_lock_held():
    """If _quote_refresh_lock is already acquired, the refresh is skipped (no duplicate calls)."""
    from main import _refresh_quotes_in_background, _quote_refresh_lock

    fetch_called = []

    async def fake_fetch(symbols):
        fetch_called.append(symbols)
        return []

    with patch("main._fetch_batch_quotes", AsyncMock(side_effect=fake_fetch)):
        async with _quote_refresh_lock:
            await _refresh_quotes_in_background(["AAPL"])

    assert len(fetch_called) == 0, "fetch should be skipped when lock is held"


@pytest.mark.asyncio
async def test_refresh_quotes_in_background_no_registry_still_tiers_and_upserts():
    """
    If registry is None (e.g. not yet initialised), refresh still completes
    tier assignment and upsert — it must not crash.
    """
    from main import _refresh_quotes_in_background

    quotes = [_make_quote("NVDA")]
    upsert_calls = []

    async def fake_upsert(q, tm):
        upsert_calls.append((q, tm))

    with (
        patch("main._fetch_batch_quotes", AsyncMock(return_value=quotes)),
        patch("main.get_registry", return_value=None),
        patch("main.assign_tiers", AsyncMock(return_value={"NVDA": 3})),
        patch("main.universe_store.upsert_symbol_quotes", AsyncMock(side_effect=fake_upsert)),
    ):
        await _refresh_quotes_in_background(["NVDA"])

    assert len(upsert_calls) == 1


@pytest.mark.asyncio
async def test_refresh_quotes_in_background_empty_quotes_is_noop():
    """If _fetch_batch_quotes returns empty list, nothing else should be called."""
    from main import _refresh_quotes_in_background

    build_called = []
    upsert_called = []

    mock_registry = MagicMock()
    mock_registry.build = AsyncMock(side_effect=lambda **kw: build_called.append(kw))

    with (
        patch("main._fetch_batch_quotes", AsyncMock(return_value=[])),
        patch("main.get_registry", return_value=mock_registry),
        patch("main.universe_store.upsert_symbol_quotes", AsyncMock(side_effect=lambda q, tm: upsert_called.append(1))),
    ):
        await _refresh_quotes_in_background(["AAPL"])

    assert len(build_called) == 0
    assert len(upsert_called) == 0
