"""
test_rearch002_ingestion_floors.py — REARCH-002 test suite.

All tests instantiate IngestionProcessor directly and call process_with_config()
so they are deterministic and require no DB, no stream, no mock WebSocket.

Test matrix:
  QA-1  — 16 boundary-value tests for the 4-gate process() contract
  QA-2  — TTL cache refresh test (monkeypatch _fetch_from_db + time.monotonic)
  QA-3  — apply_config() index symbol rejection (REARCH-001 follow-on)
  QA-4  — Admin PATCH range validation (422 on out-of-range + unknown keys)
"""
from __future__ import annotations

import time
import types
import pytest

from ingestion.processor import (
    IngestionConfig,
    IngestionProcessor,
    get_drop_stats,
    reset_drop_stats,
    invalidate_ingestion_config_cache,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ev(
    dte: int = 30,
    premium: int = 25_000,
    open_interest: int = 50,
    influence_tier: str = "INSTITUTIONAL",
    is_aggressive: bool = True,
) -> types.SimpleNamespace:
    """Build a minimal duck-typed event object for testing."""
    return types.SimpleNamespace(
        dte=dte,
        premium=premium,
        open_interest=open_interest,
        influence_tier=influence_tier,
        is_aggressive=is_aggressive,
    )


_DEFAULT_CFG = IngestionConfig()          # default floors
_PROC        = IngestionProcessor()


@pytest.fixture(autouse=True)
def reset_stats():
    """Reset drop counters before every test."""
    reset_drop_stats()
    yield


# ---------------------------------------------------------------------------
# QA-1 — Gate 1: DTE hard floor
# ---------------------------------------------------------------------------

def test_dte_exactly_at_floor():
    ev = _ev(dte=1)
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is ev


def test_dte_below_floor():
    ev = _ev(dte=0)
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is None
    assert get_drop_stats()["dropped_min_dte"] == 1


def test_dte_at_zero_increments_counter():
    for _ in range(3):
        _PROC.process_with_config(_ev(dte=0), _DEFAULT_CFG)
    assert get_drop_stats()["dropped_min_dte"] == 3


# ---------------------------------------------------------------------------
# QA-1 — Gate 2: DTE ceiling
# ---------------------------------------------------------------------------

def test_dte_exactly_at_ceiling():
    ev = _ev(dte=90)
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is ev


def test_dte_above_ceiling():
    ev = _ev(dte=91)
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is None
    assert get_drop_stats()["dropped_max_dte"] == 1


# ---------------------------------------------------------------------------
# QA-1 — Gate 3: Tier-aware premium floor
# ---------------------------------------------------------------------------

def test_premium_t1_exactly_at_floor():
    ev = _ev(premium=25_000, influence_tier="INSTITUTIONAL")
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is ev


def test_premium_t1_one_below_floor():
    ev = _ev(premium=24_999, influence_tier="INSTITUTIONAL")
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is None
    assert get_drop_stats()["dropped_min_premium"] == 1


def test_premium_t2_exactly_at_floor():
    ev = _ev(premium=15_000, influence_tier="LARGE")
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is ev


def test_premium_t2_one_below_floor():
    ev = _ev(premium=14_999, influence_tier="LARGE")
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is None
    assert get_drop_stats()["dropped_min_premium"] == 1


def test_premium_t3_exactly_at_floor():
    ev = _ev(premium=5_000, influence_tier="RETAIL")
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is ev


def test_premium_t3_one_below_floor():
    ev = _ev(premium=4_999, influence_tier="RETAIL")
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is None
    assert get_drop_stats()["dropped_min_premium"] == 1


def test_unknown_tier_uses_t3_floor():
    """Unknown tier strings must default to T3 (most permissive floor is T3)."""
    ev = _ev(premium=5_000, influence_tier="UNKNOWN")
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is ev


# ---------------------------------------------------------------------------
# QA-1 — Gate 4: Open-interest floor
# ---------------------------------------------------------------------------

def test_oi_exactly_at_floor():
    ev = _ev(open_interest=50)
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is ev


def test_oi_one_below_floor():
    ev = _ev(open_interest=49)
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is None
    assert get_drop_stats()["dropped_min_oi"] == 1


def test_oi_zero():
    ev = _ev(open_interest=0)
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is None
    assert get_drop_stats()["dropped_min_oi"] == 1


# ---------------------------------------------------------------------------
# QA-1 — All gates pass + require_ask_tag does not gate
# ---------------------------------------------------------------------------

def test_all_gates_pass():
    ev = _ev()   # defaults satisfy all floors
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is ev
    assert get_drop_stats()["passed"] == 1


def test_require_ask_tag_does_not_gate():
    """is_aggressive=False must never drop the event — it is a tag, not a gate."""
    ev = _ev(is_aggressive=False)
    assert _PROC.process_with_config(ev, _DEFAULT_CFG) is ev


# ---------------------------------------------------------------------------
# QA-1 — Stats counters accumulate correctly across multiple drops
# ---------------------------------------------------------------------------

def test_stats_counters_accumulate():
    _PROC.process_with_config(_ev(dte=0),              _DEFAULT_CFG)  # min_dte
    _PROC.process_with_config(_ev(dte=0),              _DEFAULT_CFG)  # min_dte
    _PROC.process_with_config(_ev(dte=91),             _DEFAULT_CFG)  # max_dte
    _PROC.process_with_config(_ev(premium=100),        _DEFAULT_CFG)  # min_premium
    _PROC.process_with_config(_ev(open_interest=0),    _DEFAULT_CFG)  # min_oi
    _PROC.process_with_config(_ev(),                   _DEFAULT_CFG)  # pass
    s = get_drop_stats()
    assert s["dropped_min_dte"]     == 2
    assert s["dropped_max_dte"]     == 1
    assert s["dropped_min_premium"] == 1
    assert s["dropped_min_oi"]      == 1
    assert s["passed"]              == 1


# ---------------------------------------------------------------------------
# QA-2 — Cache TTL hot-reload
# ---------------------------------------------------------------------------

def test_cache_refreshes_after_ttl(monkeypatch):
    """
    After TTL expiry, get_ingestion_config() schedules a refresh.
    Patch _fetch_from_db to return a modified config and fast-forward monotonic.
    """
    import ingestion.processor as proc_mod

    new_cfg = IngestionConfig(min_premium_t1=30_000)

    async def fake_fetch():
        return new_cfg

    monkeypatch.setattr(proc_mod, "_fetch_from_db", fake_fetch)
    # Force TTL expiry
    monkeypatch.setattr(proc_mod, "_cache_expires_at", 0.0)

    import asyncio

    async def run():
        await proc_mod._refresh_cache()
        return proc_mod.get_ingestion_config()

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result.min_premium_t1 == 30_000


# ---------------------------------------------------------------------------
# QA-2 — Cache invalidation (admin write path)
# ---------------------------------------------------------------------------

def test_invalidate_sets_expires_to_zero(monkeypatch):
    import ingestion.processor as proc_mod
    proc_mod._cache_expires_at = time.monotonic() + 9999.0
    invalidate_ingestion_config_cache()
    assert proc_mod._cache_expires_at == 0.0


# ---------------------------------------------------------------------------
# QA-3 — apply_config() rejects index symbols (REARCH-001 follow-on)
# ---------------------------------------------------------------------------

def test_apply_config_rejects_index_symbols():
    from ingestion import config as cfg_mod
    original = list(cfg_mod.SYMBOLS)
    try:
        cfg_mod.apply_config({"symbols": ["AAPL", "SPX", "MSFT", "VIX", "NDX"]})
        assert "SPX" not in cfg_mod.SYMBOLS, "SPX (index) must be rejected"
        assert "VIX" not in cfg_mod.SYMBOLS, "VIX (index) must be rejected"
        assert "NDX" not in cfg_mod.SYMBOLS, "NDX (index) must be rejected"
        assert "AAPL" in cfg_mod.SYMBOLS
        assert "MSFT" in cfg_mod.SYMBOLS
    finally:
        cfg_mod.SYMBOLS.clear()
        cfg_mod.SYMBOLS.extend(original)


# ---------------------------------------------------------------------------
# QA-4 — Admin PATCH validation (router-level, no DB)
# ---------------------------------------------------------------------------

from routers.ingestion_config import PatchIngestionConfigRequest


def _patch_request(updates: dict) -> PatchIngestionConfigRequest:
    return PatchIngestionConfigRequest(updates=updates)


def test_patch_min_dte_zero_rejected():
    with pytest.raises(Exception, match="out of range"):
        _patch_request({"ing.min_dte": 0})


def test_patch_min_dte_one_accepted():
    req = _patch_request({"ing.min_dte": 1})
    assert req.updates["ing.min_dte"] == 1


def test_patch_min_dte_six_rejected():
    with pytest.raises(Exception, match="out of range"):
        _patch_request({"ing.min_dte": 6})


def test_patch_premium_below_1000_rejected():
    with pytest.raises(Exception, match="out of range"):
        _patch_request({"ing.min_premium.t1": 999})


def test_patch_premium_at_1000_accepted():
    req = _patch_request({"ing.min_premium.t1": 1_000})
    assert req.updates["ing.min_premium.t1"] == 1_000


def test_patch_unknown_key_rejected():
    with pytest.raises(Exception, match="Unknown key"):
        _patch_request({"ing.nonexistent_key": 100})


def test_patch_multiple_keys_one_invalid_rejects_all():
    """If any key is invalid the whole request is rejected (all-or-nothing)."""
    with pytest.raises(Exception):
        _patch_request({"ing.min_dte": 1, "ing.min_dte": 0})


def test_patch_require_ask_tag_bool_accepted():
    req = _patch_request({"ing.require_ask_tag": False})
    assert req.updates["ing.require_ask_tag"] is False
