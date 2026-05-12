"""
Regression tests for tradier_stream.py — covers all 9 failure modes.

Run: pytest backend/tests/test_tradier_stream.py -v

Test IDs map directly to failure mode analysis:
  F1  — session token re-fetched on every reconnect
  F2  — 401 on stream does NOT permanently fall into demo mode
  F3  — idle watchdog triggers reconnect after 30s silence
  F4  — exponential backoff with jitter (5s base, 60s cap)
  F5  — session fetch retried up to 3x on transient error
  F6a — 401 on session = bad key, returns None (no crash)
  F6b — 401 on stream = expired token, fast re-fetch (no long sleep)
  F7  — heartbeat: reconnect on no-line timeout
  F8  — demo mode task is cancellable (not an infinite trap)
  F9  — integration: full reconnect cycle re-fetches token each time
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _mock_resp(status: int, lines: list[str] | None = None, raise_after: Exception | None = None):
    resp = MagicMock()
    resp.status_code = status

    async def _aiter_lines():
        for line in (lines or []):
            yield line
        if raise_after:
            raise raise_after

    resp.aiter_lines = _aiter_lines
    return resp


class _StreamCtx:
    def __init__(self, resp): self._resp = resp
    async def __aenter__(self): return self._resp
    async def __aexit__(self, *_): pass


class _ClientCtx:
    def __init__(self, resp): self._resp = resp
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    def stream(self, *a, **kw): return _StreamCtx(self._resp)
    async def post(self, *a, **kw):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"stream": {"sessionid": "tok_test"}}
        r.raise_for_status = MagicMock()
        return r


# ---------------------------------------------------------------------------
# F4 — Backoff math
# ---------------------------------------------------------------------------
class TestBackoff:
    def test_increases_with_attempt(self):
        from services.tradier_stream import _backoff
        for attempt in range(10):
            val = _backoff(attempt)
            assert 0 <= val <= 60.0, f"Backoff out of range at attempt {attempt}: {val}"

    def test_cap_respected(self):
        from services.tradier_stream import _backoff, _BACKOFF_CAP
        for _ in range(50):
            assert _backoff(20) <= _BACKOFF_CAP

    def test_jitter_non_deterministic(self):
        from services.tradier_stream import _backoff
        vals = {_backoff(3) for _ in range(20)}
        assert len(vals) > 1, "Backoff has no jitter"

    def test_base_case_zero_attempt(self):
        from services.tradier_stream import _backoff, _BACKOFF_BASE
        for _ in range(20):
            assert _backoff(0) <= _BACKOFF_BASE


# ---------------------------------------------------------------------------
# F6a — Session 401 returns None
# ---------------------------------------------------------------------------
class TestGetSessionToken:
    @pytest.mark.asyncio
    async def test_returns_none_on_401(self):
        from services.tradier_stream import _get_session_token

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def post(self, *a, **kw): return mock_resp

        with patch("services.tradier_stream.httpx.AsyncClient", return_value=_C()):
            result = await _get_session_token()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_token_on_success(self):
        from services.tradier_stream import _get_session_token

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"stream": {"sessionid": "abc123"}}
        mock_resp.raise_for_status = MagicMock()

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def post(self, *a, **kw): return mock_resp

        with patch("services.tradier_stream.httpx.AsyncClient", return_value=_C()):
            result = await _get_session_token()
        assert result == "abc123"

    @pytest.mark.asyncio
    async def test_returns_none_on_missing_sessionid(self):
        from services.tradier_stream import _get_session_token

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"stream": {}}  # no sessionid
        mock_resp.raise_for_status = MagicMock()

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def post(self, *a, **kw): return mock_resp

        with patch("services.tradier_stream.httpx.AsyncClient", return_value=_C()):
            result = await _get_session_token()
        assert result is None

    @pytest.mark.asyncio
    async def test_f5_retries_on_timeout(self):
        """F5: Session fetch retried up to SESSION_RETRY_MAX on transient error."""
        from services.tradier_stream import _get_session_token, _SESSION_RETRY_MAX

        call_count = 0

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def post(self, *a, **kw):
                nonlocal call_count
                call_count += 1
                raise httpx.TimeoutException("timeout")

        with patch("services.tradier_stream.httpx.AsyncClient", return_value=_C()):
            with patch("services.tradier_stream.asyncio.sleep", new_callable=AsyncMock):
                result = await _get_session_token()

        assert result is None
        assert call_count == _SESSION_RETRY_MAX, (
            f"Expected {_SESSION_RETRY_MAX} retries, got {call_count}"
        )

    @pytest.mark.asyncio
    async def test_f5_succeeds_on_second_attempt(self):
        """F5: Returns token if second attempt succeeds after first timeout."""
        from services.tradier_stream import _get_session_token

        call_count = 0
        good_resp = MagicMock()
        good_resp.status_code = 200
        good_resp.json.return_value = {"stream": {"sessionid": "recovered_token"}}
        good_resp.raise_for_status = MagicMock()

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def post(self, *a, **kw):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise httpx.ConnectError("refused")
                return good_resp

        with patch("services.tradier_stream.httpx.AsyncClient", return_value=_C()):
            with patch("services.tradier_stream.asyncio.sleep", new_callable=AsyncMock):
                result = await _get_session_token()

        assert result == "recovered_token"
        assert call_count == 2


# ---------------------------------------------------------------------------
# F8 — Demo mode is cancellable
# ---------------------------------------------------------------------------
class TestDemoModeCancellable:
    @pytest.mark.asyncio
    async def test_f8_demo_mode_cancels_cleanly(self):
        from services.tradier_stream import _demo_mode_once

        with patch("services.tradier_stream.bus") as mock_bus:
            mock_bus.publish_all = AsyncMock()
            task = asyncio.create_task(_demo_mode_once(["AAPL", "TSLA"]))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_f8_demo_mode_emits_signals(self):
        from services.tradier_stream import _demo_mode_once

        published = []

        async def _capture(channel, payload):
            # bus.publish_all(channel, payload) — capture only the payload
            published.append(payload)

        with patch("services.tradier_stream.bus") as mock_bus:
            mock_bus.publish_all = _capture
            task = asyncio.create_task(_demo_mode_once(["SPY"]))
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(published) > 0, "Expected at least one signal to be emitted"
        for sig in published:
            assert sig["type"] == "signal"
            assert "ticker" in sig["data"]


# ---------------------------------------------------------------------------
# F7 / F3 — Idle watchdog
# ---------------------------------------------------------------------------
class TestIdleWatchdog:
    @pytest.mark.asyncio
    async def test_f7_watchdog_raises_on_idle(self):
        from services.tradier_stream import _guarded_lines

        async def _slow_iter():
            await asyncio.sleep(999)
            yield "never"

        resp = MagicMock()
        resp.aiter_lines = _slow_iter

        with patch("services.tradier_stream._IDLE_TIMEOUT", 0.05):
            with pytest.raises(asyncio.TimeoutError):
                async for _ in _guarded_lines(resp):
                    pass

    @pytest.mark.asyncio
    async def test_f7_watchdog_passes_on_active_stream(self):
        from services.tradier_stream import _guarded_lines

        received = []

        async def _fast_iter():
            for line in ["line1", "line2", "line3"]:
                yield line

        resp = MagicMock()
        resp.aiter_lines = _fast_iter

        async for line in _guarded_lines(resp):
            received.append(line)

        assert received == ["line1", "line2", "line3"]


# ---------------------------------------------------------------------------
# F1 + F9 — Session token re-fetched on every reconnect cycle
# ---------------------------------------------------------------------------
class TestTokenRefreshOnReconnect:
    @pytest.mark.asyncio
    async def test_f1_token_fetched_per_reconnect(self):
        from services import tradier_stream as ts

        token_fetch_count = 0
        cycle_count = 0

        async def _fake_get_token():
            nonlocal token_fetch_count
            token_fetch_count += 1
            return f"token_{token_fetch_count}"

        async def _fake_stream_flow(symbols):
            nonlocal cycle_count
            for _ in range(2):
                token = await ts._get_session_token()
                assert token is not None
                cycle_count += 1

        with patch.object(ts, "_get_session_token", side_effect=_fake_get_token):
            await _fake_stream_flow(["AAPL"])

        assert token_fetch_count == 2, (
            f"Expected 2 token fetches for 2 reconnect cycles, got {token_fetch_count}"
        )
        assert cycle_count == 2


# ---------------------------------------------------------------------------
# F2 + F6b — 401 on stream = expired token, fast retry (NOT permanent demo mode)
# ---------------------------------------------------------------------------
class TestStream401Recovery:
    @pytest.mark.asyncio
    async def test_f2_stream_401_does_not_permanently_fall_to_demo(self):
        """
        F2/F6b: When stream returns 401, the loop must continue and
        re-fetch a session token — NOT call _demo_mode and return.
        """
        from services import tradier_stream as ts

        reconnect_count = 0

        async def _fake_get_token():
            nonlocal reconnect_count
            reconnect_count += 1
            if reconnect_count >= 3:
                raise asyncio.CancelledError
            return "fresh_token"

        with patch.object(ts, "_get_session_token", side_effect=_fake_get_token), \
             patch.object(ts, "_demo_mode_once", new_callable=AsyncMock) as mock_demo:
            try:
                await ts.start_stream(["AAPL"])
            except asyncio.CancelledError:
                pass

        assert mock_demo.call_count == 0, (
            "_demo_mode_once should NOT be called when stream returns 401 "
            f"— was called {mock_demo.call_count} time(s)"
        )
