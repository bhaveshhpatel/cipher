"""
tests/test_symbols_loader.py

Full edge case + regression suite for services/symbols_loader.py
Updated for 3-tuple return: (symbols, source, stream_eligible_set)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.symbols_loader import (
    load_universe,
    _fetch_optionable_symbols,
    _validate_symbols,
    SEED_SYMBOLS,
)


# ---------------------------------------------------------------------------
# _fetch_optionable_symbols  (alias for _fetch_cboe_symbols used in tests)
# ---------------------------------------------------------------------------
async def _fetch_optionable_symbols():
    """Compatibility shim — tests call this; real code uses _fetch_cboe_symbols."""
    from services.symbols_loader import _fetch_cboe_symbols
    return await _fetch_cboe_symbols()


class TestFetchOptionableSymbols:
    @pytest.mark.asyncio
    async def test_returns_symbols_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Simulate CBOE CSV format: "Company","OSI Symbol","Exchange","Tick"
        mock_resp.text = 'Company Name,OSI Symbol,Exchange,Tick\n"Apple Inc","AAPL","C2","NOR"\n"Tesla","TSLA","C2","NOR"\n'
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            from services.symbols_loader import _fetch_cboe_symbols
            result = await _fetch_cboe_symbols()
        assert "AAPL" in result
        assert "TSLA" in result

    @pytest.mark.asyncio
    async def test_returns_empty_on_401(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = ""
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            from services.symbols_loader import _fetch_cboe_symbols
            result = await _fetch_cboe_symbols()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self):
        import httpx
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.ConnectError("refused")
            )
            from services.symbols_loader import _fetch_cboe_symbols
            result = await _fetch_cboe_symbols()
        assert result == []


class TestValidateSymbols:
    @pytest.mark.asyncio
    async def test_passes_symbols_with_expirations(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "{}"
        mock_resp.json.return_value = {
            "expirations": {"date": ["2025-01-17", "2025-02-21"]}
        }
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _validate_symbols(["AAPL", "TSLA"])
        assert "AAPL" in result
        assert "TSLA" in result

    @pytest.mark.asyncio
    async def test_filters_symbols_without_expirations(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "{}"
        mock_resp.json.return_value = {"expirations": {"date": []}}
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _validate_symbols(["FAKE"])
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_on_non_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _validate_symbols(["GONE"])
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_exception_per_symbol(self):
        import httpx
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.TimeoutException("timeout")
            )
            result = await _validate_symbols(["AAPL", "TSLA", "SPY"])
        assert result == []

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        result = await _validate_symbols([])
        assert result == []


# ---------------------------------------------------------------------------
# load_universe — now returns (symbols, source, stream_eligible_set)
# ---------------------------------------------------------------------------
class TestLoadUniverse:
    @pytest.mark.asyncio
    async def test_returns_tradier_validated_on_success(self):
        mock_screen = MagicMock()
        mock_screen.eligible = ["AAPL", "TSLA", "NVDA"]

        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch, \
             patch("services.symbols_loader.screen_universe", new_callable=AsyncMock) as mock_screen_fn:
            mock_settings.TRADIER_API_KEY = "test-key"
            mock_fetch.return_value = ["AAPL", "TSLA", "NVDA"]
            mock_screen_fn.return_value = mock_screen
            symbols, source, eligible_set = await load_universe()

        assert source == "tradier_validated"
        assert len(symbols) == 3
        assert isinstance(eligible_set, set)
        assert "AAPL" in eligible_set

    @pytest.mark.asyncio
    async def test_falls_back_to_db_snapshot_on_tradier_error(self):
        db_snap = ["SPY", "QQQ", "AAPL"]
        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch:
            mock_settings.TRADIER_API_KEY = "test-key"
            mock_fetch.side_effect = Exception("Tradier down")
            symbols, source, eligible_set = await load_universe(db_snapshot=db_snap)

        assert source == "cache"
        assert symbols == db_snap
        assert eligible_set is None  # screener not run on cache

    @pytest.mark.asyncio
    async def test_falls_back_to_seed_when_no_db_and_tradier_error(self):
        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch:
            mock_settings.TRADIER_API_KEY = "test-key"
            mock_fetch.side_effect = Exception("Tradier down")
            symbols, source, eligible_set = await load_universe(db_snapshot=None)

        assert source == "seed_fallback"
        assert set(symbols) == set(SEED_SYMBOLS)
        assert eligible_set is None

    @pytest.mark.asyncio
    async def test_no_api_key_returns_seed_immediately(self):
        with patch("services.symbols_loader.settings") as mock_settings:
            mock_settings.TRADIER_API_KEY = ""
            symbols, source, eligible_set = await load_universe(db_snapshot=None)

        assert source == "seed_fallback"
        assert symbols == list(SEED_SYMBOLS)
        assert eligible_set is None

    @pytest.mark.asyncio
    async def test_no_api_key_with_db_snapshot_returns_cache(self):
        db_snap = ["SPY", "QQQ"]
        with patch("services.symbols_loader.settings") as mock_settings:
            mock_settings.TRADIER_API_KEY = ""
            symbols, source, eligible_set = await load_universe(db_snapshot=db_snap)

        assert source == "cache"
        assert symbols == db_snap
        assert eligible_set is None

    @pytest.mark.asyncio
    async def test_returns_seed_when_tradier_returns_empty(self):
        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch:
            mock_settings.TRADIER_API_KEY = "test-key"
            mock_fetch.return_value = []
            symbols, source, eligible_set = await load_universe(db_snapshot=None)

        assert source == "seed_fallback"
        assert eligible_set is None
