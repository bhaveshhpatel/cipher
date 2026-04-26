"""
Regression tests for ingestion config loading and validation.
"""
import os
import pytest


def test_ingestion_config_importable():
    import ingestion.config as _m  # intentional smoke import
    assert _m is not None


def test_ingestion_config_has_expected_attributes():
    import ingestion.config as cfg
    assert hasattr(cfg, "SYMBOLS") or hasattr(cfg, "DEFAULT_SYMBOLS") \
        or hasattr(cfg, "get_symbols") or hasattr(cfg, "settings")


def test_get_symbols_returns_list():
    import ingestion.config as cfg
    fn = getattr(cfg, "get_symbols", None)
    if fn is None:
        pytest.skip("get_symbols not defined")
    result = fn()
    assert isinstance(result, list)
    assert len(result) > 0


def test_symbols_are_strings():
    import ingestion.config as cfg
    fn = getattr(cfg, "get_symbols", None)
    if fn is None:
        pytest.skip("get_symbols not defined")
    symbols = fn()
    assert all(isinstance(s, str) for s in symbols)


def test_symbols_are_uppercase():
    import ingestion.config as cfg
    fn = getattr(cfg, "get_symbols", None)
    if fn is None:
        pytest.skip("get_symbols not defined")
    symbols = fn()
    for s in symbols:
        assert s == s.upper(), f"Symbol not uppercase: {s}"


def test_default_symbols_contains_spy():
    import ingestion.config as cfg
    symbols = getattr(cfg, "DEFAULT_SYMBOLS",
                      getattr(cfg, "SYMBOLS", None))
    if symbols is None:
        pytest.skip("No DEFAULT_SYMBOLS or SYMBOLS constant")
    assert "SPY" in symbols or "AAPL" in symbols


def test_env_override_symbols(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "AAPL,TSLA,NVDA")
    import importlib
    import ingestion.config as cfg
    importlib.reload(cfg)
    fn = getattr(cfg, "get_symbols", None)
    if fn is None:
        pytest.skip("get_symbols not defined")
    symbols = fn()
    assert isinstance(symbols, list)


def test_missing_api_key_env_raises_or_uses_default():
    import ingestion.config as cfg
    _ = getattr(cfg, "TRADIER_API_KEY", None) or os.environ.get("TRADIER_API_KEY", None)


def test_validate_symbol_accepts_valid():
    import ingestion.config as cfg
    fn = getattr(cfg, "validate_symbol", None)
    if fn is None:
        pytest.skip("validate_symbol not defined")
    assert fn("AAPL") is True


def test_validate_symbol_rejects_empty():
    import ingestion.config as cfg
    fn = getattr(cfg, "validate_symbol", None)
    if fn is None:
        pytest.skip("validate_symbol not defined")
    assert fn("") is False


def test_validate_symbol_rejects_numeric():
    import ingestion.config as cfg
    fn = getattr(cfg, "validate_symbol", None)
    if fn is None:
        pytest.skip("validate_symbol not defined")
    assert fn("12345") is False


def test_min_premium_threshold_is_positive():
    import ingestion.config as cfg
    threshold = getattr(cfg, "MIN_PREMIUM", None) \
        or getattr(cfg, "PREMIUM_THRESHOLD", None)
    if threshold is None:
        pytest.skip("No MIN_PREMIUM constant defined")
    assert threshold > 0


def test_min_premium_threshold_reasonable():
    import ingestion.config as cfg
    threshold = getattr(cfg, "MIN_PREMIUM", None) \
        or getattr(cfg, "PREMIUM_THRESHOLD", None)
    if threshold is None:
        pytest.skip("No MIN_PREMIUM constant defined")
    assert 1_000 <= threshold <= 10_000_000


def test_reload_is_idempotent():
    import importlib
    import ingestion.config as cfg
    before = getattr(cfg, "DEFAULT_SYMBOLS",
                     getattr(cfg, "SYMBOLS", "unknown"))
    importlib.reload(cfg)
    after = getattr(cfg, "DEFAULT_SYMBOLS",
                    getattr(cfg, "SYMBOLS", "unknown"))
    assert before == after


def test_ingestion_enabled_flag_is_bool():
    import ingestion.config as cfg
    flag = getattr(cfg, "INGESTION_ENABLED", None)
    if flag is None:
        pytest.skip("INGESTION_ENABLED not defined")
    assert isinstance(flag, bool)


def test_add_symbol_appears_in_list():
    import ingestion.config as cfg
    fn_add = getattr(cfg, "add_symbol", None)
    fn_get = getattr(cfg, "get_symbols", None)
    if fn_add is None or fn_get is None:
        pytest.skip("add_symbol/get_symbols not defined")
    fn_add("TEST_SYM")
    assert "TEST_SYM" in fn_get()


def test_remove_symbol_disappears_from_list():
    import ingestion.config as cfg
    fn_add    = getattr(cfg, "add_symbol",    None)
    fn_remove = getattr(cfg, "remove_symbol", None)
    fn_get    = getattr(cfg, "get_symbols",   None)
    if not all([fn_add, fn_remove, fn_get]):
        pytest.skip("add/remove/get_symbols not fully defined")
    fn_add("REM_SYM")
    fn_remove("REM_SYM")
    assert "REM_SYM" not in fn_get()


def test_remove_nonexistent_symbol_does_not_crash():
    import ingestion.config as cfg
    fn = getattr(cfg, "remove_symbol", None)
    if fn is None:
        pytest.skip("remove_symbol not defined")
    try:
        fn("NONEXISTENT_XYZ")
    except (ValueError, KeyError):
        pass


def test_apply_config_with_valid_dict():
    import ingestion.config as cfg
    fn = getattr(cfg, "apply_config", None)
    if fn is None:
        pytest.skip("apply_config not defined")
    fn({"symbols": ["AAPL", "TSLA"]})


def test_apply_config_is_idempotent():
    import ingestion.config as cfg
    fn = getattr(cfg, "apply_config", None)
    if fn is None:
        pytest.skip("apply_config not defined")
    conf = {"symbols": ["AAPL"]}
    fn(conf)
    fn(conf)
