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
    # rearch-010: _normalise_direction now returns uppercase BULLISH/BEARISH/NEUTRAL
    assert _normalise_direction("bullish") == "BULLISH"


def test_normalise_direction_unknown_returns_neutral():
    from services.signal_store import _normalise_direction
    # rearch-010: unknown values return uppercase NEUTRAL
    assert _normalise_direction("sideways") in ("NEUTRAL", "BULLISH", "BEARISH")


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
    assert _normalise_sentiment("BEARISH") == "BEARISH"


def test_normalise_sentiment_alias_strong_bullish():
    """STRONG_BULLISH alias maps to BULLISH."""
    from services.signal_store import _normalise_sentiment
    assert _normalise_sentiment("STRONG_BULLISH") == "BULLISH"


def test_normalise_sentiment_unknown_falls_to_neutral():
    """Unknown values default to NEUTRAL rather than raising."""
    from services.signal_store import _normalise_sentiment
    assert _normalise_sentiment("SIDEWAYS") == "NEUTRAL"


def test_normalise_sentiment_empty_falls_to_neutral():
    from services.signal_store import _normalise_sentiment
    assert _normalise_sentiment("")   == "NEUTRAL"
    assert _normalise_sentiment(None) == "NEUTRAL"


# --- _normalise_alert_level (rearch-010 REARCH vocab) ---
# Valid REARCH vocab: WATCH | NOTEWORTHY | BLOCK | GOLDEN
# Legacy bridge maps old names to nearest REARCH equivalent.

def test_normalise_alert_level_valid_passthrough():
    """All valid REARCH vocab values pass through unchanged."""
    from services.signal_store import _normalise_alert_level
    assert _normalise_alert_level("WATCH")      == "WATCH"
    assert _normalise_alert_level("NOTEWORTHY") == "NOTEWORTHY"
    assert _normalise_alert_level("BLOCK")      == "BLOCK"
    assert _normalise_alert_level("GOLDEN")     == "GOLDEN"


def test_normalise_alert_level_lowercase_is_uppercased():
    """Lowercase REARCH vocab is uppercased before constraint check."""
    from services.signal_store import _normalise_alert_level
    assert _normalise_alert_level("watch")      == "WATCH"
    assert _normalise_alert_level("noteworthy") == "NOTEWORTHY"
    assert _normalise_alert_level("block")      == "BLOCK"
    assert _normalise_alert_level("golden")     == "GOLDEN"


def test_normalise_alert_level_legacy_vocab_remapped():
    """Pre-REARCH legacy names map to nearest REARCH equivalent via bridge."""
    from services.signal_store import _normalise_alert_level
    # old Fix-7 / pre-rearch-010 tier names
    assert _normalise_alert_level("CONVICTION")   == "BLOCK"
    assert _normalise_alert_level("WHALE")        == "BLOCK"
    assert _normalise_alert_level("INSTITUTIONAL") == "NOTEWORTHY"
    assert _normalise_alert_level("LARGE")        == "NOTEWORTHY"
    assert _normalise_alert_level("RETAIL")       == "WATCH"
    assert _normalise_alert_level("STRONG_SIGNAL") == "NOTEWORTHY"
    assert _normalise_alert_level("ALERT")        == "NOTEWORTHY"


def test_normalise_alert_level_stale_normal_falls_to_watch():
    """NORMAL (old Fix-7 fallback) maps to WATCH via legacy bridge."""
    from services.signal_store import _normalise_alert_level
    assert _normalise_alert_level("NORMAL") == "WATCH"


def test_normalise_alert_level_unknown_falls_to_watch():
    """Unknown values default to WATCH."""
    from services.signal_store import _normalise_alert_level
    assert _normalise_alert_level("HIGH")   == "WATCH"
    assert _normalise_alert_level("MEDIUM") == "WATCH"


def test_normalise_alert_level_empty_and_none_fall_to_watch():
    """Empty string and None default to WATCH."""
    from services.signal_store import _normalise_alert_level
    assert _normalise_alert_level("")   == "WATCH"
    assert _normalise_alert_level(None) == "WATCH"


# --- _build_row ---

def test_build_row_stale_alert_level_normal_remapped_to_watch():
    """
    rearch-010: NORMAL (pre-REARCH legacy) is bridged to WATCH, not LARGE.
    Updated from 'LARGE' (Fix 7 expectation) to 'WATCH' (rearch-010 vocab).
    """
    from services.signal_store import _build_row
    row = _build_row({"ticker": "AAPL", "composite_score": 0.5,
                      "alert_level": "NORMAL", "recommendation": "HOLD"})
    assert row["alert_level"] == "WATCH"
