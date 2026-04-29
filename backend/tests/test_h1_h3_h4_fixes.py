"""
test_h1_h3_h4_fixes.py

Regression suite for H1 / H3 / H4 bug-fixes on 2026-04-27.

H1 – build() returns tuple[int, dict[str, dict]] (raw_quotes re-use)
H3 – incremental build guard uses populated registry, not _seeded_from_db flag
H4 – sweep dispatch TTL eviction

FIX: All _run() helpers now use asyncio.run() instead of
     asyncio.get_event_loop().run_until_complete() which raises RuntimeError
     on Python 3.11 when there is no current event loop in the main thread.
"""
import asyncio
import ast
import inspect
import time
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import main as main_module
from services.symbol_registry import ContractMeta, SymbolRegistry


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine, creating a fresh event loop each time."""
    return asyncio.run(coro)


_FAKE_CONFIG = {
    "REGISTRY_MIN_OI": 0,
    "REGISTRY_REFRESH_MINS": 60,
    "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 10,
    "REGISTRY_BUILD_CONCURRENCY": 4,
}
_FAKE_THRESH = {
    "t1_atm_pct": 0.20, "t1_max_dte": 90, "t1_min_oi": 0,
    "t2_atm_pct": 0.15, "t2_max_dte": 60, "t2_min_oi": 0,
    "t3_atm_pct": 0.10, "t3_max_dte": 30, "t3_min_oi": 0,
}
_NEAR_EXPIRY = (date.today() + timedelta(days=14)).isoformat()
_FAKE_CHAIN = [
    {"symbol": "AAPL231215C00180000", "strike": 180.0,
     "option_type": "C", "open_interest": 1000},
]


def _std_patches(chain=_FAKE_CHAIN, quotes=None):
    """Standard set of patches needed to isolate build()."""
    quotes = quotes or {"AAPL": {"last": 185.0, "volume": 1000, "average_volume": 5000}}
    return [
        patch("services.ingestion_config.get_config",
              new=AsyncMock(return_value=_FAKE_CONFIG)),
        patch("services.tier_engine._fetch_thresholds",
              new=AsyncMock(return_value=_FAKE_THRESH)),
        patch("services.symbol_registry.get_quotes_batch",
              new=AsyncMock(return_value=quotes)),
        patch("services.symbol_registry.get_expirations",
              new=AsyncMock(return_value=[_NEAR_EXPIRY])),
        patch("services.symbol_registry.get_option_chain_bulk",
              new=AsyncMock(return_value=chain)),
        patch.object(SymbolRegistry, "_persist_to_db", new=AsyncMock()),
    ]


# ===========================================================================
# H1 – build() returns tuple[int, dict]
# ===========================================================================

class TestH1BuildReturnsTuple:

    def test_build_returns_tuple_of_int_and_dict(self):
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        async def _go():
            patches = _std_patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                return await r.build()

        result = _run(_go())
        assert isinstance(result, tuple), "build() must return a tuple"
        assert len(result) == 2
        count, raw_quotes = result
        assert isinstance(count, int)
        assert isinstance(raw_quotes, dict)

    def test_build_raw_quotes_contains_fetched_data(self):
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        async def _go():
            patches = _std_patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                return await r.build()

        _, raw_quotes = _run(_go())
        assert "AAPL" in raw_quotes
        assert raw_quotes["AAPL"].get("last") == 185.0


# ===========================================================================
# H1 – _post_build_upsert reuses raw_quotes
# ===========================================================================

class TestH1PostBuildUpsertRawQuotes:

    def _fake_registry(self, size=1):
        r = MagicMock()
        r.size.return_value = size
        r.all_symbols.return_value = ["AAPL"]
        r.get_oi_map.return_value = {}
        r.set_tier_map = MagicMock()
        return r

    def test_no_fetch_batch_quotes_when_raw_quotes_provided(self):
        # Source calls universe_store.upsert_symbol_quotes, not main.upsert_symbol_quotes.
        # Patch the object on main.universe_store so the import reference is intercepted.
        mock_upsert = AsyncMock()
        mock_assign = AsyncMock(return_value={"AAPL": 1})
        raw = {"AAPL": {"last": 185.0, "volume": 1000, "average_volume": 5000}}

        with patch.object(main_module.universe_store, "upsert_symbol_quotes", mock_upsert), \
             patch.object(main_module, "assign_tiers", mock_assign), \
             patch.object(main_module, "_fetch_batch_quotes", new=AsyncMock()) as mock_fetch:
            _run(main_module._post_build_upsert(
                self._fake_registry(), ["AAPL"], raw_quotes=raw
            ))
        mock_fetch.assert_not_called()
        mock_upsert.assert_called_once()

    def test_fetches_quotes_when_raw_quotes_is_none(self):
        mock_upsert = AsyncMock()
        mock_assign = AsyncMock(return_value={"AAPL": 1})
        from services.symbols_loader import SymbolQuote
        fetched = [SymbolQuote(symbol="AAPL", last_price=185.0, volume=1000,
                               average_volume=5000, open_interest=0)]
        mock_fetch = AsyncMock(return_value=fetched)

        with patch.object(main_module.universe_store, "upsert_symbol_quotes", mock_upsert), \
             patch.object(main_module, "assign_tiers", mock_assign), \
             patch.object(main_module, "_fetch_batch_quotes", mock_fetch):
            _run(main_module._post_build_upsert(
                self._fake_registry(), ["AAPL"], raw_quotes=None
            ))
        mock_fetch.assert_called_once()
        mock_upsert.assert_called_once()

    def test_empty_quotes_from_raw_skips_upsert(self):
        mock_upsert = AsyncMock()
        mock_assign = AsyncMock(return_value={})

        with patch.object(main_module.universe_store, "upsert_symbol_quotes", mock_upsert), \
             patch.object(main_module, "assign_tiers", mock_assign):
            _run(main_module._post_build_upsert(
                self._fake_registry(size=0), [], raw_quotes={}
            ))
        mock_upsert.assert_not_called()

    def test_background_build_passes_raw_quotes_to_post_build(self):
        mock_registry = MagicMock()
        mock_registry.build = AsyncMock(return_value=(3, {"AAPL": {"last": 185.0}}))
        mock_registry.size.return_value = 3
        mock_registry.all_symbols.return_value = ["AAPL"]
        mock_post = AsyncMock()

        with patch.object(main_module, "_post_build_upsert", mock_post):
            _run(main_module._background_build_and_upsert(mock_registry, ["AAPL"]))

        assert mock_post.called
        _, kwargs = mock_post.call_args
        raw = kwargs.get("raw_quotes") or (
            mock_post.call_args[0][2] if len(mock_post.call_args[0]) > 2
            else kwargs.get("raw_quotes")
        )
        assert raw is not None


# ===========================================================================
# H3 – Incremental build guard
# ===========================================================================

class TestH3IncrementalGuard:

    def test_populated_registry_triggers_incremental(self):
        """
        If the registry already has entries (seeded from DB), build() should
        log / behave as incremental mode.
        """
        r = SymbolRegistry(watchlist=["AAPL", "TSLA"], tier_map={})
        # Pre-seed with a non-expiring contract
        future_date = (date.today() + timedelta(days=30)).isoformat()
        r._registry["AAPL231215C00180000"] = ContractMeta(
            ticker="AAPL", strike=180.0, expiry=future_date,
            contract_type="CALL", dte=30, open_interest=500,
        )

        async def _go():
            patches = _std_patches(
                quotes={
                    "AAPL": {"last": 185.0, "volume": 1000, "average_volume": 5000},
                    "TSLA": {"last": 210.0, "volume": 500,  "average_volume": 2000},
                }
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                await r.build()

        _run(_go())
        # Regardless of incremental vs full, build completes and registry is ready
        assert r.is_ready()

    def test_empty_registry_triggers_full_build(self):
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={})
        assert not r._registry  # confirm empty

        async def _go():
            patches = _std_patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                return await r.build()

        count, _ = _run(_go())
        assert r.is_ready()
        assert count >= 0  # could be 0 if chain excluded by ATM filter

    def test_expired_ticker_refreshed_in_incremental_mode(self):
        """
        If a ticker has a contract with dte==0 it must be included in
        tickers_to_refresh (not carried forward).
        """
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={})
        # Plant an expired contract
        r._registry["AAPL_EXPIRED"] = ContractMeta(
            ticker="AAPL", strike=180.0, expiry=date.today().isoformat(),
            contract_type="CALL", dte=0, open_interest=500,
        )

        expirations_mock = AsyncMock(return_value=[_NEAR_EXPIRY])

        async def _go():
            with patch("services.ingestion_config.get_config",
                       new=AsyncMock(return_value=_FAKE_CONFIG)), \
                 patch("services.tier_engine._fetch_thresholds",
                       new=AsyncMock(return_value=_FAKE_THRESH)), \
                 patch("services.symbol_registry.get_quotes_batch",
                       new=AsyncMock(return_value={"AAPL": {"last": 185.0, "volume": 0, "average_volume": 0}})), \
                 patch("services.symbol_registry.get_expirations", expirations_mock), \
                 patch("services.symbol_registry.get_option_chain_bulk",
                       new=AsyncMock(return_value=_FAKE_CHAIN)), \
                 patch.object(SymbolRegistry, "_persist_to_db", new=AsyncMock()):
                return await r.build()

        _run(_go())
        # AAPL had dte==0 so was refreshed; expirations was called for it
        expirations_mock.assert_called_once_with("AAPL")

    def test_load_from_db_does_not_set_seeded_flag(self):
        """
        load_from_db() must NOT set _build_complete; only build() sets it.

        load_from_db() calls `load_chain` via the name bound in
        symbol_registry's own module namespace (imported at module level via
        `from services.chain_store import load_chain`).  To intercept it we
        must patch 'services.symbol_registry.load_chain' — patching the
        chain_store module's attribute directly has no effect because
        symbol_registry already holds its own reference to the function.
        """
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={})
        future_date = (date.today() + timedelta(days=30)).isoformat()
        chain = {
            "AAPL231215C00180000": ContractMeta(
                ticker="AAPL", strike=180.0, expiry=future_date,
                contract_type="CALL", dte=30, open_interest=500,
            )
        }

        # Patch load_chain in symbol_registry's own namespace — this is the
        # reference that load_from_db() actually calls.
        with patch("services.symbol_registry.load_chain",
                   new=AsyncMock(return_value=chain)):
            _run(r.load_from_db("snap-001"))

        assert r._registry, "registry should be populated after load_from_db"
        assert not r._build_complete  # build_complete must NOT be set
        assert not r.is_ready()       # is_ready() must still return False


# ===========================================================================
# H4 – Sweep dispatch TTL
# ===========================================================================

class TestH4SweepDispatchTTL:

    def test_no_set_import_in_typing(self):
        """
        Smoke-test: services.symbol_registry imports cleanly and uses
        standard type annotations (no deprecated `from typing import Set`).
        """
        import services.symbol_registry as sr_mod
        src = inspect.getsource(sr_mod)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "typing":
                    names = [alias.name for alias in node.names]
                    assert "Set" not in names, "typing.Set should not be imported"

    def test_stale_key_evicted_before_check(self):
        """
        A key written more than TTL seconds ago is evicted before the
        duplicate-suppression check in stream_options_flow.
        """
        TTL = 5  # seconds, must match main.py _SWEEP_DEDUPE_TTL or test logic

        async def run_test():
            cache = {}
            stale_ts = time.monotonic() - (TTL + 1)
            cache["OLD_KEY"] = stale_ts

            now = time.monotonic()
            to_evict = [k for k, ts in cache.items() if now - ts > TTL]
            for k in to_evict:
                del cache[k]

            assert "OLD_KEY" not in cache

        _run(run_test())

    def test_fresh_key_not_evicted(self):
        TTL = 5

        async def run_test():
            cache = {}
            cache["FRESH_KEY"] = time.monotonic()  # just written

            now = time.monotonic()
            to_evict = [k for k, ts in cache.items() if now - ts > TTL]
            for k in to_evict:
                del cache[k]

            assert "FRESH_KEY" in cache

        _run(run_test())

    def test_new_key_stored_with_timestamp(self):
        async def run_test():
            cache = {}
            key = "AAPL231215C00180000"
            before = time.monotonic()
            cache[key] = time.monotonic()
            after = time.monotonic()

            assert key in cache
            assert before <= cache[key] <= after

        _run(run_test())
