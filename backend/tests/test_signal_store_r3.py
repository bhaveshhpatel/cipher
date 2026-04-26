"""
R3 regression: signal_store round-trip with TTL and dedup.

Covers:
 - Module is importable
 - save_signal + get_signals round-trip for known ticker
 - Duplicate signal (same id) is not stored twice
 - get_signals for unknown ticker returns list (empty or otherwise, never raises)
 - save_signal with missing 'id' key does not crash
 - Returned signals are all dicts
"""
import pytest


def test_signal_store_importable():
    import services.signal_store as _m
    assert _m is not None


@pytest.mark.asyncio
async def test_save_and_get_signal():
    from services.signal_store import save_signal, get_signals
    await save_signal({"ticker": "AAPL", "score": 0.9, "id": "r3-aapl-1"})
    signals = await get_signals("AAPL")
    assert any(s.get("ticker") == "AAPL" for s in signals)


@pytest.mark.asyncio
async def test_dedup_prevents_duplicate_signals():
    from services.signal_store import save_signal, get_signals
    sig = {"ticker": "TSLA", "score": 0.85, "id": "dedup-test-r3-1"}
    await save_signal(sig)
    await save_signal(sig)
    signals = await get_signals("TSLA")
    matching = [s for s in signals if s.get("id") == "dedup-test-r3-1"]
    assert len(matching) <= 1


@pytest.mark.asyncio
async def test_get_signals_unknown_ticker_returns_list():
    from services.signal_store import get_signals
    result = await get_signals("TICKER_DOES_NOT_EXIST_XYZ")
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_save_signal_without_id_does_not_crash():
    from services.signal_store import save_signal
    try:
        await save_signal({"ticker": "SPY", "score": 0.5})
    except Exception as exc:
        pytest.fail(f"save_signal without id raised: {exc}")


@pytest.mark.asyncio
async def test_returned_signals_are_dicts():
    from services.signal_store import save_signal, get_signals
    await save_signal({"ticker": "MSFT", "score": 0.7, "id": "r3-msft-type"})
    signals = await get_signals("MSFT")
    for s in signals:
        assert isinstance(s, dict)
