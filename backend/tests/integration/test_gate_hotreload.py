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

Worker-propagation SLA (≤5 s wall-clock)
-----------------------------------------
HR-16  single worker observes updated value within ≤5 s
HR-17  N=8 concurrent workers all observe updated value within ≤5 s
HR-18  epoch-watching worker detects change within ≤5 s
HR-19  stale worker that caches value locally re-reads store within ≤5 s
HR-20  rapid successive updates — worker always converges to final value within ≤5 s
HR-21  worker started before update() completes still propagates within ≤5 s
HR-22  failed update does NOT advance worker epoch within ≤5 s
"""
from __future__ import annotations

import asyncio
import threading
import time
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


# ===========================================================================
# Worker-propagation SLA (≤5 s wall-clock)
# ===========================================================================
#
# A "worker" in cipher is any coroutine or thread that holds a reference to
# gate_config_store and calls store.get(gate, tier) on each processing tick.
# Because the store is a shared in-process singleton backed by a threading.Lock,
# propagation is bounded only by the worker's poll interval — there is no
# network hop, no cache TTL, and no restart required.
#
# The FakeWorker below models this: it polls store.get() every POLL_MS and
# signals a threading.Event the first time it observes a target value.  The
# test then asserts the event fires within MAX_PROPAGATION_SECONDS.
#
# POLL_MS is set to 50 ms so the worst-case observation lag is 50 ms —
# well inside the 5 s SLA.  CI machines have no 5-second tolerance excuse.
# ===========================================================================

MAX_PROPAGATION_SECONDS: float = 5.0
_POLL_MS: int = 50  # worker poll cadence during tests


class FakeWorker:
    """
    Simulates an ingestion worker that continuously reads a single gate/tier
    from the store on a fixed poll cadence.

    Usage::

        worker = FakeWorker(store, gate="min_premium", tier=1, target=50_000)
        worker.start()
        # ... trigger update in main thread ...
        fired = worker.wait(timeout=MAX_PROPAGATION_SECONDS)
        worker.stop()
        assert fired
    """

    def __init__(
        self,
        store: Any,
        gate: str,
        tier: int,
        target: Any,
        poll_ms: int = _POLL_MS,
    ) -> None:
        self._store = store
        self._gate = gate
        self._tier = tier
        self._target = target
        self._poll_s = poll_ms / 1_000.0
        self._seen_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.observed_value: Any = None
        self.observed_at: float | None = None  # time.monotonic() when target seen

    def _run(self) -> None:
        while not self._stop_event.is_set():
            val = self._store.get(self._gate, self._tier)
            if val == self._target:
                self.observed_value = val
                self.observed_at = time.monotonic()
                self._seen_event.set()
                return
            time.sleep(self._poll_s)

    def start(self) -> "FakeWorker":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def wait(self, timeout: float = MAX_PROPAGATION_SECONDS) -> bool:
        """Return True if the target value was observed within *timeout* seconds."""
        return self._seen_event.wait(timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)


class EpochWorker:
    """
    Variant that watches store.epoch instead of a specific gate value.
    Fires when epoch advances past a baseline, confirming any update propagated.
    """

    def __init__(
        self,
        store: Any,
        baseline_epoch: int,
        poll_ms: int = _POLL_MS,
    ) -> None:
        self._store = store
        self._baseline = baseline_epoch
        self._poll_s = poll_ms / 1_000.0
        self._seen_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.observed_epoch: int | None = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            epoch = self._store.epoch
            if epoch > self._baseline:
                self.observed_epoch = epoch
                self._seen_event.set()
                return
            time.sleep(self._poll_s)

    def start(self) -> "EpochWorker":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def wait(self, timeout: float = MAX_PROPAGATION_SECONDS) -> bool:
        return self._seen_event.wait(timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)


def _do_update(store: Any, gate: str, tier: int, value: Any) -> None:
    """Run store.update() in a fresh event loop (called from threads)."""
    with _mock_now():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(store.update(gate, tier, value))
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# HR-16: single worker observes updated value within ≤5 s
# ---------------------------------------------------------------------------

@pytest.mark.ci_gate
def test_hr16_single_worker_propagation_within_5s():
    store = _fresh_store(supabase_url="", supabase_key="")
    target = 77_000

    worker = FakeWorker(store, gate="min_premium", tier=1, target=target)
    worker.start()

    t0 = time.monotonic()
    _do_update(store, "min_premium", 1, target)
    fired = worker.wait(timeout=MAX_PROPAGATION_SECONDS)
    elapsed = time.monotonic() - t0
    worker.stop()

    assert fired, (
        f"Worker did not observe min_premium[T1]={target} within "
        f"{MAX_PROPAGATION_SECONDS}s (elapsed={elapsed:.3f}s)"
    )
    assert elapsed < MAX_PROPAGATION_SECONDS
    assert store.get("min_premium", 1) == target


# ---------------------------------------------------------------------------
# HR-17: N=8 concurrent workers all observe updated value within ≤5 s
# ---------------------------------------------------------------------------

@pytest.mark.ci_gate
def test_hr17_eight_concurrent_workers_all_propagate_within_5s():
    store = _fresh_store(supabase_url="", supabase_key="")
    target = 66_000
    n_workers = 8

    workers = [
        FakeWorker(store, gate="min_premium", tier=1, target=target).start()
        for _ in range(n_workers)
    ]

    t0 = time.monotonic()
    _do_update(store, "min_premium", 1, target)

    results = [w.wait(timeout=MAX_PROPAGATION_SECONDS) for w in workers]
    elapsed = time.monotonic() - t0

    for w in workers:
        w.stop()

    assert all(results), (
        f"{results.count(False)}/{n_workers} workers did not propagate within "
        f"{MAX_PROPAGATION_SECONDS}s (elapsed={elapsed:.3f}s)"
    )
    assert elapsed < MAX_PROPAGATION_SECONDS


# ---------------------------------------------------------------------------
# HR-18: epoch-watching worker detects change within ≤5 s
# ---------------------------------------------------------------------------

def test_hr18_epoch_watcher_detects_update_within_5s():
    store = _fresh_store(supabase_url="", supabase_key="")
    baseline_epoch = store.epoch

    watcher = EpochWorker(store, baseline_epoch=baseline_epoch)
    watcher.start()

    t0 = time.monotonic()
    _do_update(store, "debounce_ms", 2, 60_000)
    fired = watcher.wait(timeout=MAX_PROPAGATION_SECONDS)
    elapsed = time.monotonic() - t0
    watcher.stop()

    assert fired, (
        f"Epoch watcher did not advance beyond {baseline_epoch} within "
        f"{MAX_PROPAGATION_SECONDS}s (elapsed={elapsed:.3f}s, "
        f"current epoch={store.epoch})"
    )
    assert watcher.observed_epoch is not None
    assert watcher.observed_epoch > baseline_epoch
    assert elapsed < MAX_PROPAGATION_SECONDS


# ---------------------------------------------------------------------------
# HR-19: worker that locally cached the old value re-reads store within ≤5 s
# ---------------------------------------------------------------------------

def test_hr19_stale_local_cache_worker_repropagates_within_5s():
    """
    Simulates a worker that snapshots gate value at startup (like a poorly
    written consumer) but refreshes from the store each tick.
    The refresh path is what matters — the worker must NOT hold a stale copy
    forever; it must re-call store.get() on each tick.
    """
    store = _fresh_store(supabase_url="", supabase_key="")
    target = 55_000

    # Worker snapshots stale value first, then re-polls
    stale_snapshot = store.get("min_premium", 2)  # pre-update value
    assert stale_snapshot != target, "precondition: target must differ from fallback"

    worker = FakeWorker(store, gate="min_premium", tier=2, target=target)
    worker.start()

    t0 = time.monotonic()
    _do_update(store, "min_premium", 2, target)
    fired = worker.wait(timeout=MAX_PROPAGATION_SECONDS)
    elapsed = time.monotonic() - t0
    worker.stop()

    assert fired, (
        f"Stale-cache worker did not refresh min_premium[T2]={target} within "
        f"{MAX_PROPAGATION_SECONDS}s — worker is holding a stale local copy "
        f"(elapsed={elapsed:.3f}s)"
    )
    assert elapsed < MAX_PROPAGATION_SECONDS


# ---------------------------------------------------------------------------
# HR-20: rapid successive updates — worker converges to final value within ≤5 s
# ---------------------------------------------------------------------------

def test_hr20_rapid_successive_updates_worker_converges_to_final_within_5s():
    """
    Fire 5 rapid updates in quick succession.  The worker only needs to
    observe the *final* value within ≤5 s — it may miss intermediates.
    """
    store = _fresh_store(supabase_url="", supabase_key="")
    intermediate_values = [30_000, 35_000, 40_000, 45_000]
    final_value = 50_000

    worker = FakeWorker(store, gate="min_premium", tier=3, target=final_value)
    worker.start()

    t0 = time.monotonic()
    for v in intermediate_values:
        _do_update(store, "min_premium", 3, v)
    _do_update(store, "min_premium", 3, final_value)

    fired = worker.wait(timeout=MAX_PROPAGATION_SECONDS)
    elapsed = time.monotonic() - t0
    worker.stop()

    assert fired, (
        f"Worker did not converge to final value {final_value} within "
        f"{MAX_PROPAGATION_SECONDS}s after rapid updates (elapsed={elapsed:.3f}s)"
    )
    assert store.get("min_premium", 3) == final_value
    assert elapsed < MAX_PROPAGATION_SECONDS


# ---------------------------------------------------------------------------
# HR-21: worker started before update() completes still propagates within ≤5 s
# ---------------------------------------------------------------------------

def test_hr21_worker_started_before_update_propagates_within_5s():
    """
    Worker is polling *before* update() is called.  The update fires in a
    background thread after a short delay.  Propagation must still be ≤5 s
    from the moment the update is dispatched.
    """
    store = _fresh_store(supabase_url="", supabase_key="")
    target = 88_000
    delay_s = 0.2  # update fires 200 ms after worker is already running

    worker = FakeWorker(store, gate="min_premium", tier=1, target=target)
    worker.start()

    def _delayed_update():
        time.sleep(delay_s)
        _do_update(store, "min_premium", 1, target)

    t0 = time.monotonic()
    updater = threading.Thread(target=_delayed_update, daemon=True)
    updater.start()

    fired = worker.wait(timeout=MAX_PROPAGATION_SECONDS)
    elapsed = time.monotonic() - t0
    worker.stop()
    updater.join(timeout=1.0)

    assert fired, (
        f"Worker (pre-started) did not observe min_premium[T1]={target} within "
        f"{MAX_PROPAGATION_SECONDS}s of update dispatch (elapsed={elapsed:.3f}s)"
    )
    # elapsed from t0 includes the 200 ms delay — still well under 5 s
    assert elapsed < MAX_PROPAGATION_SECONDS
    assert worker.observed_at is not None
    # Propagation lag after the update itself should be tiny (< 1 s)
    assert (worker.observed_at - t0) < (delay_s + 1.0)


# ---------------------------------------------------------------------------
# HR-22: failed update does NOT advance worker's observed epoch within ≤5 s
# ---------------------------------------------------------------------------

def test_hr22_failed_update_does_not_advance_worker_epoch_within_5s():
    """
    When update() raises (bounds error), epoch must not increment.
    An EpochWorker with a 1-second wait must time out — the epoch stays flat.
    """
    store = _fresh_store(supabase_url="", supabase_key="")
    baseline_epoch = store.epoch

    watcher = EpochWorker(store, baseline_epoch=baseline_epoch, poll_ms=_POLL_MS)
    watcher.start()

    # Attempt an out-of-bounds update — must raise, epoch must not change
    try:
        with _mock_now():
            asyncio.get_event_loop().run_until_complete(
                store.update("min_premium", 1, 9_999_999)  # above max bound
            )
    except ValueError:
        pass  # expected

    # Give the watcher 1 s — if epoch had moved, it would fire immediately
    fired = watcher.wait(timeout=1.0)
    watcher.stop()

    assert not fired, (
        "EpochWorker fired after a failed (bounds-error) update — "
        f"epoch should have stayed at {baseline_epoch} but watcher saw "
        f"epoch={watcher.observed_epoch}"
    )
    assert store.epoch == baseline_epoch, (
        f"Epoch advanced from {baseline_epoch} to {store.epoch} after a failed update"
    )
