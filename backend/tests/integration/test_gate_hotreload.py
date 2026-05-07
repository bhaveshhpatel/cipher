"""
tests/integration/test_gate_hotreload.py
=========================================
Chunk 6 — ING-010: Gate hot-reload end-to-end (no process restart)

Proves that a PATCH to gate_config_store.update() immediately propagates
the new value to store.get() callers in the same process — the canonical
definition of "hot reload without restart".

All DB I/O is intercepted with a custom httpx transport so the test is
hermetic and never touches a real Supabase instance.

Scenarios covered
-----------------
HR-01  load() populates get() with DB values (epoch advances)
HR-02  update() changes get() immediately — no restart required
HR-03  epoch is monotonically incremented on every update
HR-04  two successive updates both propagate (idempotent reload)
HR-05  update() for a different tier does NOT affect sibling tiers
HR-06  DB PATCH failure raises RuntimeError, get() retains old value
HR-07  audit-row failure (non-fatal) — gate value still updated in-memory
HR-08  concurrent updates are safe under threading.Lock (no torn reads)
HR-09  unknown gate raises ValueError, get() unaffected
HR-10  out-of-bounds value raises ValueError, get() unaffected
HR-11  unknown tier raises ValueError, get() unaffected
HR-12  market-hours guard raises ValueError when confirm=False
HR-13  no-db mode: update() propagates in-memory only (epoch still increments)
HR-14  load() → update() → load() cycle: second load re-reads DB, overwrites
HR-15  get() for unknown tier falls back to T3 value, never raises
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import patch, MagicMock

import httpx
import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fresh_store(
    supabase_url: str = "https://test.invalid",
    supabase_key: str = "test-svc-key",
) -> Any:
    """Return a clean GateConfigStore instance (not the module singleton)."""
    from services.gate_config_store import GateConfigStore
    store = GateConfigStore.__new__(GateConfigStore)
    GateConfigStore.__init__(store)
    store._supabase_url = supabase_url
    store._supabase_key = supabase_key
    return store


# ---------------------------------------------------------------------------
# Fake httpx transports
# ---------------------------------------------------------------------------

class _LoadTransport(httpx.BaseTransport):
    """
    Serves GET /rest/v1/gate_configs with a fixed set of rows,
    and accepts POST /rest/v1/gate_config_audit with 201.
    """

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if b"gate_configs" in request.url.raw_path and request.method == "GET":
            import json
            return httpx.Response(200, json=self._rows)
        return httpx.Response(404, json={"error": "not found"})


class _PatchAuditTransport(httpx.BaseTransport):
    """
    Accepts PATCH /rest/v1/gate_configs (204) and
    POST  /rest/v1/gate_config_audit (201).
    Records calls for assertion.
    """

    def __init__(
        self,
        patch_status: int = 204,
        audit_status: int = 201,
    ):
        self.patch_calls: list[httpx.Request] = []
        self.audit_calls: list[httpx.Request] = []
        self._patch_status = patch_status
        self._audit_status = audit_status

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "gate_configs" in path and request.method == "PATCH":
            self.patch_calls.append(request)
            return httpx.Response(self._patch_status, text="")
        if "gate_config_audit" in path and request.method == "POST":
            self.audit_calls.append(request)
            return httpx.Response(self._audit_status, text="")
        return httpx.Response(404)


# ---------------------------------------------------------------------------
# Market-hours patcher — freeze time to a known non-market UTC moment
# (Saturday 2026-01-03 00:00:00 UTC)
# ---------------------------------------------------------------------------

_OFF_HOURS_DT = "2026-01-03T00:00:00+00:00"  # Saturday — market closed


def _mock_now(iso: str = _OFF_HOURS_DT):
    """Patch datetime.datetime.now inside gate_config_store to a fixed time."""
    import datetime
    fixed = datetime.datetime.fromisoformat(iso)

    class _FakeDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    return patch("services.gate_config_store.datetime.datetime", _FakeDatetime)


# ---------------------------------------------------------------------------
# HR-01: load() populates get() with DB values
# ---------------------------------------------------------------------------

@pytest.mark.ci_gate
def test_hr01_load_populates_get():
    store = _fresh_store()
    rows = [
        {"gate_name": "min_premium",          "tier": 1, "value": "99000"},
        {"gate_name": "dte_floor_multiplier", "tier": 2, "value": "1.25"},
        {"gate_name": "debounce_ms",          "tier": 3, "value": "45000"},
    ]
    epoch_before = store.epoch
    transport = _LoadTransport(rows)
    with patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != 'transport'})):
        asyncio.get_event_loop().run_until_complete(store.load())

    assert store.get("min_premium",          1) == 99_000
    assert store.get("dte_floor_multiplier", 2) == pytest.approx(1.25)
    assert store.get("debounce_ms",          3) == 45_000
    assert store.epoch > epoch_before


# ---------------------------------------------------------------------------
# HR-02: update() propagates to get() immediately (core hotreload guarantee)
# ---------------------------------------------------------------------------

@pytest.mark.ci_gate
def test_hr02_update_propagates_immediately_no_restart():
    store = _fresh_store()
    original = store.get("min_premium", 1)

    transport = _PatchAuditTransport()
    with _mock_now(), \
         patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != 'transport'})):
        asyncio.get_event_loop().run_until_complete(
            store.update("min_premium", 1, 50_000, updated_by="ci")
        )

    # No restart — must see new value immediately
    assert store.get("min_premium", 1) == 50_000
    assert store.get("min_premium", 1) != original


# ---------------------------------------------------------------------------
# HR-03: epoch increments monotonically on every update
# ---------------------------------------------------------------------------

def test_hr03_epoch_increments_on_update():
    store = _fresh_store()
    e0 = store.epoch

    transport = _PatchAuditTransport()
    with _mock_now(), \
         patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != 'transport'})):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(store.update("min_premium", 1, 40_000))
        e1 = store.epoch
        loop.run_until_complete(store.update("min_premium", 1, 41_000))
        e2 = store.epoch

    assert e1 > e0
    assert e2 > e1


# ---------------------------------------------------------------------------
# HR-04: two successive updates both propagate
# ---------------------------------------------------------------------------

def test_hr04_successive_updates_both_propagate():
    store = _fresh_store()
    transport = _PatchAuditTransport()
    with _mock_now(), \
         patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != 'transport'})):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(store.update("debounce_ms", 2, 90_000))
        assert store.get("debounce_ms", 2) == 90_000
        loop.run_until_complete(store.update("debounce_ms", 2, 120_000))
        assert store.get("debounce_ms", 2) == 120_000


# ---------------------------------------------------------------------------
# HR-05: updating tier N does NOT affect sibling tiers
# ---------------------------------------------------------------------------

def test_hr05_update_does_not_bleed_across_tiers():
    store = _fresh_store()
    before_t2 = store.get("min_premium", 2)
    before_t3 = store.get("min_premium", 3)

    transport = _PatchAuditTransport()
    with _mock_now(), \
         patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != 'transport'})):
        asyncio.get_event_loop().run_until_complete(
            store.update("min_premium", 1, 80_000)
        )

    assert store.get("min_premium", 1) == 80_000
    assert store.get("min_premium", 2) == before_t2   # untouched
    assert store.get("min_premium", 3) == before_t3   # untouched


# ---------------------------------------------------------------------------
# HR-06: DB PATCH failure raises RuntimeError, get() retains old value
# ---------------------------------------------------------------------------

def test_hr06_db_patch_failure_raises_and_retains_old_value():
    store = _fresh_store()
    old = store.get("min_premium", 1)

    transport = _PatchAuditTransport(patch_status=500)
    with _mock_now(), \
         patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != 'transport'})):
        with pytest.raises(RuntimeError, match="DB PATCH failed"):
            asyncio.get_event_loop().run_until_complete(
                store.update("min_premium", 1, 50_000)
            )

    # In-memory value must be unchanged
    assert store.get("min_premium", 1) == old


# ---------------------------------------------------------------------------
# HR-07: audit-row failure is non-fatal — gate still updated in-memory
# ---------------------------------------------------------------------------

def test_hr07_audit_failure_nonfatal_gate_still_propagates():
    store = _fresh_store()
    transport = _PatchAuditTransport(patch_status=204, audit_status=500)
    with _mock_now(), \
         patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != 'transport'})):
        # Should not raise even though audit insert returns 500
        asyncio.get_event_loop().run_until_complete(
            store.update("min_premium", 1, 60_000)
        )

    assert store.get("min_premium", 1) == 60_000


# ---------------------------------------------------------------------------
# HR-08: concurrent updates are safe under threading.Lock
# ---------------------------------------------------------------------------

def test_hr08_concurrent_updates_no_torn_reads():
    store = _fresh_store()
    errors: list[Exception] = []
    results: list[int] = []

    def _update(value: int):
        try:
            transport = _PatchAuditTransport()
            with _mock_now(), \
                 patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != 'transport'})):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(store.update("dedup_window_ms", 1, value))
                loop.close()
            results.append(store.get("dedup_window_ms", 1))
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_update, args=(5_000 + i * 100,))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent update errors: {errors}"
    # Final in-memory value must be one of the valid values, not torn
    final = store.get("dedup_window_ms", 1)
    valid = {5_000 + i * 100 for i in range(8)}
    assert final in valid


# ---------------------------------------------------------------------------
# HR-09: unknown gate raises ValueError, get() unaffected
# ---------------------------------------------------------------------------

def test_hr09_unknown_gate_raises_get_unaffected():
    store = _fresh_store()
    old = store.get("min_premium", 1)
    with _mock_now():
        with pytest.raises(ValueError, match="Unknown gate"):
            asyncio.get_event_loop().run_until_complete(
                store.update("nonexistent_gate", 1, 999)
            )
    assert store.get("min_premium", 1) == old


# ---------------------------------------------------------------------------
# HR-10: out-of-bounds value raises ValueError, get() unaffected
# ---------------------------------------------------------------------------

def test_hr10_out_of_bounds_raises_get_unaffected():
    store = _fresh_store()
    old = store.get("min_premium", 1)
    with _mock_now():
        with pytest.raises(ValueError, match="outside allowed bounds"):
            # min_premium max is 500_000; 9_000_000 is out
            asyncio.get_event_loop().run_until_complete(
                store.update("min_premium", 1, 9_000_000)
            )
    assert store.get("min_premium", 1) == old


# ---------------------------------------------------------------------------
# HR-11: unknown tier raises ValueError, get() unaffected
# ---------------------------------------------------------------------------

def test_hr11_unknown_tier_raises():
    store = _fresh_store()
    with _mock_now():
        with pytest.raises(ValueError, match="Invalid tier"):
            asyncio.get_event_loop().run_until_complete(
                store.update("min_premium", 9, 50_000)
            )


# ---------------------------------------------------------------------------
# HR-12: market-hours guard fires when confirm=False
# ---------------------------------------------------------------------------

def test_hr12_market_hours_guard_fires():
    import datetime
    # Patch to a Tuesday 15:00 UTC (market open)
    market_open_dt = datetime.datetime(
        2026, 1, 6, 15, 0, 0,
        tzinfo=datetime.timezone.utc,
    )

    class _MarketOpenDt(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return market_open_dt if tz else market_open_dt.replace(tzinfo=None)

    store = _fresh_store()
    with patch("services.gate_config_store.datetime.datetime", _MarketOpenDt):
        with pytest.raises(ValueError, match="Market is currently open"):
            asyncio.get_event_loop().run_until_complete(
                store.update("min_premium", 1, 50_000, confirm_market_hours=False)
            )


# ---------------------------------------------------------------------------
# HR-13: no-db mode — update propagates in-memory only, epoch still increments
# ---------------------------------------------------------------------------

def test_hr13_no_db_mode_update_propagates():
    store = _fresh_store(supabase_url="", supabase_key="")
    e0 = store.epoch
    old = store.get("min_premium", 2)

    with _mock_now():
        result = asyncio.get_event_loop().run_until_complete(
            store.update("min_premium", 2, 20_000)
        )

    assert store.get("min_premium", 2) == 20_000
    assert store.get("min_premium", 2) != old
    assert store.epoch > e0
    assert result["_note"] == "no-db mode — in-memory only"


# ---------------------------------------------------------------------------
# HR-14: load() → update() → load() cycle: second load re-reads DB
# ---------------------------------------------------------------------------

def test_hr14_second_load_re_reads_db():
    store = _fresh_store()

    # First load: DB says min_premium T1 = 30_000
    load1_rows = [{"gate_name": "min_premium", "tier": 1, "value": "30000"}]
    t1 = _LoadTransport(load1_rows)
    with patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=t1, **{k: v for k, v in kw.items() if k != 'transport'})):
        asyncio.get_event_loop().run_until_complete(store.load())
    assert store.get("min_premium", 1) == 30_000

    # In-process update → 40_000
    t_patch = _PatchAuditTransport()
    with _mock_now(), \
         patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=t_patch, **{k: v for k, v in kw.items() if k != 'transport'})):
        asyncio.get_event_loop().run_until_complete(
            store.update("min_premium", 1, 40_000)
        )
    assert store.get("min_premium", 1) == 40_000

    # Second load: DB now says 55_000 — should overwrite the in-memory 40_000
    load2_rows = [{"gate_name": "min_premium", "tier": 1, "value": "55000"}]
    t2 = _LoadTransport(load2_rows)
    with patch("httpx.AsyncClient", lambda **kw: httpx.AsyncClient(transport=t2, **{k: v for k, v in kw.items() if k != 'transport'})):
        asyncio.get_event_loop().run_until_complete(store.load())
    assert store.get("min_premium", 1) == 55_000


# ---------------------------------------------------------------------------
# HR-15: get() for unknown tier falls back to T3, never raises
# ---------------------------------------------------------------------------

def test_hr15_unknown_tier_get_falls_back_to_t3():
    store = _fresh_store()
    t3_val = store.get("min_premium", 3)
    assert store.get("min_premium", 99) == t3_val   # unknown tier → T3
    assert store.get("min_premium", 0)  == t3_val   # tier 0 → T3
    assert store.get("min_premium", -1) == t3_val   # negative → T3
