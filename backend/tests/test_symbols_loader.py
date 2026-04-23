"""
tests/test_symbols_loader.py

Full edge case + regression suite for services/symbols_loader.py
Covers Steps 1–3: CBOE fetch, Tradier validation, batch quotes + stream_eligible.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.symbols_loader import (
    load_universe,
    _fetch_cboe_symbols,
    _validate_symbols,
    _fetch_batch_quotes,
    SymbolQuote,
    SEED_SYMBOLS,
)


# ---------------------------------------------------------------------------
# _fetch_cboe_symbols  (Step 1)
# ---------------------------------------------------------------------------

class TestFetchCboeSymbols:
    @pytest.mark.asyncio
    async def test_returns_symbols_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            'Company Name,OSI Symbol,Exchange,Tick\n'
            '"Apple Inc","AAPL","C2","NOR"\n'
            '"Tesla","TSLA","C2","NOR"\n'
        )
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _fetch_cboe_symbols()
        assert "AAPL" in result
        assert "TSLA" in result

    @pytest.mark.asyncio
    async def test_returns_empty_on_non_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = ""
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _fetch_cboe_symbols()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self):
        import httpx
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.ConnectError("refused")
            )
            result = await _fetch_cboe_symbols()
        assert result == []

    @pytest.mark.asyncio
    async def test_deduplicates_symbols(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            'Company,Symbol,Exchange,Tick\n'
            '"Apple","AAPL","C2","NOR"\n'
            '"Apple2","AAPL","C2","NOR"\n'
        )
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _fetch_cboe_symbols()
        assert result.count("AAPL") == 1


# ---------------------------------------------------------------------------
# _validate_symbols  (Step 2)
# ---------------------------------------------------------------------------

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
# _fetch_batch_quotes  (Step 3)
# ---------------------------------------------------------------------------

class TestFetchBatchQuotes:
    @pytest.mark.asyncio
    async def test_returns_symbol_quotes_list(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "quotes": {
                "quote": [
                    {"symbol": "AAPL", "last": 185.5,  "volume": 500_000},
                    {"symbol": "TSLA", "last": 210.0,  "volume": 1_200_000},
                ]
            }
        }
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _fetch_batch_quotes(["AAPL", "TSLA"])
        assert len(result) == 2
        aapl = next(q for q in result if q.symbol == "AAPL")
        assert aapl.last_price == 185.5
        assert aapl.volume == 500_000
        assert aapl.stream_eligible is True

    @pytest.mark.asyncio
    async def test_stream_eligible_false_below_min_price(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "quotes": {"quote": [{"symbol": "PENNY", "last": 0.50, "volume": 5_000_000}]}
        }
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client, \
             patch("services.symbols_loader.settings") as mock_settings:
            mock_settings.TRADIER_API_KEY   = "key"
            mock_settings.UNIVERSE_MIN_PRICE   = 1.0
            mock_settings.UNIVERSE_MIN_VOLUME  = 100_000
            mock_settings.UNIVERSE_QUOTES_BATCH_SIZE   = 200
            mock_settings.UNIVERSE_QUOTES_CONCURRENCY  = 28
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _fetch_batch_quotes(["PENNY"])
        assert result[0].stream_eligible is False

    @pytest.mark.asyncio
    async def test_stream_eligible_false_below_min_volume(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "quotes": {"quote": [{"symbol": "LOW", "last": 50.0, "volume": 500}]}
        }
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client, \
             patch("services.symbols_loader.settings") as mock_settings:
            mock_settings.TRADIER_API_KEY   = "key"
            mock_settings.UNIVERSE_MIN_PRICE   = 1.0
            mock_settings.UNIVERSE_MIN_VOLUME  = 100_000
            mock_settings.UNIVERSE_QUOTES_BATCH_SIZE   = 200
            mock_settings.UNIVERSE_QUOTES_CONCURRENCY  = 28
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _fetch_batch_quotes(["LOW"])
        assert result[0].stream_eligible is False

    @pytest.mark.asyncio
    async def test_single_symbol_dict_response_handled(self):
        """Tradier returns a dict (not list) when only 1 symbol is in the batch."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "quotes": {"quote": {"symbol": "SPY", "last": 520.0, "volume": 80_000_000}}
        }
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _fetch_batch_quotes(["SPY"])
        assert len(result) == 1
        assert result[0].symbol == "SPY"
        assert result[0].stream_eligible is True

    @pytest.mark.asyncio
    async def test_missing_symbol_in_response_gets_null_quote(self):
        """Symbol not returned by Tradier gets last_price=None, stream_eligible=False."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"quotes": {"quote": []}}
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _fetch_batch_quotes(["GHOST"])
        assert result[0].symbol == "GHOST"
        assert result[0].last_price is None
        assert result[0].stream_eligible is False

    @pytest.mark.asyncio
    async def test_network_error_returns_null_quotes(self):
        import httpx
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.ConnectError("refused")
            )
            result = await _fetch_batch_quotes(["AAPL", "TSLA"])
        assert all(q.stream_eligible is False for q in result)
        assert all(q.last_price is None for q in result)

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        result = await _fetch_batch_quotes([])
        assert result == []

    @pytest.mark.asyncio
    async def test_batches_large_symbol_list(self):
        """500 symbols with batch_size=200 should fire 3 batches."""
        symbols = [f"SYM{i}" for i in range(500)]
        call_count = 0

        async def _mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            params   = kwargs.get("params", {})
            syms     = params.get("symbols", "").split(",")
            mock_r   = MagicMock()
            mock_r.status_code = 200
            mock_r.json.return_value = {
                "quotes": {
                    "quote": [{"symbol": s, "last": 10.0, "volume": 200_000} for s in syms]
                }
            }
            return mock_r

        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client, \
             patch("services.symbols_loader.settings") as mock_settings:
            mock_settings.TRADIER_API_KEY              = "key"
            mock_settings.UNIVERSE_MIN_PRICE           = 1.0
            mock_settings.UNIVERSE_MIN_VOLUME          = 100_000
            mock_settings.UNIVERSE_QUOTES_BATCH_SIZE   = 200
            mock_settings.UNIVERSE_QUOTES_CONCURRENCY  = 28
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=_mock_get)
            result = await _fetch_batch_quotes(symbols)

        assert call_count == 3           # 500 / 200 = 3 batches
        assert len(result) == 500
        assert all(q.stream_eligible for q in result)


# ---------------------------------------------------------------------------
# load_universe — returns (symbols, source, stream_eligible_set)
# ---------------------------------------------------------------------------

class TestLoadUniverse:
    @pytest.mark.asyncio
    async def test_full_pipeline_success(self):
        """Full Step 1→2→3 pipeline: returns tradier_validated + eligible set."""
        mock_quotes = [
            SymbolQuote(symbol="AAPL", last_price=185.0, volume=500_000, stream_eligible=True),
            SymbolQuote(symbol="TSLA", last_price=210.0, volume=1_200_000, stream_eligible=True),
            SymbolQuote(symbol="NVDA", last_price=0.50,  volume=50,        stream_eligible=False),
        ]
        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch, \
             patch("services.symbols_loader._fetch_batch_quotes", new_callable=AsyncMock) as mock_quotes_fn, \
             patch("services.symbols_loader.upsert_symbol_quotes", new_callable=AsyncMock), \
             patch("services.symbols_loader.settings") as mock_settings:
            mock_settings.TRADIER_API_KEY  = "test-key"
            mock_settings.priority_symbols = []
            mock_fetch.return_value        = ["AAPL", "TSLA", "NVDA"]
            mock_quotes_fn.return_value    = mock_quotes
            symbols, source, eligible_set  = await load_universe()

        assert source == "tradier_validated"
        assert set(symbols) == {"AAPL", "TSLA", "NVDA"}
        assert "AAPL" in eligible_set
        assert "TSLA" in eligible_set
        assert "NVDA" not in eligible_set

    @pytest.mark.asyncio
    async def test_priority_symbols_always_eligible(self):
        """Priority symbols forced into eligible_set even if below price/volume thresholds."""
        mock_quotes = [
            SymbolQuote(symbol="SPY", last_price=0.01, volume=1, stream_eligible=False),
        ]
        with patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch, \
             patch("services.symbols_loader._fetch_batch_quotes", new_callable=AsyncMock) as mock_quotes_fn, \
             patch("services.symbols_loader.upsert_symbol_quotes", new_callable=AsyncMock), \
             patch("services.symbols_loader.settings") as mock_settings:
            mock_settings.TRADIER_API_KEY  = "test-key"
            mock_settings.priority_symbols = ["SPY"]
            mock_fetch.return_value        = ["SPY"]
            mock_quotes_fn.return_value    = mock_quotes
            symbols, source, eligible_set  = await load_universe()

        assert "SPY" in eligible_set

    @pytest.mark.asyncio
    async def test_falls_back_to_db_snapshot_on_error(self):
        db_snap = ["SPY", "QQQ", "AAPL"]
        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch:
            mock_settings.TRADIER_API_KEY = "test-key"
            mock_fetch.side_effect        = Exception("Tradier down")
            symbols, source, eligible_set = await load_universe(db_snapshot=db_snap)

        assert source == "cache"
        assert symbols == db_snap
        assert eligible_set is None

    @pytest.mark.asyncio
    async def test_falls_back_to_seed_when_no_db(self):
        with patch("services.symbols_loader.settings") as mock_settings, \
             patch("services.symbols_loader._fetch_and_validate", new_callable=AsyncMock) as mock_fetch:
            mock_settings.TRADIER_API_KEY = "test-key"
            mock_fetch.side_effect        = Exception("Tradier down")
            symbols, source, eligible_set = await load_universe(db_snapshot=None)

        assert source == "seed_fallback"
        assert set(symbols) == set(SEED_SYMBOLS)
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
    async def test_no_api_key_no_db_returns_seed(self):
        with patch("services.symbols_loader.settings") as mock_settings:
            mock_settings.TRADIER_API_KEY = ""
            symbols, source, eligible_set = await load_universe(db_snapshot=None)

        assert source == "seed_fallback"
        assert eligible_set is None

    @pytest.mark.asyncio
    async def test_average_volume_zero_falls_back_to_today_volume(self):
        """HOOD-case: average_volume=0 but today volume=24M → stream_eligible=True."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "quotes": {
                "quote": [{
                    "symbol": "HOOD",
                    "last": 83.4,
                    "volume": 24_549_554,
                    "average_volume": 0,
                }]
            }
        }
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _fetch_batch_quotes(["HOOD"])
        assert result[0].stream_eligible is True
        assert result[0].volume == 24_549_554

    @pytest.mark.asyncio
    async def test_effective_volume_uses_max_of_avg_and_today(self):
        """Takes the higher of average_volume vs today volume for eligibility."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "quotes": {
                "quote": [
                    # avg_vol > today_vol → use avg_vol
                    {"symbol": "RIVN", "last": 16.99, "volume": 5_000_000,  "average_volume": 28_622_140},
                    # today_vol > avg_vol (avg=0) → use today_vol
                    {"symbol": "HOOD", "last": 83.40, "volume": 24_549_554, "average_volume": 0},
                    # both below threshold → not eligible
                    {"symbol": "ILLQ", "last": 5.00,  "volume": 100,        "average_volume": 50},
                ]
            }
        }
        with patch("services.symbols_loader.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            result = await _fetch_batch_quotes(["RIVN", "HOOD", "ILLQ"])

        rivn = next(q for q in result if q.symbol == "RIVN")
        hood = next(q for q in result if q.symbol == "HOOD")
        illq = next(q for q in result if q.symbol == "ILLQ")

        assert rivn.stream_eligible is True
        assert hood.stream_eligible is True
        assert illq.stream_eligible is False
