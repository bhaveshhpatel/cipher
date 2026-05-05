"""
Coverage tests for utils/contract_day_cache.py — _fetch_from_db HTTP paths:
  - 200 success with valid JSON → LookbackResult populated correctly
  - 200 success with null fields → coerced to 0
  - non-200 status → returns zero LookbackResult, cache still updated
  - exception in httpx call → returns zero LookbackResult
  - not configured (no URL/KEY) → returns zero without HTTP call
  - get_lookback cache hit (fresh) → _fetch_from_db NOT called again
  - get_lookback cache miss (stale) → _fetch_from_db called again
  - clear_cache() empties the cache dict
"""
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_resp(status: int, json_data=None, raise_exc=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    if raise_exc:
        resp.json = MagicMock(side_effect=raise_exc)
    else:
        resp.json = MagicMock(return_value=json_data or {})
    return resp


def _mock_client(resp=None, side_effect=None):
    mc = AsyncMock()
    mc.__aenter__ = AsyncMock(return_value=mc)
    mc.__aexit__ = AsyncMock(return_value=None)
    if side_effect:
        mc.post = AsyncMock(side_effect=side_effect)
    else:
        mc.post = AsyncMock(return_value=resp)
    return mc


# ---------------------------------------------------------------------------
# _fetch_from_db — 200 success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_from_db_200_populates_result():
    from utils.contract_day_cache import _fetch_from_db, ContractKey, clear_cache

    clear_cache()
    key = ContractKey("AAPL", "CALL", 150.0, "2026-06-20")
    resp = _make_resp(200, {"prior_days_active": 3, "prior_days_aggressive": 1})
    mc = _mock_client(resp=resp)

    with patch("utils.contract_day_cache._SUPABASE_URL", "https://x.supabase.co"), \
         patch("utils.contract_day_cache._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mc):
        result = await _fetch_from_db(key, 10_000.0)

    assert result.prior_days_active == 3
    assert result.prior_days_aggressive == 1
    assert result.fetched_at > 0


@pytest.mark.asyncio
async def test_fetch_from_db_200_null_fields_coerced_to_zero():
    from utils.contract_day_cache import _fetch_from_db, ContractKey, clear_cache

    clear_cache()
    key = ContractKey("SPY", "PUT", 440.0, "2026-05-17")
    resp = _make_resp(200, {"prior_days_active": None, "prior_days_aggressive": None})
    mc = _mock_client(resp=resp)

    with patch("utils.contract_day_cache._SUPABASE_URL", "https://x.supabase.co"), \
         patch("utils.contract_day_cache._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mc):
        result = await _fetch_from_db(key, 10_000.0)

    assert result.prior_days_active == 0
    assert result.prior_days_aggressive == 0


# ---------------------------------------------------------------------------
# _fetch_from_db — non-200
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_from_db_non200_returns_zero_result():
    from utils.contract_day_cache import _fetch_from_db, ContractKey, clear_cache

    clear_cache()
    key = ContractKey("TSLA", "CALL", 200.0, "2026-06-20")
    resp = _make_resp(500)
    mc = _mock_client(resp=resp)

    with patch("utils.contract_day_cache._SUPABASE_URL", "https://x.supabase.co"), \
         patch("utils.contract_day_cache._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mc):
        result = await _fetch_from_db(key, 10_000.0)

    assert result.prior_days_active == 0
    assert result.prior_days_aggressive == 0


@pytest.mark.asyncio
async def test_fetch_from_db_404_returns_zero_result():
    from utils.contract_day_cache import _fetch_from_db, ContractKey, clear_cache

    clear_cache()
    key = ContractKey("NVDA", "PUT", 900.0, "2026-07-18")
    resp = _make_resp(404)
    mc = _mock_client(resp=resp)

    with patch("utils.contract_day_cache._SUPABASE_URL", "https://x.supabase.co"), \
         patch("utils.contract_day_cache._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mc):
        result = await _fetch_from_db(key, 50_000.0)

    assert result.prior_days_active == 0


# ---------------------------------------------------------------------------
# _fetch_from_db — exception
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_from_db_exception_returns_zero_result():
    from utils.contract_day_cache import _fetch_from_db, ContractKey, clear_cache

    clear_cache()
    key = ContractKey("AMD", "CALL", 100.0, "2026-06-20")
    mc = _mock_client(side_effect=Exception("timeout"))

    with patch("utils.contract_day_cache._SUPABASE_URL", "https://x.supabase.co"), \
         patch("utils.contract_day_cache._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient", return_value=mc):
        result = await _fetch_from_db(key, 10_000.0)

    assert result.prior_days_active == 0
    assert result.fetched_at > 0  # still sets fetched_at so cache can store


# ---------------------------------------------------------------------------
# _fetch_from_db — not configured
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_from_db_not_configured_returns_zero_without_http():
    from utils.contract_day_cache import _fetch_from_db, ContractKey, clear_cache

    clear_cache()
    key = ContractKey("META", "CALL", 550.0, "2026-06-20")

    with patch("utils.contract_day_cache._SUPABASE_URL", None):
        with patch("httpx.AsyncClient") as mock_cls:
            result = await _fetch_from_db(key, 10_000.0)
        mock_cls.assert_not_called()

    assert result.prior_days_active == 0


# ---------------------------------------------------------------------------
# get_lookback — cache hit (fresh) path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_lookback_cache_hit_skips_fetch():
    from utils.contract_day_cache import get_lookback, ContractKey, LookbackResult, _cache, clear_cache

    clear_cache()
    key = ContractKey("MSFT", "CALL", 420.0, "2026-07-18")
    cached = LookbackResult(prior_days_active=2, prior_days_aggressive=1, fetched_at=time.monotonic())
    _cache[key] = cached  # seed a fresh entry

    with patch("utils.contract_day_cache._fetch_from_db", new_callable=AsyncMock) as mock_fetch:
        result = await get_lookback(key, 10_000.0)

    mock_fetch.assert_not_called()
    assert result.prior_days_active == 2


# ---------------------------------------------------------------------------
# get_lookback — cache miss → _fetch_from_db called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_lookback_cache_miss_calls_fetch():
    from utils.contract_day_cache import get_lookback, ContractKey, LookbackResult, clear_cache

    clear_cache()
    key = ContractKey("QQQ", "PUT", 350.0, "2026-05-17")
    fake = LookbackResult(prior_days_active=1, prior_days_aggressive=0, fetched_at=time.monotonic())

    with patch("utils.contract_day_cache._fetch_from_db", new_callable=AsyncMock, return_value=fake) as mock_fetch:
        result = await get_lookback(key, 25_000.0)

    mock_fetch.assert_called_once_with(key, 25_000.0)
    assert result.prior_days_active == 1


# ---------------------------------------------------------------------------
# get_lookback — stale cache → re-fetches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_lookback_stale_cache_refetches():
    from utils.contract_day_cache import (
        get_lookback, ContractKey, LookbackResult, _cache, clear_cache, _CACHE_TTL_S
    )

    clear_cache()
    key = ContractKey("IWM", "CALL", 200.0, "2026-06-20")
    # plant a stale entry (fetched TTL+10 seconds ago)
    stale = LookbackResult(
        prior_days_active=5,
        prior_days_aggressive=2,
        fetched_at=time.monotonic() - _CACHE_TTL_S - 10,
    )
    _cache[key] = stale

    fresh = LookbackResult(prior_days_active=0, prior_days_aggressive=0, fetched_at=time.monotonic())

    with patch("utils.contract_day_cache._fetch_from_db", new_callable=AsyncMock, return_value=fresh) as mock_fetch:
        result = await get_lookback(key, 10_000.0)

    mock_fetch.assert_called_once()
    assert result.prior_days_active == 0  # fresh result, not the stale 5


# ---------------------------------------------------------------------------
# clear_cache
# ---------------------------------------------------------------------------

def test_clear_cache_empties_dict():
    from utils.contract_day_cache import ContractKey, LookbackResult, _cache, clear_cache

    key = ContractKey("AAPL", "CALL", 150.0, "2026-06-20")
    _cache[key] = LookbackResult(prior_days_active=1, prior_days_aggressive=0, fetched_at=1.0)
    assert len(_cache) >= 1

    clear_cache()
    assert len(_cache) == 0
