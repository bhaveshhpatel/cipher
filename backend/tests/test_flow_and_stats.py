"""
Regression tests for flow_store module-level helpers and stats functions.

flow_store.py exposes standalone functions, not a FlowStore class.
All tests cover the public API that actually exists in the module.
"""
import asyncio
import sys
import types

# ---------------------------------------------------------------------------
# Stub out heavy deps before importing the module under test
# ---------------------------------------------------------------------------
for _mod in (
    "httpx",
    "core",
    "core.async_bus",
    "services",
    "services.chain_store",
    "ingestion",
    "ingestion.processor",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# Provide a minimal bus stub with a subscribe no-op
_bus_stub = sys.modules["core.async_bus"]
if not hasattr(_bus_stub, "bus"):
    class _BusStub:
        def subscribe(self, *a, **kw):
            raise RuntimeError("stub")
    _bus_stub.bus = _BusStub()

import services.flow_store as fs  # noqa: E402  (import after stubs)


# ---------------------------------------------------------------------------
# enqueue_lookback / get_lookback_stats
# ---------------------------------------------------------------------------

def test_lookback_stats_are_dict():
    stats = fs.get_lookback_stats()
    assert isinstance(stats, dict)


def test_lookback_stats_initial_keys():
    stats = fs.get_lookback_stats()
    assert "lookback_queued" in stats
    assert "lookback_queue_overflow" in stats


def test_enqueue_lookback_increments_queued():
    before = fs.get_lookback_stats()["lookback_queued"]
    fs.enqueue_lookback(("AAPL", "call", 150.0, "2026-06-20"))
    after = fs.get_lookback_stats()["lookback_queued"]
    assert after == before + 1


def test_lookback_stats_returns_copy():
    s1 = fs.get_lookback_stats()
    s1["lookback_queued"] = -999
    s2 = fs.get_lookback_stats()
    assert s2["lookback_queued"] != -999


# ---------------------------------------------------------------------------
# get_episode_stats
# ---------------------------------------------------------------------------

def test_episode_stats_are_dict():
    stats = fs.get_episode_stats()
    assert isinstance(stats, dict)


def test_episode_stats_initial_keys():
    stats = fs.get_episode_stats()
    assert "created_episodes" in stats
    assert "merged_episodes" in stats


def test_episode_stats_returns_copy():
    s1 = fs.get_episode_stats()
    s1["created_episodes"] = -999
    s2 = fs.get_episode_stats()
    assert s2["created_episodes"] != -999


# ---------------------------------------------------------------------------
# reset_episode_state
# ---------------------------------------------------------------------------

def test_reset_episode_state_clears_locks_and_inflight():
    # Populate both dicts first
    key = "TEST|bullish|call|100.0|2026-01-17"
    fs._episode_locks[key] = asyncio.Lock()
    fs._episode_in_flight[key] = {"id": 1, "trade_count": 1, "total_premium": 1000.0}
    fs.reset_episode_state()
    assert key not in fs._episode_locks
    assert key not in fs._episode_in_flight


def test_reset_episode_state_is_idempotent():
    fs.reset_episode_state()
    fs.reset_episode_state()  # second call must not raise


# ---------------------------------------------------------------------------
# _classify_bid_ask
#
# Boundary spec (REARCH-003): fill >= ask * 0.98 → ASK; fill <= bid * 1.02 → BID.
# Mid test must use a fill clearly inside mid territory, NOT on the boundary.
# ---------------------------------------------------------------------------

def test_classify_bid_ask_ask_side():
    cls, is_ask = fs._classify_bid_ask(5.00, 4.80, 5.00)
    assert cls == "ASK"
    assert is_ask is True


def test_classify_bid_ask_bid_side():
    cls, is_ask = fs._classify_bid_ask(4.80, 4.80, 5.00)
    assert cls == "BID"
    assert is_ask is False


def test_classify_bid_ask_mid():
    # 4.80 * 1.02 = 4.896  |  5.00 * 0.98 = 4.90
    # fill=4.92 is strictly between both thresholds → MID
    cls, is_ask = fs._classify_bid_ask(4.92, 4.80, 5.00)
    assert cls == "MID"
    assert is_ask is False


def test_classify_bid_ask_none_ask():
    cls, is_ask = fs._classify_bid_ask(5.00, 4.80, None)
    assert cls == "MID"
    assert is_ask is False


def test_classify_bid_ask_zero_ask():
    cls, is_ask = fs._classify_bid_ask(5.00, 4.80, 0)
    assert cls == "MID"
    assert is_ask is False


# ---------------------------------------------------------------------------
# _compute_vol_oi_signal
# ---------------------------------------------------------------------------

def test_vol_oi_signal_high():
    assert fs._compute_vol_oi_signal(1000, 1000, 0.5) == "HIGH"


def test_vol_oi_signal_normal():
    assert fs._compute_vol_oi_signal(100, 1000, 0.5) == "NORMAL"


def test_vol_oi_signal_none_volume():
    assert fs._compute_vol_oi_signal(None, 1000) == "UNKNOWN"


def test_vol_oi_signal_none_oi():
    assert fs._compute_vol_oi_signal(500, None) == "UNKNOWN"


def test_vol_oi_signal_zero_oi():
    assert fs._compute_vol_oi_signal(500, 0) == "UNKNOWN"


# ---------------------------------------------------------------------------
# _compute_normalized_premium
# ---------------------------------------------------------------------------

def test_normalized_premium_basic():
    result = fs._compute_normalized_premium(500.0, 100.0)
    assert result == 5.0


def test_normalized_premium_rounds_to_4dp():
    result = fs._compute_normalized_premium(1.0, 3.0)
    assert result == round(1 / 3, 4)


def test_normalized_premium_none_underlying():
    assert fs._compute_normalized_premium(500.0, None) is None


def test_normalized_premium_zero_underlying():
    assert fs._compute_normalized_premium(500.0, 0) is None


def test_normalized_premium_none_premium():
    assert fs._compute_normalized_premium(None, 100.0) is None


# ---------------------------------------------------------------------------
# _compute_vol_oi_ratio
# ---------------------------------------------------------------------------

def test_vol_oi_ratio_basic():
    result = fs._compute_vol_oi_ratio(1000, 2000)
    assert result == 0.5


def test_vol_oi_ratio_none_volume():
    assert fs._compute_vol_oi_ratio(None, 1000) is None


def test_vol_oi_ratio_zero_oi():
    assert fs._compute_vol_oi_ratio(500, 0) is None


def test_vol_oi_ratio_none_oi():
    assert fs._compute_vol_oi_ratio(500, None) is None


# ---------------------------------------------------------------------------
# _is_configured (unconfigured env — env vars not set in test environment)
# ---------------------------------------------------------------------------

def test_is_configured_false_without_env_vars(monkeypatch):
    monkeypatch.setattr(fs, "_SUPABASE_URL", None)
    monkeypatch.setattr(fs, "_SUPABASE_KEY", None)
    assert fs._is_configured() is False


def test_is_configured_true_with_env_vars(monkeypatch):
    monkeypatch.setattr(fs, "_SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setattr(fs, "_SUPABASE_KEY", "fake-key")
    assert fs._is_configured() is True
