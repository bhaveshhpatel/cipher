"""
Regression tests for services/signal_store.py
"""
import asyncio
from unittest.mock import patch, MagicMock
from dataclasses import dataclass


@dataclass
class _Flow:
    symbol: str
    contract_type: str = "CALL"
    direction: str = "bullish"
    influence_tier: str = "WHALE"
    trade_type: str = "sweep"
    premium: float = 50000.0
    composite_score: float = 0.80
    flow_score: float = 0.75
    backtest_score: float = 0.70
    volume_premium_factor: float = 1.2
    is_accelerating: bool = False
    reasoning: str = "test"


def setup_function():
    """Clear in-memory signal store before every test to prevent state bleed."""
    from services.signal_store import _clear_signal_memory
    _clear_signal_memory()


def test_normalise_direction_bullish():
    from services.signal_store import _normalise_direction
    assert _normalise_direction("bullish") == "bullish"


def test_normalise_direction_unknown_returns_neutral():
    from services.signal_store import _normalise_direction
    assert _normalise_direction("sideways") in ("neutral", "bullish", "bearish")


def test_normalise_influence_tier_whale():
    from services.signal_store import _normalise_influence_tier
    assert _normalise_influence_tier("WHALE") == "WHALE"


def test_normalise_influence_tier_unknown_returns_retail():
    from services.signal_store import _normalise_influence_tier
    assert _normalise_influence_tier("UNKNOWN") in ("RETAIL", "WHALE", "INSTITUTIONAL", "LARGE")


def test_normalise_trade_type_sweep():
    from services.signal_store import _normalise_trade_type
    assert _normalise_trade_type("sweep") == "sweep"


def test_save_signal_calls_supabase_insert():
    from services import signal_store
    sb = MagicMock()
    t  = MagicMock()
    t.insert.return_value  = t
    t.execute.return_value = MagicMock(data=[{"id": 1}])
    sb.table.return_value  = t
    with patch.object(signal_store, "_client", return_value=sb):
        asyncio.run(signal_store.save_signal(_Flow("AAPL")))
    assert t.insert.called


def test_save_signal_no_supabase_does_not_raise():
    from services import signal_store
    with patch.object(signal_store, "_client", return_value=None):
        asyncio.run(signal_store.save_signal(_Flow("TSLA")))


def test_save_signal_db_exception_does_not_raise():
    from services import signal_store
    sb = MagicMock()
    sb.table.side_effect = Exception("DB error")
    with patch.object(signal_store, "_client", return_value=sb):
        asyncio.run(signal_store.save_signal(_Flow("SPY")))


def test_get_recent_signals_returns_list():
    from services import signal_store
    sb = MagicMock()
    t  = MagicMock()
    t.select.return_value  = t
    t.order.return_value   = t
    t.limit.return_value   = t
    t.execute.return_value = MagicMock(data=[{"ticker": "AAPL"}])
    sb.table.return_value  = t
    with patch.object(signal_store, "_client", return_value=sb):
        result = asyncio.run(signal_store.get_recent_signals(limit=1))
    assert isinstance(result, list)


def test_get_recent_signals_no_supabase_returns_empty():
    from services import signal_store
    with patch.object(signal_store, "_client", return_value=None):
        assert asyncio.run(signal_store.get_recent_signals()) == []


# ---------------------------------------------------------------------------
# Fix 6 — PR #72: _normalise_sentiment, _normalise_alert_level, _build_row
# Validates that sentiment and alert_level are always constraint-safe before
# being written to signal_history (signal_feed_log_sentiment_check /
# signal_feed_log_alert_level_check).
# ---------------------------------------------------------------------------

# --- _normalise_sentiment ---

def test_normalise_sentiment_valid_uppercase_passthrough():
    """Valid uppercase values pass through unchanged."""
    from services.signal_store import _normalise_sentiment
    assert _normalise_sentiment("BULLISH")  == "BULLISH"
    assert _normalise_sentiment("BEARISH")  == "BEARISH"
    assert _normalise_sentiment("NEUTRAL")  == "NEUTRAL"


def test_normalise_sentiment_lowercase_is_uppercased():
    """Lowercase upstream values are correctly uppercased."""
    from services.signal_store import _normalise_sentiment
    assert _normalise_sentiment("bullish") == "BULLISH"
    assert _normalise_sentiment("bearish") == "BEARISH"
    assert _normalise_sentiment("neutral") == "NEUTRAL"


def test_normalise_sentiment_mixed_case_is_uppercased():
    """Mixed-case values are correctly resolved."""
    from services.signal_store import _normalise_sentiment
    assert _normalise_sentiment("Bullish") == "BULLISH"
    assert _normalise_sentiment("Bearish") == "BEARISH"


def test_normalise_sentiment_aliases_bullish():
    """Known bullish aliases resolve to BULLISH."""
    from services.signal_store import _normalise_sentiment
    assert _normalise_sentiment("BULL")           == "BULLISH"
    assert _normalise_sentiment("STRONG_BULLISH") == "BULLISH"
    assert _normalise_sentiment("BULLISH_STRONG") == "BULLISH"
    assert _normalise_sentiment("BUY")            == "BULLISH"


def test_normalise_sentiment_aliases_bearish():
    """Known bearish aliases resolve to BEARISH."""
    from services.signal_store import _normalise_sentiment
    assert _normalise_sentiment("BEAR")           == "BEARISH"
    assert _normalise_sentiment("STRONG_BEARISH") == "BEARISH"
    assert _normalise_sentiment("BEARISH_STRONG") == "BEARISH"
    assert _normalise_sentiment("SELL")           == "BEARISH"


def test_normalise_sentiment_unknown_falls_to_neutral():
    """Unknown values fall back to NEUTRAL — never 23514."""
    from services.signal_store import _normalise_sentiment
    assert _normalise_sentiment("CONFUSED")     == "NEUTRAL"
    assert _normalise_sentiment("STRONG_HOLD")  == "NEUTRAL"
    assert _normalise_sentiment("SIDEWAYS")     == "NEUTRAL"


def test_normalise_sentiment_empty_and_none_fall_to_neutral():
    """Falsy inputs fall back to NEUTRAL safely."""
    from services.signal_store import _normalise_sentiment
    assert _normalise_sentiment("")   == "NEUTRAL"
    assert _normalise_sentiment(None) == "NEUTRAL"


# --- _normalise_alert_level (Fix 7 vocab: CONVICTION|WHALE|INSTITUTIONAL|LARGE|RETAIL) ---

def test_normalise_alert_level_valid_passthrough():
    """Valid alert_level values (Fix 7 vocab) pass through unchanged."""
    from services.signal_store import _normalise_alert_level
    assert _normalise_alert_level("CONVICTION")   == "CONVICTION"
    assert _normalise_alert_level("WHALE")         == "WHALE"
    assert _normalise_alert_level("INSTITUTIONAL") == "INSTITUTIONAL"
    assert _normalise_alert_level("LARGE")         == "LARGE"
    assert _normalise_alert_level("RETAIL")        == "RETAIL"


def test_normalise_alert_level_lowercase_is_uppercased():
    """Lowercase Fix 7 alert_level values are correctly resolved."""
    from services.signal_store import _normalise_alert_level
    assert _normalise_alert_level("conviction")   == "CONVICTION"
    assert _normalise_alert_level("whale")         == "WHALE"
    assert _normalise_alert_level("institutional") == "INSTITUTIONAL"
    assert _normalise_alert_level("large")         == "LARGE"
    assert _normalise_alert_level("retail")        == "RETAIL"


def test_normalise_alert_level_legacy_vocab_remapped():
    """Old constraint vocab (pre-Fix 7) is remapped via the legacy bridge."""
    from services.signal_store import _normalise_alert_level
    # Legacy values map to nearest Fix 7 equivalent.
    assert _normalise_alert_level("STRONG_SIGNAL") == "WHALE"
    assert _normalise_alert_level("ALERT")         == "INSTITUTIONAL"
    assert _normalise_alert_level("WATCH")         == "LARGE"


def test_normalise_alert_level_stale_normal_falls_to_large():
    """'NORMAL' (pre-migration column default) falls back to LARGE via legacy bridge."""
    from services.signal_store import _normalise_alert_level
    assert _normalise_alert_level("NORMAL") == "LARGE"
    assert _normalise_alert_level("normal") == "LARGE"


def test_normalise_alert_level_unknown_falls_to_large():
    """Arbitrary unknown values fall back to LARGE (Fix 7 default)."""
    from services.signal_store import _normalise_alert_level
    assert _normalise_alert_level("HIGH")     == "LARGE"
    assert _normalise_alert_level("CRITICAL") == "LARGE"


def test_normalise_alert_level_empty_and_none_fall_to_large():
    """Falsy inputs fall back to LARGE safely (Fix 7 default)."""
    from services.signal_store import _normalise_alert_level
    assert _normalise_alert_level("")   == "LARGE"
    assert _normalise_alert_level(None) == "LARGE"


# --- _build_row integration: constraint-safe output ---

def test_build_row_lowercase_sentiment_produces_uppercase_db_value():
    """
    When upstream emits lowercase sentiment, _build_row must produce a
    value that satisfies signal_feed_log_sentiment_check.
    This was the direct cause of the 23514 violations.
    """
    from services.signal_store import _build_row, _VALID_SENTIMENTS
    sig = {
        "ticker": "AAPL",
        "composite_score": 0.80,
        "flow_score": 0.75,
        "backtest_score": 0.70,
        "sentiment": "bullish",   # lowercase — the broken path
    }
    row = _build_row(sig, ep={})
    assert row["sentiment"] in _VALID_SENTIMENTS, (
        f"row['sentiment']={row['sentiment']!r} is not in CHECK constraint values"
    )
    assert row["sentiment"] == "BULLISH"


def test_build_row_alias_sentiment_resolves_correctly():
    """STRONG_BULLISH alias in sig produces BULLISH in the DB row."""
    from services.signal_store import _build_row, _VALID_SENTIMENTS
    sig = {
        "ticker": "SPY",
        "composite_score": 0.90,
        "flow_score": 0.85,
        "backtest_score": 0.80,
        "sentiment": "STRONG_BULLISH",
    }
    row = _build_row(sig, ep={})
    assert row["sentiment"] in _VALID_SENTIMENTS
    assert row["sentiment"] == "BULLISH"


def test_build_row_stale_alert_level_normal_remapped_to_large():
    """
    If sig carries alert_level='NORMAL' (pre-migration column default),
    _build_row must remap to 'LARGE' (via legacy bridge in _normalise_alert_level)
    to satisfy signal_feed_log_alert_level_check (Fix 7 vocab).
    """
    from services.signal_store import _build_row, _VALID_ALERT_LEVELS
    sig = {
        "ticker": "TSLA",
        "composite_score": 0.90,   # would score CONVICTION via score path
        "flow_score": 0.85,
        "backtest_score": 0.80,
        "alert_level": "NORMAL",   # stale value overrides score path
    }
    row = _build_row(sig, ep={})
    assert row["alert_level"] in _VALID_ALERT_LEVELS, (
        f"row['alert_level']={row['alert_level']!r} is not in CHECK constraint values"
    )
    assert row["alert_level"] == "LARGE"


def test_build_row_all_constrained_fields_are_always_valid():
    """
    Boundary/smoke test: for a minimally populated sig dict,
    all 5 constrained fields in the DB row must be within their
    respective CHECK constraint value sets.
    """
    from services.signal_store import (
        _build_row,
        _VALID_SENTIMENTS,
        _VALID_ALERT_LEVELS,
        _VALID_DIRECTIONS,
        _VALID_TRADE_TYPES,
        _VALID_TIERS,
    )
    sig = {"ticker": "QQQ", "composite_score": 0.50}
    row = _build_row(sig, ep={})
    assert row["sentiment"]      in _VALID_SENTIMENTS,   f"sentiment={row['sentiment']!r}"
    assert row["alert_level"]    in _VALID_ALERT_LEVELS, f"alert_level={row['alert_level']!r}"
    assert row["direction"]      in _VALID_DIRECTIONS,   f"direction={row['direction']!r}"
    assert row["trade_type"]     in _VALID_TRADE_TYPES,  f"trade_type={row['trade_type']!r}"
    assert row["influence_tier"] in _VALID_TIERS,        f"influence_tier={row['influence_tier']!r}"
