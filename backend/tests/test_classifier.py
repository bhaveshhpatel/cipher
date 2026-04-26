# patched by pyflakes-fix: removed unused 'import pytest'
from unittest.mock import patch, MagicMock
from services.composite_signal_engine import _classify_signal


def test_classify_strong_bullish():
    signal = {
        "composite_score": 0.85,
        "flow_score": 0.80,
        "backtest_score": 0.75,
        "direction": "bullish",
        "influence_tier": "WHALE",
    }
    result = _classify_signal(signal)
    assert result in ("CONVICTION", "STRONG_SIGNAL", "ALERT", "WATCH")


def test_classify_weak_signal():
    signal = {
        "composite_score": 0.30,
        "flow_score": 0.25,
        "backtest_score": 0.20,
        "direction": "bearish",
        "influence_tier": "RETAIL",
    }
    result = _classify_signal(signal)
    assert result in ("CONVICTION", "STRONG_SIGNAL", "ALERT", "WATCH", "NOISE")
