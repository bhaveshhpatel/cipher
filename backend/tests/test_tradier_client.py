"""
Tests for utils/tradier_client.py

Covers:
  B-022 — Global Session Token Semaphore (max 3 concurrent)
  B-023 — Explicit 429 Handling with Retry-After

  TC-01  _SESSION_SEM is asyncio.Semaphore with value 3
  TC-02  _DEFAULT_RETRY_AFTER_S is 10.0
  TC-03  get_session_token() acquires _SESSION_SEM (only 3 concurrent at a time)
  TC-04  get_session_token() returns token on 200 response
  TC-05  get_session_token() returns None on 401 response
  TC-06  get_session_token() sleeps Retry-After value on 429 then retries
  TC-07  get_session_token() uses _DEFAULT_RETRY_AFTER_S when Retry-After header absent
  TC-08  get_session_token() returns None after 3 failed attempts (timeout/connect errors)
"""
import asyncio
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ============================================================
# B-022 — Semaphore constants
# ============================================================

class TestB022SemaphoreConstants:

    # TC-01: _SESSION_SEM is a Semaphore with value 3
    def test_session_sem_is_semaphore_value_3(self):
        from utils.tradier_client import _SESSION_SEM
        assert isinstance(_SESSION_SEM, asyncio.Semaphore)
        # asyncio.Semaphore stores initial value in _value
        assert _SESSION_SEM._value == 3

    # TC-02: _DEFAULT_RETRY_AFTER_S is 10.0
    def test_default_retry_after_is_10(self):
        from utils.tradier_client import _DEFAULT_RETRY_AFTER_S
        assert abs(_DEFAULT_RETRY_AFTER_S - 10.0) < 1e-9


# ============================================================
# B-022 — Semaphore concurrency enforcement
# ============================================================

class TestB022SemaphoreConcurrency:

    # TC-03: at most 3 concurrent get_session_token() calls inside semaphore
    def test_max_3_concurrent_token_fetches(self):
        """
        Launch 6 concurrent get_session_token() calls. Count the maximum
        number that are inside the semaphore at the same time — must be ≤3.
        """
        from utils.tradier_client import _SESSION_SEM

        concurrent_peak = {"n": 0, "max": 0}
        release_event = asyncio.Event()

        async def _test():
            async def fake_post_slow(*args, **kwargs):
                # Simulates a slow token fetch — holds semaphore until released
                concurrent_peak["n"] += 1
                concurrent_peak["max"] = max(concurrent_peak["max"], concurrent_peak["n"])
                await release_event.wait()
                concurrent_peak["n"] -= 1
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"stream": {"sessionid": "tok"}}
                return resp

            # Patch httpx.AsyncClient to use our fake slow POST
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=fake_post_slow)

            with patch("utils.tradier_client.httpx.AsyncClient", return_value=mock_client):
                tasks = [asyncio.create_task(_call_get_session_token()) for _ in range(6)]
                # Let 3 acquire and block inside semaphore
                await asyncio.sleep(0.05)
                # Release all
                release_event.set()
                await asyncio.gather(*tasks, return_exceptions=True)

        async def _call_get_session_token():
            from utils import tradier_client
            return await tradier_client.get_session_token()

        _run(_test())
        # Peak concurrency inside semaphore must never exceed 3
        assert concurrent_peak["max"] <= 3


# ============================================================
# B-022 + B-023 — get_session_token() response handling
# ============================================================

class TestGetSessionToken:

    def _mock_response(self, status_code: int, json_data: dict = None,
                       headers: dict = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data or {}
        resp.headers = headers or {}
        resp.text = ""
        resp.raise_for_status = MagicMock()  # no-op unless explicitly raised
        return resp

    def _make_client(self, resp):
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=resp)
        return mock_client

    # TC-04: returns token on 200
    def test_returns_token_on_200(self):
        async def _test():
            resp = self._mock_response(
                200, json_data={"stream": {"sessionid": "abc123"}}
            )
            with patch("utils.tradier_client.httpx.AsyncClient",
                       return_value=self._make_client(resp)):
                from utils import tradier_client
                result = await tradier_client.get_session_token()
            return result

        assert _run(_test()) == "abc123"

    # TC-05: returns None on 401
    def test_returns_none_on_401(self):
        async def _test():
            resp = self._mock_response(401)
            with patch("utils.tradier_client.httpx.AsyncClient",
                       return_value=self._make_client(resp)):
                from utils import tradier_client
                result = await tradier_client.get_session_token()
            return result

        assert _run(_test()) is None

    # TC-06: sleeps Retry-After on 429 then retries
    def test_sleeps_retry_after_on_429_then_retries(self):
        sleep_calls = []

        async def _test():
            # First call returns 429 with Retry-After: 15, second returns 200
            resp_429 = self._mock_response(
                429, headers={"Retry-After": "15"}
            )
            resp_200 = self._mock_response(
                200, json_data={"stream": {"sessionid": "tok_after_retry"}}
            )

            call_count = {"n": 0}

            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            async def fake_post(*args, **kwargs):
                call_count["n"] += 1
                return resp_429 if call_count["n"] == 1 else resp_200

            mock_client.post = AsyncMock(side_effect=fake_post)

            async def fake_sleep(secs):
                sleep_calls.append(secs)

            with patch("utils.tradier_client.httpx.AsyncClient",
                       return_value=mock_client), \
                 patch("utils.tradier_client.asyncio.sleep",
                       side_effect=fake_sleep):
                from utils import tradier_client
                result = await tradier_client.get_session_token()
            return result

        token = _run(_test())
        assert token == "tok_after_retry"
        # Must have slept exactly the Retry-After value
        assert len(sleep_calls) >= 1
        assert abs(sleep_calls[0] - 15.0) < 1e-9

    # TC-07: uses _DEFAULT_RETRY_AFTER_S when header absent
    def test_uses_default_retry_after_when_header_absent(self):
        from utils.tradier_client import _DEFAULT_RETRY_AFTER_S
        sleep_calls = []

        async def _test():
            resp_429 = self._mock_response(429)  # no Retry-After header
            resp_200 = self._mock_response(
                200, json_data={"stream": {"sessionid": "tok_default"}}
            )

            call_count = {"n": 0}
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            async def fake_post(*args, **kwargs):
                call_count["n"] += 1
                return resp_429 if call_count["n"] == 1 else resp_200

            mock_client.post = AsyncMock(side_effect=fake_post)

            async def fake_sleep(secs):
                sleep_calls.append(secs)

            with patch("utils.tradier_client.httpx.AsyncClient",
                       return_value=mock_client), \
                 patch("utils.tradier_client.asyncio.sleep",
                       side_effect=fake_sleep):
                from utils import tradier_client
                result = await tradier_client.get_session_token()
            return result

        token = _run(_test())
        assert token == "tok_default"
        assert len(sleep_calls) >= 1
        assert abs(sleep_calls[0] - _DEFAULT_RETRY_AFTER_S) < 1e-9

    # TC-08: returns None after 3 network failures
    def test_returns_none_after_3_timeouts(self):
        import httpx

        async def _test():
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                side_effect=httpx.TimeoutException("timeout")
            )

            async def fake_sleep(secs):
                pass  # don't actually wait

            with patch("utils.tradier_client.httpx.AsyncClient",
                       return_value=mock_client), \
                 patch("utils.tradier_client.asyncio.sleep",
                       side_effect=fake_sleep):
                from utils import tradier_client
                result = await tradier_client.get_session_token()
            return result

        assert _run(_test()) is None
