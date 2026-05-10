"""test_rearch_001_index_purge.py

Test suite for REARCH-001: Index Symbol Purge.

Covers:
  A. is_index_symbol() — blacklist hits, dollar-prefix hits, equity pass-throughs,
     edge cases (empty, whitespace, case variants, leveraged ETF false-positive guard)
  B. validate_symbol() — index symbols are rejected even though they pass structural
     checks; equity symbols pass; combined gate ordering is correct
  C. Defence-in-depth contract — the two gates (filters.py + config.py) are
     independent: patching one does not affect the other
  D. Migration SQL shape — both SQL files are syntactically present and contain
     the required constraint names and blocked ticker list

All tests are pure unit tests. No DB, no network, no mocking required.
"""
from __future__ import annotations

import pathlib
import sys
import types
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module-level import helpers
# ---------------------------------------------------------------------------
# ingestion/ lives under backend/ which may not be on sys.path in all CI
# environments. Add backend/ to path so `from ingestion.filters import ...`
# resolves correctly regardless of how pytest is invoked.
_BACKEND = pathlib.Path(__file__).resolve().parents[1]  # …/backend
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from ingestion.filters import is_index_symbol  # noqa: E402
from ingestion.config import validate_symbol   # noqa: E402


# ===========================================================================
# A. is_index_symbol()
# ===========================================================================

class TestIsIndexSymbol:
    # --- Blacklist exact matches ---
    @pytest.mark.parametrize("sym", [
        "SPX", "SPXW", "SPXPM", "NDX", "NDXP",
        "VIX", "VIXW", "RUT", "MRUT", "DJX", "XSP",
    ])
    def test_blacklisted_tickers_are_index(self, sym):
        assert is_index_symbol(sym) is True

    # --- Case insensitivity ---
    @pytest.mark.parametrize("sym", ["spx", "Spx", "vix", "Ndx", "rut"])
    def test_blacklist_is_case_insensitive(self, sym):
        assert is_index_symbol(sym) is True

    # --- Whitespace tolerance ---
    @pytest.mark.parametrize("sym", [" SPX", "VIX ", "  NDX  "])
    def test_blacklist_strips_whitespace(self, sym):
        assert is_index_symbol(sym) is True

    # --- Dollar-prefix ---
    @pytest.mark.parametrize("sym", ["$SPX", "$NDX", "$NDX.X", "$VIX", "$"])
    def test_dollar_prefix_is_index(self, sym):
        assert is_index_symbol(sym) is True

    # --- Equity pass-throughs (must NOT be rejected) ---
    @pytest.mark.parametrize("sym", [
        "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA",
        "GOOGL", "AMD", "META", "IWM", "GLD",
    ])
    def test_equities_are_not_index(self, sym):
        assert is_index_symbol(sym) is False

    # --- Leveraged ETF false-positive guard ---
    # SPXL, SPXS, SOXL share a prefix with SPX but are equity ETFs.
    # The filter must NOT reject them (only '$' prefix triggers, not 'SPX' prefix).
    @pytest.mark.parametrize("sym", ["SPXL", "SPXS", "SOXL", "VIXY", "UVXY"])
    def test_leveraged_etfs_are_not_index(self, sym):
        assert is_index_symbol(sym) is False

    # --- Edge cases ---
    def test_empty_string_is_not_index(self):
        assert is_index_symbol("") is False

    def test_whitespace_only_is_not_index(self):
        assert is_index_symbol("   ") is False

    def test_numeric_string_is_not_index(self):
        assert is_index_symbol("12345") is False


# ===========================================================================
# B. validate_symbol() — index rejection at API layer
# ===========================================================================

class TestValidateSymbolIndexRejection:
    # --- Index symbols must be rejected despite passing structural checks ---
    @pytest.mark.parametrize("sym", [
        "SPX", "VIX", "NDX", "RUT", "DJX", "XSP",
        "SPXW", "NDXP", "VIXW", "MRUT", "SPXPM",
    ])
    def test_index_symbols_rejected_by_validate(self, sym):
        """Index tickers pass alpha/length checks but validate_symbol must return False."""
        # Sanity: confirm they would pass structural checks alone
        assert sym.isalpha()
        assert 1 <= len(sym) <= 5  # all blocked tickers are 3-5 chars
        # Now confirm the full gate rejects them
        assert validate_symbol(sym) is False

    # --- Equity symbols must still pass ---
    @pytest.mark.parametrize("sym", [
        "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA",
        "AMZN", "META", "GOOGL", "AMD", "IWM",
    ])
    def test_equity_symbols_pass_validate(self, sym):
        assert validate_symbol(sym) is True

    # --- Structural rejections still work ---
    def test_empty_string_rejected(self):
        assert validate_symbol("") is False

    def test_none_rejected(self):
        assert validate_symbol(None) is False  # type: ignore[arg-type]

    def test_numeric_rejected(self):
        assert validate_symbol("12345") is False

    def test_too_long_rejected(self):
        assert validate_symbol("TOOLONG") is False

    def test_dollar_prefix_rejected(self):
        # '$SPX' fails isalpha() before reaching is_index_symbol, still False
        assert validate_symbol("$SPX") is False

    # --- Case handling ---
    def test_lowercase_index_rejected(self):
        assert validate_symbol("spx") is False

    def test_lowercase_equity_passes(self):
        # validate_symbol does NOT uppercase before is_index_symbol;
        # is_index_symbol uppercases internally, so lowercase equities pass
        assert validate_symbol("spy") is True


# ===========================================================================
# C. Defence-in-depth — gates are independent
# ===========================================================================

class TestDefenceInDepthIndependence:
    """Verify filters.py and config.py are independent enforcement points.

    Patching is_index_symbol to always return False must not affect the
    structural checks in validate_symbol, and vice versa.
    """

    def test_structural_checks_fire_before_index_check(self):
        """If is_index_symbol is stubbed out, empty/numeric/too-long still fail."""
        with patch("ingestion.config.is_index_symbol", return_value=False):
            assert validate_symbol("") is False
            assert validate_symbol("12345") is False
            assert validate_symbol("TOOLONG") is False

    def test_index_check_fires_after_structural_checks(self):
        """If is_index_symbol is stubbed to True, valid equity tickers are rejected."""
        with patch("ingestion.config.is_index_symbol", return_value=True):
            assert validate_symbol("SPY") is False
            assert validate_symbol("AAPL") is False

    def test_filters_module_has_no_import_from_config(self):
        """filters.py must not import from ingestion.config — one-way dependency only."""
        import ingestion.filters as filters_mod
        # Walk the module's globals looking for anything that came from ingestion.config
        for name, obj in vars(filters_mod).items():
            if isinstance(obj, types.ModuleType):
                assert obj.__name__ != "ingestion.config", (
                    f"filters.py imported ingestion.config as '{name}' — "
                    "dependency must be one-way (config → filters, not filters → config)"
                )


# ===========================================================================
# D. Migration SQL shape
# ===========================================================================

class TestMigrationSqlShape:
    """Verify the migration files exist and contain required elements.

    These are smoke tests only — they do not execute SQL against a DB.
    They catch the most common mistake: a migration that omits a constraint
    name or a ticker, discovered only at apply-time.

    NOTE (REARCH-001 patch 2026-05-09): tracked_symbols does not exist in the
    cipher schema. Both migration files operate on options_universe_symbols only.
    Assertions referencing chk_tracked_symbols_no_index and
    NOTIFY tracked_symbols_changed have been removed accordingly.
    """

    _MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
    _CONSTRAINT_FILE = _MIGRATIONS / "rearch_001_add_index_blacklist_constraint.sql"
    _DELETE_FILE = _MIGRATIONS / "rearch_001_delete_index_tickers_from_tracked_symbols.sql"

    _BLOCKED = ["SPX", "SPXW", "SPXPM", "NDX", "NDXP", "VIX", "VIXW", "RUT", "MRUT", "DJX", "XSP"]

    def test_constraint_migration_file_exists(self):
        assert self._CONSTRAINT_FILE.exists(), (
            f"Missing migration file: {self._CONSTRAINT_FILE.name}"
        )

    def test_delete_migration_file_exists(self):
        assert self._DELETE_FILE.exists(), (
            f"Missing migration file: {self._DELETE_FILE.name}"
        )

    def test_constraint_file_has_universe_symbols_constraint_name(self):
        sql = self._CONSTRAINT_FILE.read_text()
        assert "chk_options_universe_symbols_no_index" in sql

    def test_constraint_file_contains_all_blocked_tickers(self):
        sql = self._CONSTRAINT_FILE.read_text()
        missing = [t for t in self._BLOCKED if t not in sql]
        assert not missing, f"Constraint migration missing blocked tickers: {missing}"

    def test_constraint_file_has_dollar_prefix_guard(self):
        sql = self._CONSTRAINT_FILE.read_text()
        assert "$%" in sql or "LIKE '\\$%'" in sql or "LIKE '$%'" in sql

    def test_delete_file_contains_all_blocked_tickers(self):
        sql = self._DELETE_FILE.read_text()
        missing = [t for t in self._BLOCKED if t not in sql]
        assert not missing, f"Delete migration missing blocked tickers: {missing}"

    def test_delete_file_has_dollar_prefix_guard(self):
        sql = self._DELETE_FILE.read_text()
        assert "$%" in sql

    def test_delete_file_has_notify_for_universe_symbols(self):
        sql = self._DELETE_FILE.read_text()
        assert "NOTIFY" in sql
        assert "options_universe_symbols_changed" in sql

    def test_both_files_wrapped_in_transaction(self):
        for f in (self._CONSTRAINT_FILE, self._DELETE_FILE):
            sql = f.read_text()
            assert "BEGIN;" in sql, f"{f.name} missing BEGIN;"
            assert "COMMIT;" in sql, f"{f.name} missing COMMIT;"
