"""
tests/test_ing010_admin_tier_thresholds.py
==========================================
Chunk 5 — ING-010: Admin tier-threshold control-plane endpoints

Covers:
  GET  /api/admin/tier-thresholds
    - 200 happy path (cache warm / cold)
    - 403 non-admin
    - 404 no active row
    - 500 missing SUPABASE_SERVICE_KEY

  PATCH /api/admin/tier-thresholds
    - 200 happy path + cache invalidation
    - 403 non-admin
    - 404 no active row
    - 422 unknown column(s)
    - 422 empty updates dict
    - 500 missing SUPABASE_SERVICE_KEY

  GET  /api/admin/tier-distribution
    - 200 happy path (tier counts + samples)
    - 403 non-admin
    - 404 no active snapshot
    - 500 missing SUPABASE_SERVICE_KEY
    - tier overflow (value not in {1,2,3}) falls back to tier 3

  Module-level: _ALLOWED_TIER_COLUMNS / _TIER_THRESHOLD_COLUMNS whitelist
"""
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# App + auth fixtures
# ---------------------------------------------------------------------------

ADMIN_TOKEN = {"sub": "admin@cipher.com", "email": "admin@cipher.com", "role": "admin"}
USER_TOKEN  = {"sub": "user@cipher.com",  "email": "user@cipher.com",  "role": "user"}


def _make_app():
    from fastapi import FastAPI
    from routers.admin import router
    app = FastAPI()
    app.include_router(router)
    return app


def _override_auth(token_data: dict):
    from core.auth import TokenData
    td = TokenData(**token_data)

    async def _dep():
        return td

    return _dep


@pytest.fixture
def admin_client():
    from core.auth import get_current_user
    app = _make_app()
    app.dependency_overrides[get_current_user] = _override_auth(ADMIN_TOKEN)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def user_client():
    from core.auth import get_current_user
    app = _make_app()
    app.dependency_overrides[get_current_user] = _override_auth(USER_TOKEN)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_ROW = {
    "id": 1,
    "is_active": True,
    "t1_min_volume": 100,
    "t1_min_last_price": 0.10,
    "t1_min_oi": 500,
    "t1_atm_pct": 0.05,
    "t1_max_dte": 45,
    "t2_min_volume": 50,
    "t2_min_last_price": 0.05,
    "t2_min_oi": 200,
    "t2_atm_pct": 0.08,
    "t2_max_dte": 60,
    "t3_min_volume": 10,
    "t3_min_last_price": 0.01,
    "t3_min_oi": 50,
    "t3_atm_pct": 0.12,
    "t3_max_dte": 90,
}


def _mock_sb_get(rows):
    """Return a mock supabase client whose table().select()...execute() yields rows."""
    mock_result = MagicMock()
    mock_result.data = rows
    chain = MagicMock()
    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.order.return_value = chain
    chain.limit.return_value = chain
    chain.execute.return_value = mock_result
    sb = MagicMock()
    sb.table.return_value = chain
    return sb


def _mock_sb_update(updated_rows):
    """Return a mock that also supports .update().eq().execute()."""
    mock_result = MagicMock()
    mock_result.data = updated_rows
    chain = MagicMock()
    chain.select.return_value = chain
    chain.update.return_value = chain
    chain.eq.return_value = chain
    chain.order.return_value = chain
    chain.limit.return_value = chain
    chain.execute.return_value = mock_result
    sb = MagicMock()
    sb.table.return_value = chain
    return sb


# ===========================================================================
# Whitelist introspection
# ===========================================================================

class TestAllowedColumns:
    def test_whitelist_exported(self):
        from routers.admin import _ALLOWED_TIER_COLUMNS, _TIER_THRESHOLD_COLUMNS
        assert _ALLOWED_TIER_COLUMNS is _TIER_THRESHOLD_COLUMNS

    def test_all_three_tiers_present(self):
        from routers.admin import _ALLOWED_TIER_COLUMNS
        for t in ("t1", "t2", "t3"):
            for col in ("min_volume", "min_last_price", "min_oi", "atm_pct", "max_dte"):
                assert f"{t}_{col}" in _ALLOWED_TIER_COLUMNS

    def test_whitelist_count(self):
        from routers.admin import _ALLOWED_TIER_COLUMNS
        assert len(_ALLOWED_TIER_COLUMNS) == 15


# ===========================================================================
# GET /api/admin/tier-thresholds
# ===========================================================================

class TestGetTierThresholds:

    def _get(self, client, sb_client=None, service_key="svc-key", te_patch=None):
        with patch("config.settings.SUPABASE_SERVICE_KEY", service_key), \
             patch("config.settings.SUPABASE_URL", "https://example.supabase.co"):
            if sb_client is not None:
                with patch("supabase.create_client", return_value=sb_client):
                    if te_patch:
                        with patch.multiple("services.tier_engine", **te_patch):
                            return client.get("/api/admin/tier-thresholds")
                    return client.get("/api/admin/tier-thresholds")
            return client.get("/api/admin/tier-thresholds")

    def test_403_non_admin(self, user_client):
        r = user_client.get("/api/admin/tier-thresholds")
        assert r.status_code == 403

    def test_500_no_service_key(self, admin_client):
        r = self._get(admin_client, service_key="")
        assert r.status_code == 500
        assert "SUPABASE_SERVICE_KEY" in r.json()["detail"]

    def test_404_no_active_row(self, admin_client):
        sb = _mock_sb_get([])
        r = self._get(admin_client, sb_client=sb)
        assert r.status_code == 404
        assert "No active tier_thresholds" in r.json()["detail"]

    def test_200_returns_row_and_cache(self, admin_client):
        import services.tier_engine as te
        sb = _mock_sb_get([_SAMPLE_ROW])
        te_patch = {
            "_cache_ts": time.monotonic() - 5,   # warm: age < TTL
            "CACHE_TTL": 60,
        }
        r = self._get(admin_client, sb_client=sb, te_patch=te_patch)
        assert r.status_code == 200
        body = r.json()
        assert body["row"]["id"] == 1
        assert "cache" in body
        assert body["cache"]["ttl_seconds"] == 60

    def test_200_cache_cold(self, admin_client):
        sb = _mock_sb_get([_SAMPLE_ROW])
        # _cache_ts = 0 → cache_age is None → warm = False
        te_patch = {"_cache_ts": 0.0, "CACHE_TTL": 60}
        r = self._get(admin_client, sb_client=sb, te_patch=te_patch)
        assert r.status_code == 200
        body = r.json()
        assert body["cache"]["warm"] is False
        assert body["cache"]["age_seconds"] is None

    def test_200_cache_warm_age_present(self, admin_client):
        sb = _mock_sb_get([_SAMPLE_ROW])
        te_patch = {"_cache_ts": time.monotonic() - 3, "CACHE_TTL": 60}
        r = self._get(admin_client, sb_client=sb, te_patch=te_patch)
        assert r.status_code == 200
        body = r.json()
        assert body["cache"]["warm"] is True
        assert body["cache"]["age_seconds"] is not None
        assert body["cache"]["age_seconds"] >= 0

    def test_200_cache_expired(self, admin_client):
        sb = _mock_sb_get([_SAMPLE_ROW])
        # age > TTL → warm = False but age_seconds is set
        te_patch = {"_cache_ts": time.monotonic() - 120, "CACHE_TTL": 60}
        r = self._get(admin_client, sb_client=sb, te_patch=te_patch)
        assert r.status_code == 200
        body = r.json()
        assert body["cache"]["warm"] is False
        assert body["cache"]["age_seconds"] is not None


# ===========================================================================
# PATCH /api/admin/tier-thresholds
# ===========================================================================

class TestPatchTierThresholds:

    def _patch(self, client, payload, sb_client=None, service_key="svc-key"):
        with patch("config.settings.SUPABASE_SERVICE_KEY", service_key), \
             patch("config.settings.SUPABASE_URL", "https://example.supabase.co"), \
             patch("services.tier_engine.invalidate_cache", return_value=None) as _ic, \
             patch("services.tier_engine.invalidate_thresholds_cache", return_value=None) as _itc:
            if sb_client is not None:
                with patch("supabase.create_client", return_value=sb_client):
                    r = client.patch("/api/admin/tier-thresholds", json=payload)
            else:
                r = client.patch("/api/admin/tier-thresholds", json=payload)
            return r, _ic, _itc

    def test_403_non_admin(self, user_client):
        r = user_client.patch(
            "/api/admin/tier-thresholds",
            json={"updates": {"t1_min_volume": 200}},
        )
        assert r.status_code == 403

    def test_422_unknown_columns(self, admin_client):
        r, _, _ = self._patch(
            admin_client,
            {"updates": {"t1_min_volume": 200, "evil_col": 999}},
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "evil_col" in detail

    def test_422_empty_updates(self, admin_client):
        r, _, _ = self._patch(admin_client, {"updates": {}})
        assert r.status_code == 422
        assert "No updates" in r.json()["detail"]

    def test_500_no_service_key(self, admin_client):
        r, _, _ = self._patch(
            admin_client,
            {"updates": {"t1_min_volume": 200}},
            service_key="",
        )
        assert r.status_code == 500

    def test_404_no_active_row(self, admin_client):
        sb = _mock_sb_update([])
        r, _, _ = self._patch(
            admin_client,
            {"updates": {"t1_min_volume": 200}},
            sb_client=sb,
        )
        assert r.status_code == 404
        assert "No active tier_thresholds" in r.json()["detail"]

    def test_200_happy_path(self, admin_client):
        updated_row = dict(_SAMPLE_ROW, t1_min_volume=200)
        sb = _mock_sb_update([updated_row])
        r, invalidate_cache, invalidate_thresholds_cache = self._patch(
            admin_client,
            {"updates": {"t1_min_volume": 200}},
            sb_client=sb,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["updated"]["t1_min_volume"] == 200
        assert body["row"]["t1_min_volume"] == 200
        assert "Cache invalidated" in body["note"]

    def test_200_calls_both_invalidate_aliases(self, admin_client):
        """Both invalidate_cache() and invalidate_thresholds_cache() must be called."""
        updated_row = dict(_SAMPLE_ROW, t1_min_volume=200)
        sb = _mock_sb_update([updated_row])
        r, ic, itc = self._patch(
            admin_client,
            {"updates": {"t1_min_volume": 200}},
            sb_client=sb,
        )
        assert r.status_code == 200
        ic.assert_called_once()
        itc.assert_called_once()

    def test_200_multi_column_update(self, admin_client):
        updated_row = dict(_SAMPLE_ROW, t1_min_volume=300, t2_min_oi=100)
        sb = _mock_sb_update([updated_row])
        r, _, _ = self._patch(
            admin_client,
            {"updates": {"t1_min_volume": 300, "t2_min_oi": 100}},
            sb_client=sb,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["updated"]["t1_min_volume"] == 300
        assert body["updated"]["t2_min_oi"] == 100

    def test_422_all_unknown_columns_listed(self, admin_client):
        r, _, _ = self._patch(
            admin_client,
            {"updates": {"bad_col_a": 1, "bad_col_b": 2}},
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "bad_col_a" in detail
        assert "bad_col_b" in detail


# ===========================================================================
# GET /api/admin/tier-distribution
# ===========================================================================

_SNAP_ROW = [{"id": 42}]

_SYMS_MIXED = [
    {"symbol": "AAPL",  "tier": 1, "open_interest": 5000},
    {"symbol": "MSFT",  "tier": 1, "open_interest": 4200},
    {"symbol": "NVDA",  "tier": 2, "open_interest": 1500},
    {"symbol": "AMD",   "tier": 3, "open_interest": 200},
    {"symbol": "INTC",  "tier": 3, "open_interest": 80},
    {"symbol": "TSLA",  "tier": 9, "open_interest": 10},   # overflow → tier 3
]


class TestGetTierDistribution:

    def _build_sb_dist(self, snap_rows, sym_rows):
        """
        Two-call supabase client: first call returns snapshot rows,
        second call returns symbol rows.
        """
        snap_result = MagicMock()
        snap_result.data = snap_rows

        sym_result = MagicMock()
        sym_result.data = sym_rows

        snap_chain = MagicMock()
        snap_chain.select.return_value = snap_chain
        snap_chain.eq.return_value = snap_chain
        snap_chain.order.return_value = snap_chain
        snap_chain.limit.return_value = snap_chain
        snap_chain.execute.return_value = snap_result

        sym_chain = MagicMock()
        sym_chain.select.return_value = sym_chain
        sym_chain.eq.return_value = sym_chain
        sym_chain.execute.return_value = sym_result

        sb = MagicMock()
        sb.table.side_effect = lambda name: snap_chain if "snapshot" in name else sym_chain
        return sb

    def _get(self, client, sb_client=None, service_key="svc-key"):
        with patch("config.settings.SUPABASE_SERVICE_KEY", service_key), \
             patch("config.settings.SUPABASE_URL", "https://example.supabase.co"):
            if sb_client is not None:
                with patch("supabase.create_client", return_value=sb_client):
                    return client.get("/api/admin/tier-distribution")
            return client.get("/api/admin/tier-distribution")

    def test_403_non_admin(self, user_client):
        r = user_client.get("/api/admin/tier-distribution")
        assert r.status_code == 403

    def test_500_no_service_key(self, admin_client):
        r = self._get(admin_client, service_key="")
        assert r.status_code == 500

    def test_404_no_active_snapshot(self, admin_client):
        sb = self._build_sb_dist([], [])
        r = self._get(admin_client, sb_client=sb)
        assert r.status_code == 404
        assert "No active snapshot" in r.json()["detail"]

    def test_200_happy_path_structure(self, admin_client):
        sb = self._build_sb_dist(_SNAP_ROW, _SYMS_MIXED)
        r = self._get(admin_client, sb_client=sb)
        assert r.status_code == 200
        body = r.json()
        assert body["snapshot_id"] == 42
        assert body["total"] == len(_SYMS_MIXED)
        assert "1" in body["tiers"]
        assert "2" in body["tiers"]
        assert "3" in body["tiers"]

    def test_200_tier_counts_correct(self, admin_client):
        sb = self._build_sb_dist(_SNAP_ROW, _SYMS_MIXED)
        r = self._get(admin_client, sb_client=sb)
        body = r.json()
        assert body["tiers"]["1"]["count"] == 2   # AAPL, MSFT
        assert body["tiers"]["2"]["count"] == 1   # NVDA
        # AMD(3) + INTC(3) + TSLA(overflow→3) = 3
        assert body["tiers"]["3"]["count"] == 3

    def test_200_tier_overflow_falls_back_to_tier3(self, admin_client):
        """Tier value 9 (unknown) must be bucketed into tier 3."""
        syms = [{"symbol": "XX", "tier": 9, "open_interest": 1}]
        sb = self._build_sb_dist(_SNAP_ROW, syms)
        r = self._get(admin_client, sb_client=sb)
        assert r.status_code == 200
        body = r.json()
        assert body["tiers"]["3"]["count"] == 1
        assert body["tiers"]["1"]["count"] == 0
        assert body["tiers"]["2"]["count"] == 0

    def test_200_samples_capped_at_10(self, admin_client):
        syms = [{"symbol": f"T{i}", "tier": 1, "open_interest": i * 10} for i in range(25)]
        sb = self._build_sb_dist(_SNAP_ROW, syms)
        r = self._get(admin_client, sb_client=sb)
        assert r.status_code == 200
        body = r.json()
        assert len(body["tiers"]["1"]["samples"]) == 10
        assert body["tiers"]["1"]["count"] == 25

    def test_200_empty_universe(self, admin_client):
        sb = self._build_sb_dist(_SNAP_ROW, [])
        r = self._get(admin_client, sb_client=sb)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        for t in ("1", "2", "3"):
            assert body["tiers"][t]["count"] == 0

    def test_200_tier_none_falls_back_to_tier3(self, admin_client):
        syms = [{"symbol": "ZZZ", "tier": None, "open_interest": 100}]
        sb = self._build_sb_dist(_SNAP_ROW, syms)
        r = self._get(admin_client, sb_client=sb)
        assert r.status_code == 200
        body = r.json()
        assert body["tiers"]["3"]["count"] == 1
