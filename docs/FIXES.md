# Cipher — Bug Fix Log

Chronological record of all bugs found and fixed. Each entry includes root cause, symptom, and the exact change made.

---

## ING-003 — Cold-Start DTE Floor Bypass at Accumulator Instantiation

**Date:** 2026-05-03
**Severity:** P0 — data quality; low-quality short-DTE lottery tickets passed Gate 6 during cold-start window (~30 min)
**PR:** [#59](https://github.com/bhaveshhpatel/cipher/pull/59) — squash merged commit `62b159f`
**Files:** `backend/services/tradier_stream.py`, `backend/tests/test_ing003_dte_floors.py`

### Root Cause

`RepetitionAccumulator` was instantiated in `tradier_stream.py` with `dte_premium_tiers=None`:

```python
# BEFORE (broken)
accumulator = RepetitionAccumulator(
    window_minutes=30,
    min_trades=1,
    min_premium=10_000,
)
```

With `dte_premium_tiers=None`, `_get_episode_min_premium()` fell back to the flat `min_premium=$10,000` floor for all DTE buckets. This bypassed all DTE-stratified tier floors until registry warmup (~30 min) called `set_dte_premium_tiers()`. During cold-start:
- A `$12k 2-DTE` lottery ticket cleared the same floor as a `$500k 45-DTE` institutional print
- All unknown tickers defaulted to the flat $10k floor regardless of DTE

### Fix

Pass `_DEFAULT_DTE_PREMIUM_TIERS` at instantiation so DTE-stratified floors are active from tick 1:

```python
# AFTER (fixed)
from signals.repetition_accumulator import RepetitionAccumulator, _DEFAULT_DTE_PREMIUM_TIERS

accumulator = RepetitionAccumulator(
    window_minutes=30,
    min_trades=1,
    min_premium=10_000,
    dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
)
```

### Deliberation Decisions (3-way panel, 2026-05-03)

**SA-Q1 — Cold-start default tier: T1 (strictest)**
Decision: T1-default stands. Unknown tickers default to T1 until registry warmup confirms their tier. Safe direction is too strict, not too permissive. A $30k DTE=7 print dropped at cold-start is the borderline noise ING-003 is designed to eliminate — the suppression is doing work.

**SA-Q2 — T3-default rejected**
T3-default would pass everything during cold-start, defeating DTE tiers for the first 30 minutes.

**PBE-Q1 — Import safety confirmed**
`_DEFAULT_DTE_PREMIUM_TIERS` is a module-level dict constant — instantiated at import time. No function call, no class instantiation, no side effects.

**PBE-Q2 — `set_dte_premium_tiers()` override confirmed clean**
Post-warmup `set_dte_premium_tiers()` replaces `self.dte_premium_tiers` entirely under lock. No merging, no double-application. Clean atomic replace.

**QA-Q1 — Cold-start accumulator test (D-11, D-12)**
DTE=5, unknown ticker (T1 default, floor=$50k): $30k → None (D-11), $60k → RepetitionEpisode (D-12).

**QA-Q2 — Post-warmup tier override test (D-13)**
After `set_tier_map({"TESTTICKER": 2})`, DTE=5, $30k → RepetitionEpisode (T2 floor=$25k).

### No option choice required
This story had no Option A/B/C decision — it was a single unambiguous fix: pass the existing constant at instantiation.

---

## ALERT-LEVEL — `flow_episodes.alert_level` Always Written as `WATCH`

**Date:** 2026-04-28
**Severity:** High — every `flow_episodes` row was stored with `alert_level = WATCH` regardless of actual episode premium
**Files:** `backend/services/flow_store.py`, `backend/services/tradier_stream.py`

### Root Cause

`_bus_signal_listener()` in `flow_store.py` was building the `persist_flow_episode()` call with:

```python
"alert_level": sig.get("recommendation"),
```

`sig` is the `signal` dict inside the `composite_signal` bus message. `recommendation` is the composite engine field that returns `BUY`, `SELL`, or `HOLD` — completely unrelated to alert level. The `flow_episodes` table schema expects one of `CONVICTION`, `STRONG_SIGNAL`, `ALERT`, or `WATCH` (derived from cumulative episode premium).

Since `recommendation` does not match the alert level enum, Postgres silently accepted the value and stored it literally. Every episode row was persisted with `alert_level = WATCH`.

### Symptom

- `flow_episodes.alert_level` always `WATCH` in Supabase regardless of premium size
- Dashboard alert level badges all showed `WATCH`
- Filtering by `alert_level = CONVICTION` returned 0 rows

### Fix Applied

**`backend/services/tradier_stream.py`** — Added `alert_level` to the `composite_signal` `signal` sub-dict before bus publish:

```python
alert_level = accumulator.get_alert_level(sig_ep)

composite_msg = {
    "type": "composite_signal",
    "data": {
        "signal": {
            "ticker":         composite.ticker,
            "recommendation": composite.recommendation,
            ...
            "alert_level":    alert_level,   # ← injected here
            "reasoning":      composite.reasoning,
        },
        ...
    },
}
```

**`backend/services/flow_store.py`** — `_bus_signal_listener()` reads the correct field:

```python
# BEFORE (wrong — reads BUY/SELL/HOLD)
"alert_level": sig.get("recommendation"),

# AFTER (correct — reads CONVICTION/STRONG_SIGNAL/ALERT/WATCH)
"alert_level": sig.get("alert_level"),
```

### Alert Level Source of Truth

`accumulator.get_alert_level(ep)` in `signals/repetition_accumulator.py`:

| Premium | Level |
|---|---|
| ≥ $1,000,000 | `CONVICTION` |
| ≥ $500,000 | `STRONG_SIGNAL` |
| ≥ $200,000 | `ALERT` |
| < $200,000 | `WATCH` |

---

## DEDUP-KWARGS — `DedupCache.is_duplicate()` Raised `TypeError` on Every Tick

**Date:** 2026-04-28
**Severity:** High — deduplication completely broken in production; Layer 4 silently a no-op again
**Files:** `backend/services/tradier_stream.py`

### Root Cause

`DedupCache.is_duplicate()` in `utils/dedup.py` defines its first parameter as `event_or_occ_symbol` (positional). The call in `_process_trade()` passed it as a keyword argument:

```python
flow_dedup.is_duplicate(
    occ_symbol=occ_symbol,   # ← raises TypeError
    size=ev.size,
    ...
)
```

Python raised `TypeError: got an unexpected keyword argument 'occ_symbol'` on every tick. The outer try/except caught and dropped it silently — `_stats["deduped"]` stayed at 0 and all exchange copies of every trade were written to DB (same symptom as pre-C-019).

### Fix Applied

**`backend/services/tradier_stream.py`** — Pass `occ_symbol` positionally:

```python
# BEFORE
if flow_dedup.is_duplicate(occ_symbol=occ_symbol, size=ev.size, ...):

# AFTER
if flow_dedup.is_duplicate(occ_symbol, size=ev.size, ...):
```

---

## H4 — `_sweep_upgrade_dispatched` Set Never Evicted (Unbounded Memory Leak)

**Date:** 2026-04-28
**Severity:** Medium — memory grew unboundedly over a full trading day; also caused missed sweep upgrades for contracts reprinting after 30 min
**Files:** `backend/services/tradier_stream.py`

### Root Cause

`_sweep_upgrade_dispatched` was `Set[str]`. Keys were added via `.add()` and **never removed**. Over a full day with thousands of unique `occ|size|fill` keys, the set accumulated indefinitely.

Secondary correctness bug: if a contract reprinted after its 30-min episode window (a valid new episode), the stale key in the set would block the retroactive sweep upgrade from being dispatched.

### Fix Applied

**`backend/services/tradier_stream.py`**

Changed to `dict[str, float]` (key → wall-clock timestamp) with TTL eviction before each check:

```python
# BEFORE
_sweep_upgrade_dispatched: set = set()

# AFTER
_sweep_upgrade_dispatched: dict[str, float] = {}
_SWEEP_DISPATCH_TTL_S = 1800.0  # 30 min

# Before membership check:
now = _time.time()
stale = [k for k, ts in _sweep_upgrade_dispatched.items() if now - ts > _SWEEP_DISPATCH_TTL_S]
for k in stale:
    del _sweep_upgrade_dispatched[k]

if dispatch_key not in _sweep_upgrade_dispatched:
    _sweep_upgrade_dispatched[dispatch_key] = now
    asyncio.create_task(upgrade_to_sweep_in_db(...))
```

| Behaviour | Before H4 | After H4 |
|---|---|---|
| Memory over full trading day | Unbounded | Bounded to ~30 min of keys |
| Sweep re-dispatch after 30 min | Silently skipped | Correctly re-dispatched |

---

## Gate 2 — Accumulator Re-Emission Spam on Active QQQ/SPY Episodes

**Date:** 2026-04-28
**Severity:** Medium — high-volume tickers emitted a new `signal_history` and `flow_episodes` row on every tick after Gate 1 was crossed
**Files:** `backend/signals/repetition_accumulator.py`

### Root Cause

`ingest_tick()` returned the episode on every tick after Gate 1 (`trade_count >= 3` OR `total_premium >= $10k`) was crossed. For SPY/QQQ with heavy flow, this meant the downstream signal pipeline received a new emission at ~10–100 ticks/sec, writing a new row to `signal_history` and `flow_episodes` on every single tick.

### Fix Applied

**`backend/signals/repetition_accumulator.py`** — Added `last_signaled_premium: float = 0.0` to `RepetitionEpisode`.

Gate 2 added inside `ingest_tick()` after Gate 1:

```python
# Gate 2: re-emit only on first crossing or after >= $50k new premium
delta = ep.total_premium - ep.last_signaled_premium
if ep.last_signaled_premium == 0 or delta >= self.retrigger:
    ep.last_signaled_premium = ep.total_premium
    return ep
return None
```

Default `SIGNAL_RETRIGGER_THRESHOLD = $50,000`.

| Gate | Condition | Result |
|---|---|---|
| Gate 1 not crossed | `count < 3` AND `prem < $10k` | `None` — dropped |
| First Gate 1 crossing | Either threshold met, `last_signaled_premium == 0` | Episode returned |
| Re-emission | Δ `total_premium >= $50k` | Episode returned |
| Blocked re-emission | Δ < $50k | `None` — no new row |

---

## FLOW-DEBUG — Every Tick Drop Gate Silent in Railway Logs

**Date:** 2026-04-28
**Severity:** Observability — impossible to diagnose stream throughput; a dead stream looked identical to a healthy one
**Files:** `backend/services/tradier_stream.py`

### Root Cause

All drop gates in `_process_trade()` logged at `DEBUG` or were silent. Railway does not surface `DEBUG` by default. With no visible logging, a stream parsing 0 trades/sec and a healthy stream at 500 trades/sec were indistinguishable in the Railway log panel.

### Fix Applied

- `parse_tradier_trade() → None`: upgraded to `INFO` with symbol, size, bid, ask, last
- `accumulator.ingest_tick() → None`: upgraded to `INFO` with ticker, contract, premium, threshold
- Dedup hits: `DEBUG → INFO` with running count, occ_symbol, size, fill, exchange
- First 5 ticks individually at `INFO` (any type) — confirms WebSocket data arriving
- Non-timesale event types at `INFO` for first 10 distinct types seen, then `DEBUG`
- Periodic funnel summary every 100 ticks at `INFO`:
  ```
  [flow-funnel] ticks=100 parsed=87 parse_failed=13 deduped=42 classified=45 accumulator_gated=12 persisted=33 signals=8
  ```
- `_stats` extended: `parsed_count`, `accumulator_gated`, `parse_failed` — exposed on `/health/stream`

---

## U-1 — `options_universe_symbols` Duplicate Rows on Every Restart

**Date:** 2026-04-28
**Severity:** Medium — Railway restart inserted a fresh snapshot with duplicate OCC rows each time
**Files:** `backend/services/universe_store.py`
**Migration:** `backend/migrations/013_*.sql`

### Root Cause

`_sync_save_snapshot()` always generated a new `snapshot_id` (UUID) and inserted fresh rows on startup. No uniqueness constraint existed on `options_universe_symbols`, so Postgres accepted all duplicates. After N restarts the table contained N copies of every symbol.

### Fix Applied

**`backend/services/universe_store.py`** — Snapshot reuse logic:

```python
# Reuse if snapshot is < 20h old AND symbol count within ±10%
existing = _find_recent_snapshot(max_age_hours=20, symbol_count=len(symbols), tolerance=0.10)
if existing:
    snapshot_id = existing["id"]
else:
    snapshot_id = str(uuid4())
    _insert_new_snapshot(snapshot_id, len(symbols))
```

**`backend/migrations/013_*.sql`**:

```sql
ALTER TABLE options_universe_symbols
  ADD CONSTRAINT uq_universe_snapshot_symbol UNIQUE (snapshot_id, symbol);
```

Same constraint added to `chain_store` table.

---

## D-001 — Duplicate `build()` Call: Two Independent `SymbolRegistry` Instances at Startup

**Date:** 2026-04-28
**Severity:** High — doubled Tradier chain API calls; two registries with no shared state; doubled cold-start time
**Files:** `backend/services/tradier_stream.py`, `backend/main.py`

### Root Cause

`main.py` lifespan called `init_registry()` + `registry.build()`. Then `stream_options_flow()` also called `init_registry()` + `build()` internally — creating a second independent registry. Two full Tradier chain fetches (~31,920 symbols each), two `refresh_loop()` tasks running simultaneously, workers using the wrong registry.

### Fix Applied

**`backend/services/tradier_stream.py`** — `stream_options_flow()` accepts `registry=` from lifespan:

```python
async def stream_options_flow(symbols: list[str], registry=None):
```

When `registry` is provided: poll `registry.is_ready()` at 500ms intervals (30-min timeout max) — no `build()` call.
When `registry` is `None` (standalone/test): original inline build + `refresh_loop()` spawn.

**D-002 companion:** `refresh_loop()` only spawned by lifespan in production path — not inside `stream_options_flow()`.

---

## D-003 — Stream Worker Count Hard-Coded to 32 (Coverage Gaps for Large Universe)

**Date:** 2026-04-28
**Severity:** Medium — ~half the OCC symbol universe unstreamed with a 32-worker cap
**Files:** `backend/services/stream_manager.py`

### Root Cause

`StreamManager` spawned exactly 32 workers regardless of `registry.size()`. With ~31,920 OCC symbols at 500 symbols/worker, 64 workers are needed for full coverage. Hard-coding 32 left ~16,000 symbols unstreamed with no log warning.

### Fix Applied

**`backend/services/stream_manager.py`**:

```python
import math
worker_count = math.ceil(registry.size() / _CHUNK_SIZE)
# For 31,920 symbols at _CHUNK_SIZE=500 → 64 workers
```

Worker count logged at `INFO` on startup.

---

## B-008 — Stream Health Endpoint: `errors` / `reconnects` / `last_reconnect_at` Never Written

**Date:** 2026-04-25
**Severity:** Observability — `/health/stream` always returned `errors=0, reconnects=0, last_reconnect_at=null`
**Files:** `backend/services/stream_worker.py`, `backend/tests/test_stream_worker_b008.py`

### Root Cause

`_stats["errors"]`, `_stats["reconnects"]`, and `_stats["last_reconnect_at"]` in `tradier_stream.py` were declared at module level but never written. `StreamWorker` maintained local `self._errors` and `self._reconnects` counters that never propagated to the shared `_stats` dict. `get_stats()` read only its own `_stats`.

### Fix Applied

**`backend/services/stream_worker.py`** — Added two helpers with lazy import (avoids circular dependency):

```python
def _inc_global_error(self) -> None:
    try: _global_stats()["errors"] += 1
    except Exception: pass

def _inc_global_reconnect(self) -> None:
    try:
        s = _global_stats()
        s["reconnects"] += 1
        s["last_reconnect_at"] = _time.time()
    except Exception: pass
```

Wired at every error and reconnect site in `run()`. Tests: `test_stream_worker_b008.py` (5 tests, SW-01–05).

---

## B-023 — Unhandled 429 in `get_session_token()` Caused Crash-Loop

**Date:** 2026-04-25
**Severity:** Reliability — rate-limited token requests entered a crash loop, burning API budget
**Files:** `backend/utils/tradier_client.py`

### Root Cause

`get_session_token()` called `resp.raise_for_status()` without first checking for 429. Under load, Tradier returned 429s which became unhandled `httpx.HTTPStatusError`. Workers caught them as generic exceptions, slept `_backoff()`, then immediately re-requested tokens — re-triggering the 429.

### Fix Applied

Explicit 429 check before `raise_for_status()`. Reads `Retry-After` header (defaults to `_DEFAULT_RETRY_AFTER_S = 10.0`). Sleeps the correct window, then retries.

---

## B-022 — 32 Concurrent Session Token Requests at Startup

**Date:** 2026-04-25
**Severity:** Reliability — burst of simultaneous token POSTs triggered Tradier rate-limiter; workers failed to connect
**Files:** `backend/utils/tradier_client.py`

### Root Cause

All workers called `get_session_token()` at startup simultaneously. ~32 concurrent POSTs to `/v1/markets/events/session` exceeded Tradier's rate limit. Without B-023 handling, the 429s became crash loops.

### Fix Applied

`_SESSION_SEM = asyncio.Semaphore(3)` module-level. All `get_session_token()` calls serialized to max 3 concurrent. 32 workers batch through in `⌈32/3⌉ = 11` batches × ~400ms = **~4.4s total** one-time startup cost.

---

## B-021 — All Workers Started Simultaneously (Zero Stagger)

**Date:** 2026-04-25
**Severity:** Reliability — amplified B-022 by causing simultaneous semaphore contention at t=0
**Files:** `backend/services/stream_manager.py`, `backend/services/stream_worker.py`

### Fix Applied

Each worker receives `startup_delay_s = idx * 0.200`. Worker 0 starts at 0s, worker 31 at 6.2s. Reconnects do not re-apply this delay. `startup_delay_s` exposed in `worker.stats`. Tests: `TestB021StaggeredStartup` in `test_stream_manager.py` (7 tests).

---

## T-001 — Unit Test Suite: OCC Parser, Bid/Ask Classifier, Repetition Engine

**Date:** 2026-04-25
**Type:** Test coverage
**Files:** `tests/test_occ_parser.py` (40), `tests/test_classifier.py` (24), `tests/test_repetition_engine.py` (22)

Covers: full OCC parse paths, all 4 influence tiers, synthetic quote detection, golden sweep, DTE calc, `bid_ask_class` edge cases, all 4 trade type detections, Gate 1 / Gate 2 accumulator logic, all 4 alert levels, episode isolation across contracts.

**Grand total after T-001: ~250 tests.**

---

## C-020 — Tier Engine + Universe Tier Assignment

**Date:** 2026-04-25
**Severity:** Enhancement — no tier metadata; `backtest_score` OI factor defaulted to 0.5 for all symbols
**Files:** `backend/services/tier_engine.py` *(new)*, `backend/services/universe_store.py`, `backend/main.py`
**Migrations:** `010_add_tier_and_oi_to_universe.sql`, `011_add_tier_thresholds.sql`

New `TierEngine` class: loads thresholds from DB, assigns T1/T2/T3 per symbol volume, upserts back. Admin whitelist (SPY, QQQ, AAPL, TSLA, NVDA, MSFT, AMZN, META, GOOGL, AMD, PLTR, COIN) always forces T1. `set_tier_map()` builds in-memory lookup for signal pipeline. `load_tier_map()` added to `universe_store`.

| Tier | Label | Min Avg Volume |
|---|---|---|
| T1 | Liquid large-cap | ≥ 20M |
| T2 | Mid-cap | ≥ 2M |
| T3 | Standard (default) | ≥ 500K |

---

## C-019 — Layer 4 Dedup Inert + TTL Too Tight + Sweep Never Firing (5 Bugs)

**Date:** 2026-04-24
**Severity:** High — all dedup was a production no-op; sweeps never detected; premium inflated 2–4× in accumulator
**Files:** `backend/utils/dedup.py`, `backend/services/tradier_stream.py`

**Bug 1** — TTL 2s too tight for MIAX/PHLX lag (real lag: 500ms–5s). Changed to 5s.
**Bug 2** — `int(ts // 2)` time-bucket created gap at 2s boundary: straddle prints both passed as canonical. Replaced with pure `first_seen_ts` + TTL comparison.
**Bug 3** — Fill key `fill:.2f` too tight (exchange rounding). Changed to `fill:.1f`.
**Bug 4** — `flow_dedup` singleton never imported into `tradier_stream.py`. Layer 4 was a no-op from initial implementation.
**Bug 5** — `exchange` was never passed to `is_duplicate()` (defaulted to `""`). `set(["","",""])` length = 1 — sweep threshold (`>= 3`) never reached. Zero sweeps ever detected in production.

---

## C-018 — Synthetic Quotes Polluting `bid_ask_class` / `is_aggressive` Metrics

**Date:** 2026-04-24
**Severity:** Medium — data quality; backtesting aggression metrics skewed
**Fix:** Added `is_synthetic_quote: bool` to `OptionsFlowEvent`. Set `True` when `bid == 0 AND ask == 0`. Persisted to `flow_events` via migration `009_flow_events_synthetic_quote.sql`. Backtest queries should filter `WHERE is_synthetic_quote = false`.

---

## C-017 — Duplicate `flow_episodes` Rows Per Signal Episode

**Date:** 2026-04-24
**Severity:** Medium — 2× rows per episode
**Fix:** `_bus_signal_listener` in `flow_store.py` now writes `flow_episodes` ONLY on `composite_signal` events. Raw `signal` events are WebSocket-only, no DB write.

---

## C-016 — `UnboundLocalError` in `persist_flow_event()` After 100-Row Buffer

**Date:** 2026-04-24
**Severity:** High — every flow event write crashed when buffer hit 100 rows
**Fix:** Added `global _flow_event_buffer` declaration inside `persist_flow_event()`.

---

## C-015 — Stream Filter `trade` Delivering Equity Events Instead of Option Events

**Date:** 2026-04-23
**Severity:** Critical — all strike/expiry/bid/ask values were 0 in DB
**Fix:** Switched Tradier stream `filter` from `trade` to `timesale`.

---

## C-014 — Over-Aggressive Null Guards Silently Dropping Valid Trades

**Date:** 2026-04-23
**Severity:** Medium — trades with fill=0 or unknown contract type silently dropped
**Fix:** Removed `if fill == 0: return None`. Unknown `ctype` defaults to PUT instead of returning `None`.

---

## C-013 — Tradier Stream Envelope Not Unwrapped Before Parse

**Date:** 2026-04-23
**Severity:** High — `parse_tradier_trade()` received outer envelope dict, not inner trade payload
**Fix:** `_process_trade()` unwraps `raw[event_type]` before passing to parser.

---

## C-010 — `flow_episodes` Inserts Failing: 401 / RLS Policy Violation

**Date:** 2026-04-23
**Severity:** Critical — no flow episodes ever persisted to DB
**Root Cause:** `flow_store.py` fell back to `SUPABASE_KEY` (anon) when `SUPABASE_SERVICE_ROLE_KEY` was unset. Anon key is subject to RLS. All inserts rejected with `42501`. The fallback was silent — no error at startup.
**Fix:** Removed the anon key fallback. `_SUPABASE_KEY` reads only `SUPABASE_SERVICE_ROLE_KEY`. Startup warning made explicit if missing.

| Key | Env Var | RLS | Use |
|---|---|---|---|
| Anon / Public | `SUPABASE_KEY` | Enforced | Client-side reads only |
| Service Role | `SUPABASE_SERVICE_ROLE_KEY` | Bypassed | All server-side DB writes |

---

## C-009 — `universe_screener.py` OI Screener Replaced by Batch Quotes

**Date:** 2026-04-20
**Fix:** Replaced `screen_universe()` with `_fetch_batch_quotes()` in Step 3 of universe pipeline. `universe_screener.py` kept for reference, marked deprecated.

---

## C-008 — `stream_eligible` Column Missing from DB Migration

**Date:** 2026-04-20
**Severity:** High — `options_universe_symbols` upsert failing on missing column
**Fix:** Added `stream_eligible`, `last_price`, `volume` columns + index in `002_universe_symbols_quotes.sql`.

---

## C-007 — `config.py` Missing `priority_symbols` Property

**Date:** 2026-04-18
**Fix:** Added `@property priority_symbols` to `config.py` that splits the `UNIVERSE_PRIORITY_SYMBOLS` env var into `list[str]`.

---

## C-006 — `options_universe_snapshots.provider` NOT NULL with No Default

**Date:** 2026-04-18
**Severity:** High — every snapshot insert failed
**Fix:** Always pass `"tradier"` explicitly on insert.

---

## C-005 — supabase-py v2: `.select()` Not Available After `.insert()`

**Date:** 2026-04-17
**Severity:** High — snapshot ID could not be read back after insert
**Fix:** Generate `snapshot_id` via `uuid4()` in Python before insert; use directly without reading back from DB response.
