"""
tests/test_symbols_loader.py

Full edge case + regression suite for services/symbols_loader.py
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
# _fetch_optionable_symbols
# ---------------------------------------------------------------------------
class TestFetchOptionableSymbols:
    @pytest.mark.asyncio
    async def test_returns_symbols_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "symbols": [{"rootSymbol": "AAPL"}, {"rootSymbol": "TSLA"}]
        }
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )
            result = await _fetch_optionable_symbols()
        assert "AAPL" in result
        assert "TSLA" in result

    @pytest.mark.asyncio
    async def test_returns_empty_on_401(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )
            result = await _fetch_optionable_symbols()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self):
        import httpx
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.ConnectError("refused")
            )
            result = await _fetch_optionable_symbols()
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_single_symbol_dict(self):
        """Tradier sometimes returns a dict instead of list when only 1 symbol."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"symbols": {"rootSymbol": "SPY"}}
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )
            result = await _fetch_optionable_symbols()
        assert "SPY" in result

    @pytest.mark.asyncio
    async def test_strips_and_uppercases_symbols(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "symbols": [{"rootSymbol": " aapl "}, {"rootSymbol": "nvda"}]
        }
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )
            result = await _fetch_optionable_symbols()
        assert "AAPL" in result
        assert "NVDA" in result


# ---------------------------------------------------------------------------
# _validate_symbols
# ---------------------------------------------------------------------------
class TestValidateSymbols:
    @pytest.mark.asyncio
    async def test_passes_symbols_with_expirations(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "expirations": {"date": ["2025-01-17", "2025-02-21"]}
        }
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )
            result = await _validate_symbols(["AAPL", "TSLA"])
        assert "AAPL" in result
        assert "TSLA" in result

    @pytest.mark.asyncio
    async def test_filters_symbols_without_expirations(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"expirations": {"date": []}}
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )
            result = await _validate_symbols(["FAKE"])
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_on_non_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )
            result = await _validate_symbols(["GONE"])
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_exception_per_symbol(self):
        """An exception for one symbol shouldn't kill the whole batch."""
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
# load_universe — integration-level edge cases
# ---------------------------------------------------------------------------
class TestLoadUniverse:
    @pytest.mark.asyncio
    async def test_returns_tradier_validated_on_success(self):
        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch:
            mock_settings.TRADIER_API_KEY = "test-key"
            mock_fetch.return_value = ["AAPL", "TSLA", "NVDA"]
            symbols, source = await load_universe()
        assert source == "tradier_validated"
        assert len(symbols) == 3

    @pytest.mark.asyncio
    async def test_falls_back_to_db_snapshot_on_tradier_error(self):
        db_snap = ["SPY", "QQQ", "AAPL"]
        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch:
            mock_settings.TRADIER_API_KEY = "test-key"
            mock_fetch.side_effect = Exception("Tradier down")
            symbols, source = await load_universe(db_snapshot=db_snap)
        assert source == "cache"
        assert symbols == db_snap

    @pytest.mark.asyncio
    async def test_falls_back_to_seed_when_no_db_and_tradier_error(self):
        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch:
            mock_settings.TRADIER_API_KEY = "test-key"
            mock_fetch.side_effect = Exception("Tradier down")
            symbols, source = await load_universe(db_snapshot=None)
        assert source == "seed_fallback"
        assert set(symbols) == set(SEED_SYMBOLS)

    @pytest.mark.asyncio
    async def test_no_api_key_returns_seed_immediately(self):
        with patch("services.symbols_loader.settings") as mock_settings:
            mock_settings.TRADIER_API_KEY = ""
            symbols, source = await load_universe(db_snapshot=None)
        assert source == "seed_fallback"
        assert symbols == list(SEED_SYMBOLS)

    @pytest.mark.asyncio
    async def test_no_api_key_with_db_snapshot_returns_cache(self):
        db_snap = ["SPY", "QQQ"]
        with patch("services.symbols_loader.settings") as mock_settings:
            mock_settings.TRADIER_API_KEY = ""
            symbols, source = await load_universe(db_snapshot=db_snap)
        assert source == "cache"
        assert symbols == db_snap

    @pytest.mark.asyncio
    async def test_returns_seed_when_tradier_returns_empty(self):
        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch:
            mock_settings.TRADIER_API_KEY = "test-key"
            mock_fetch.return_value = []
            symbols, source = await load_universe(db_snapshot=None)
        assert source == "seed_fallback"
