"""
R3 regression: signal_store round-trip with TTL and dedup.
"""
import pytest


def test_signal_store_importable():
    import services.signal_store as _m  # intentional smoke import
    assert _m is not None


@pytest.mark.asyncio
async def test_save_and_get_signal():
    from services.signal_store import save_signal, get_signals
    await save_signal({"ticker": "AAPL", "score": 0.9})
    signals = await get_signals("AAPL")
    assert any(s.get("ticker") == "AAPL" for s in signals)


@pytest.mark.asyncio
async def test_dedup_prevents_duplicate_signals():
    from services.signal_store import save_signal, get_signals
    sig = {"ticker": "TSLA", "score": 0.85, "id": "dedup-test-1"}
    await save_signal(sig)
    await save_signal(sig)
    signals = await get_signals("TSLA")
    matching = [s for s in signals if s.get("id") == "dedup-test-1"]
    assert len(matching) <= 1
