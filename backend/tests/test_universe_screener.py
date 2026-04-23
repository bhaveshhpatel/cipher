"""
tests/test_universe_screener.py

Comprehensive tests for services/universe_screener.py

Covers:
  SC-1  empty input returns empty ScreenResult
  SC-2  priority symbols always in eligible list — no API call
  SC-3  non-priority symbol with OI > 0 is eligible
  SC-4  non-priority symbol with OI = 0 falls back to default (True)
  SC-5  non-priority symbol with OI = 0 excluded when default=False
  SC-6  network error on symbol falls back to default (True)
  SC-7  network error on symbol excluded when default=False
  SC-8  no API key — all candidates get default=True treatment
  SC-9  no API key — all candidates get default=False treatment
  SC-10 ScreenResult.summary() has all expected keys
  SC-11 ScreenResult.total == eligible + ineligible
  SC-12 get_stream_eligible() returns only eligible list
  SC-13 batch delay is awaited between batches
  SC-14 priority symbols are NOT double-counted when also in candidates
  SC-15 _nearest_expiry_param returns a valid YYYY-MM-DD string (Friday)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date

from services.universe_screener import (
    screen_universe,
    get_stream_eligible,
    ScreenResult,
    _nearest_expiry_param,
    _is_stream_eligible,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_chain_resp(open_interest: int, status: int = 200):
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = {
        "options": {
            "option": [
                {"open_interest": open_interest, "symbol": "AAPL260117C00150000"}
            ]
        }
    }
    return mock


def _mock_settings(priority="SPY,QQQ", api_key="test-key", default=True, delay_ms=0):
    m = MagicMock()
    m.priority_symbols = [s.strip() for s in priority.split(",") if s.strip()]
    m.TRADIER_API_KEY = api_key
    m.TRADIER_BASE_URL = "https://api.tradier.com"
    m.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = default
    m.UNIVERSE_BATCH_DELAY_MS = delay_ms
    return m


# ---------------------------------------------------------------------------
# SC-1: empty input
# ---------------------------------------------------------------------------
class TestEmptyInput:
    @pytest.mark.asyncio
    async def test_empty_symbols_returns_empty_result(self):
        result = await screen_universe([])
        assert result.eligible == []
        assert result.ineligible == []
        assert result.screened == 0


# ---------------------------------------------------------------------------
# SC-2: priority symbols always eligible
# ---------------------------------------------------------------------------
class TestPrioritySymbols:
    @pytest.mark.asyncio
    async def test_priority_symbols_always_eligible(self):
        mock_s = _mock_settings(priority="SPY,QQQ,AAPL")
        with patch("services.universe_screener.settings", mock_s):
            result = await screen_universe(["SPY", "QQQ", "AAPL"])
        assert set(result.eligible) == {"SPY", "QQQ", "AAPL"}
        assert result.ineligible == []
        assert result.screened == 0  # no API calls needed

    @pytest.mark.asyncio
    async def test_priority_symbols_not_screened_via_api(self):
        """Priority symbols should not trigger any HTTP calls."""
        mock_s = _mock_settings(priority="SPY,QQQ")
        with patch("services.universe_screener.settings", mock_s), \
             patch("services.universe_screener.httpx.AsyncClient") as mock_client:
            await screen_universe(["SPY", "QQQ"])
        mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# SC-3: non-priority with OI > 0 is eligible
# ---------------------------------------------------------------------------
class TestScreeningEligible:
    @pytest.mark.asyncio
    async def test_symbol_with_oi_is_eligible(self):
        mock_s = _mock_settings(priority="SPY")
        resp   = _mock_chain_resp(open_interest=500)
        with patch("services.universe_screener.settings", mock_s), \
             patch("services.universe_screener.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
            result = await screen_universe(["SPY", "TSLA"])
        assert "SPY"  in result.eligible
        assert "TSLA" in result.eligible


# ---------------------------------------------------------------------------
# SC-4 & SC-5: zero OI — default flag controls outcome
# ---------------------------------------------------------------------------
class TestZeroOI:
    @pytest.mark.asyncio
    async def test_zero_oi_defaults_to_eligible_when_default_true(self):
        mock_s = _mock_settings(priority="", default=True)
        resp   = _mock_chain_resp(open_interest=0)
        with patch("services.universe_screener.settings", mock_s), \
             patch("services.universe_screener.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
            result = await screen_universe(["TSLA"])
        assert "TSLA" in result.eligible

    @pytest.mark.asyncio
    async def test_zero_oi_excluded_when_default_false(self):
        mock_s = _mock_settings(priority="", default=False)
        resp   = _mock_chain_resp(open_interest=0)
        with patch("services.universe_screener.settings", mock_s), \
             patch("services.universe_screener.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
            result = await screen_universe(["TSLA"])
        assert "TSLA" not in result.eligible
        assert "TSLA" in result.ineligible


# ---------------------------------------------------------------------------
# SC-6 & SC-7: network error — default flag controls outcome
# ---------------------------------------------------------------------------
class TestNetworkError:
    @pytest.mark.asyncio
    async def test_network_error_defaults_to_eligible_when_default_true(self):
        import httpx
        mock_s = _mock_settings(priority="", default=True)
        with patch("services.universe_screener.settings", mock_s), \
             patch("services.universe_screener.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.ConnectError("refused")
            )
            result = await screen_universe(["NVDA"])
        assert "NVDA" in result.eligible

    @pytest.mark.asyncio
    async def test_network_error_excluded_when_default_false(self):
        import httpx
        mock_s = _mock_settings(priority="", default=False)
        with patch("services.universe_screener.settings", mock_s), \
             patch("services.universe_screener.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.ConnectError("refused")
            )
            result = await screen_universe(["NVDA"])
        assert "NVDA" not in result.eligible
        assert "NVDA" in result.ineligible


# ---------------------------------------------------------------------------
# SC-8 & SC-9: no API key
# ---------------------------------------------------------------------------
class TestNoApiKey:
    @pytest.mark.asyncio
    async def test_no_api_key_all_eligible_when_default_true(self):
        mock_s = _mock_settings(priority="", api_key="", default=True)
        with patch("services.universe_screener.settings", mock_s):
            result = await screen_universe(["AAPL", "TSLA", "NVDA"])
        assert set(result.eligible) == {"AAPL", "TSLA", "NVDA"}
        assert result.ineligible == []

    @pytest.mark.asyncio
    async def test_no_api_key_all_ineligible_when_default_false(self):
        mock_s = _mock_settings(priority="", api_key="", default=False)
        with patch("services.universe_screener.settings", mock_s):
            result = await screen_universe(["AAPL", "TSLA"])
        assert result.eligible == []
        assert set(result.ineligible) == {"AAPL", "TSLA"}


# ---------------------------------------------------------------------------
# SC-10 & SC-11: ScreenResult helpers
# ---------------------------------------------------------------------------
class TestScreenResult:
    def test_summary_has_all_keys(self):
        r = ScreenResult(eligible=["A", "B"], ineligible=["C"], priority=["A"], screened=2, duration_s=1.5)
        s = r.summary()
        for key in ("eligible", "ineligible", "priority", "screened", "duration_s", "source"):
            assert key in s, f"Missing key: {key}"

    def test_total_equals_eligible_plus_ineligible(self):
        r = ScreenResult(eligible=["A", "B", "C"], ineligible=["D"])
        assert r.total == 4


# ---------------------------------------------------------------------------
# SC-12: get_stream_eligible convenience wrapper
# ---------------------------------------------------------------------------
class TestGetStreamEligible:
    @pytest.mark.asyncio
    async def test_returns_only_eligible_list(self):
        mock_s = _mock_settings(priority="SPY,QQQ")
        with patch("services.universe_screener.settings", mock_s):
            eligible = await get_stream_eligible(["SPY", "QQQ"])
        assert set(eligible) == {"SPY", "QQQ"}


# ---------------------------------------------------------------------------
# SC-13: batch delay is awaited
# ---------------------------------------------------------------------------
class TestBatchDelay:
    @pytest.mark.asyncio
    async def test_batch_delay_is_awaited_between_batches(self):
        """With delay_ms > 0 and >CONCURRENCY symbols, asyncio.sleep must be called."""
        mock_s = _mock_settings(priority="", delay_ms=50)
        resp   = _mock_chain_resp(open_interest=100)

        sleep_calls = []
        original_sleep = __import__("asyncio").sleep

        async def mock_sleep(delay):
            sleep_calls.append(delay)

        symbols = [f"SYM{i}" for i in range(25)]  # > batch size of 20

        with patch("services.universe_screener.settings", mock_s), \
             patch("services.universe_screener.httpx.AsyncClient") as mock_client, \
             patch("services.universe_screener.asyncio.sleep", side_effect=mock_sleep):
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
            await screen_universe(symbols)

        assert len(sleep_calls) >= 1
        assert sleep_calls[0] == pytest.approx(0.05, abs=1e-4)


# ---------------------------------------------------------------------------
# SC-14: priority symbols not double-counted
# ---------------------------------------------------------------------------
class TestNoDuplicates:
    @pytest.mark.asyncio
    async def test_priority_symbols_not_in_candidates(self):
        mock_s = _mock_settings(priority="SPY,QQQ")
        resp   = _mock_chain_resp(open_interest=200)
        with patch("services.universe_screener.settings", mock_s), \
             patch("services.universe_screener.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
            result = await screen_universe(["SPY", "QQQ", "AAPL"])
        # SPY and QQQ are priority — AAPL is screened
        # Eligible should have all 3 but no duplicates
        assert len(result.eligible) == len(set(result.eligible))
        assert "SPY"  in result.eligible
        assert "QQQ"  in result.eligible
        assert "AAPL" in result.eligible


# ---------------------------------------------------------------------------
# SC-15: _nearest_expiry_param
# ---------------------------------------------------------------------------
class TestNearestExpiry:
    def test_returns_valid_date_string(self):
        result = _nearest_expiry_param()
        # Should be YYYY-MM-DD
        parsed = date.fromisoformat(result)
        assert parsed >= date.today()

    def test_returns_a_friday(self):
        result  = _nearest_expiry_param()
        parsed  = date.fromisoformat(result)
        assert parsed.weekday() == 4, f"Expected Friday (4), got weekday {parsed.weekday()}"
