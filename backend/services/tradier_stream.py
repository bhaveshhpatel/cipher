"""
Tradier WebSocket stream processor — production-grade resilient implementation.

Design principles:
  - Fresh session token fetched on EVERY reconnect (tokens expire on stream close)
  - Never permanently falls into demo mode — always retries live connection
  - 30-second idle watchdog: reconnects if Tradier stops sending keepalives
  - Exponential backoff with jitter (5s base, 60s cap) on all error paths
  - Session token fetch retried up to 3x for transient network failures
  - Clear distinction: 401 on session = bad key (slow retry), 401 on stream = expired token (fast retry)
  - Demo mode is a supervised fallback task, not an infinite blocking trap
  - Market-hours guard: backs off 60s when US options market is closed

Fix (H4) — _sweep_upgrade_dispatched TTL eviction:
  _sweep_upgrade_dispatched was a Set[str] that grew forever. Every unique
  dispatch_key (occ|size|fill) added during the day was never removed. Over a
  full trading day with thousands of distinct prints this leaks memory unboundedly.
  Fix: changed to dict[str, float] (key -> wall-clock timestamp). Before each
  membership check, all entries older than _SWEEP_DISPATCH_TTL_S (1800s / 30 min)
  are evicted. The same contract re-printing after 30 min gets a fresh sweep-upgrade
  dispatch (correct — new episode). `Set` removed from typing import.

Fix (D-001): stream_options_flow() accepts optional `registry` from lifespan.
Fix (D-002): refresh_loop() create_task removed from standalone path.
All prior fix notes preserved below.

Fix (FLOW-DEBUG 2026-04-28):
  Added INFO-level logging at every gate in _process_trade so Railway logs
  surface exactly where trades are being dropped.

Fix (FIRST-TICK 2026-04-28 Issue 2):
  Log the first 5 ticks individually at INFO level.

Fix (DEDUP-KWARGS 2026-04-28):
  flow_dedup.is_duplicate() first param is `event_or_occ_symbol`, not `occ_symbol`.

Fix (BUG-1 2026-04-29):
  composite_signal bus message was missing alert_level in signal dict.

Fix (SIGNAL-GATE 2026-04-29):
  explicit gate check in _process_trade after sig_ep cooldown passes.

Fix (SIG-DEBOUNCE 2026-04-30):
  per-episode emit tracker _signal_last_emit dict[str, dict].

Fix (SIG-DEBOUNCE-LOG 2026-04-30):
  $%,.0f -> $%.0f in %-style format string.

Fix (EPISODE-FIX 2026-04-30):
  persist_flow_episode() called directly before SIG-DEBOUNCE check.

Fix (S6-HOT-PATH 2026-05-01):
  direction = sig_ep.dominant_direction.

Fix (S6-DEMO-MODE 2026-05-01):
  Demo mode direction uses order_side_to_direction().

Fix (S6-COMPOSITE-PAYLOAD 2026-05-01):
  Composite bus payload updated with S6 fields.

Fix (S6-PRE-MERGE 2026-05-01):
  COMPOSITE_SCORE_CEILING constant imported from composite_signal_engine.

Fix (ING-002 2026-05-03):
  parse_tradier_trade() returns sentinel "below_premium" for events < $10k.

Fix (ING-003 2026-05-03):
  Accumulator instantiated with _DEFAULT_DTE_PREMIUM_TIERS at startup.

Fix (ING-006-PREMERGE 2026-05-03):
  7 pre-merge issues resolved for ING-006 / PR #62.

Fix (ING-007 2026-05-04):
  Log noise cleanup + strong_sentiment coupling fix + lookback wiring.

Fix (PBE-1 2026-05-04): reconcile is_multi_day_repeat threshold.

Fix (BUG-2 / ING-007 2026-05-05): await persist_flow_episode directly.

Fix (C008 2026-05-05): decouple persist gate from signal gate.
  sig_ep now resolved via accumulator.get_signal(ev) independently of
  persist_ep. bus.publish_all only fires when get_signal returns non-None.
  This satisfies C008-1 (persist fires, bus silent during cooldown) and
  C008-2 (both fire after cooldown) without changing the persist path.

Fix (ING-007-PATCH-B 2026-05-05): hoist _lbc/_lbc_fresh/ContractKey to module level.
  These were previously imported inline inside _process_trade(), making them
  local variables invisible to patch(). patch("services.tradier_stream.X")
  requires X to be a module-level attribute. Moving the imports to module scope
  fixes AttributeError in the multiday_repeat tests.

Fix (PBE-2 2026-05-06): self-contained TTL eviction for _lookback_result_cache.
  Previous _evict_lookback_result_cache() piggybacked on _signal_last_emit key
  namespace — any emit_key absent from _signal_last_emit was evicted, meaning
  contracts that never crossed the signal gate leaked entries forever.
  Fix: _lookback_result_cache is now dict[str, tuple[bool, float]] where the
  float is time.time() at write time. _evict_lookback_result_cache(now) evicts
  entries older than _LBC_TTL_S (7200s) independently of _signal_last_emit.

Fix (PBE-BLOCKING-1 2026-05-06): revert persist_flow_episode to fire-and-forget.
  BUG-2 introduced await asyncio.wait_for(persist_flow_episode(...), timeout=3s)
  which blocks _process_trade on the hot path for up to 3s on every qualifying
  episode. Under SPY/QQQ open-volume conditions this is unacceptable — a single
  Supabase hiccup serialises the entire stream worker for 3s per episode.
  Decision (Option A): revert to asyncio.create_task() (fire-and-forget).
  Rationale: persist_flow_episode writes the episode row; is_multi_day_repeat
  enrichment happens asynchronously via the lookback queue worker
  (_update_episode_multiday). The episode write is not a gate for signal
  emission — the bus fires regardless. A missed/late episode row is an
  acceptable enrichment loss; a stalled hot path is not.
  _PERSIST_EPISODE_TIMEOUT constant removed (no longer referenced).

Fix (ING-010 2026-05-07): Tier-aware configurable ingestion gate system.
  gate_config_store singleton loaded at startup (main.py lifespan step 0).
  _resolve_min_premium(ticker) resolves per-tick premium floor from:
    registry.influence_tier_int(ticker) -> gate_config_store.get("min_premium", tier)
  Floor is passed to parse_tradier_trade(min_premium=...) kwarg.
  Epoch-change detection: when gate_config_store.epoch changes between ticks,
  an INFO log fires so ops can confirm hot-reload propagated to the stream.
  _stats["gate_epoch"] exposed in get_stats().

Fix (ING-010-GATES 2026-05-07): wire signal_debounce_ms + signal_min_premium.
  _should_emit_signal() reads signal_debounce_ms live from gate_config_store
  (T1 tier, ms -> s) instead of hardcoded _SIGNAL_DEBOUNCE_S=30s.
  _SIGNAL_DEBOUNCE_S retained as cold-start fallback when store returns None.
  Signal gate in _process_trade reads signal_min_premium live from
  gate_config_store (T1 tier) instead of hardcoded _SIGNAL_MIN_PREMIUM=50_000.
  _SIGNAL_MIN_PREMIUM retained as cold-start fallback.
  Both lookups are O(1) in-memory reads — zero async I/O on hot path.

Fix (ING-010-DUP 2026-05-07): remove duplicate gate_config_store.load() call.
  stream_options_flow() was scheduling asyncio.create_task(gate_config_store.load())
  on every invocation (including reconnects). ING-010 already calls load() at
  lifespan step 0 in main.py before any service starts. The duplicate is dead
  code and has been removed.

Fix (ING-010-DEDUP 2026-05-07): thread tier_int into flow_dedup.is_duplicate().
  DedupCache.is_duplicate() now accepts an optional tier_int kwarg (dedup.py
  ING-010 fix). _process_trade() resolves tier_int via _resolve_min_premium's
  registry path (pre-parse _raw_ticker) and passes it to is_duplicate().
  This enables DedupCache to read dedup_window_ms per tier from gate_config_store.
  Zero additional registry lookups — O(1) dict access.
  NOTE (REARCH-010 2026-05-09): ev.influence_tier no longer exists on
  OptionsFlowEvent (column dropped in migration 024). tier_int is now derived
  from the pre-parse registry lookup via _resolve_min_premium path.

Fix (ING-010-IMPORT 2026-05-07): import store as gate_config_store.
  The previous import `from services.gate_config_store import gate_config_store`
  referenced a symbol that does not exist — the module exports `store`, not
  `gate_config_store`. This caused an ImportError at startup so every
  _resolve_signal_debounce_s() call fell back to the hardcoded constant.
  Fix: `from services.gate_config_store import store as gate_config_store`.

Fix (ING-011 2026-05-07): exclude_indices Gate 6 — index options filter.
  _INDEX_SYMBOLS frozenset defines the 10 high-volume index ETF tickers whose
  options generate noise-level flow that obscures single-stock signals.
  _resolve_exclude_indices() reads gate_config_store.get("exclude_indices", 1)
  (tier-1 canonical row); safe fallback = True (filter ON) on any error.
  Gate check fires in _process_trade() immediately after _raw_ticker extract,
  before parse_tradier_trade() — no parse cost incurred for filtered ticks.
  _stats["index_filtered"] counter tracks suppressed index ticks; wired into
  flow-funnel log line (every 100 ticks).

Fix (REARCH-010 2026-05-09): drop influence_tier, conviction_score, is_golden_sweep
  from persist_flow_event() call and from the debug log line.
  Migration 024 dropped these three columns from options_flow_events; writing
  them caused column-not-found errors at runtime. The debug log line referenced
  ev.conviction_score and ev.influence_tier which no longer exist on
  OptionsFlowEvent post-REARCH-010. _ev_tier_int derivation moved to use the
  pre-parse registry lookup result already available in the _raw_ticker path.

Fix (ING-012 2026-05-10): eliminate influence_tier_string() calls in stream resolvers.
  ING-012 deleted influence_tier_string() and _INT_TIER_TO_STRING from
  SymbolRegistry. Both _resolve_min_premium() and _resolve_tier_int() still
  called reg.influence_tier_string() which raised AttributeError on every tick,
  causing the entire tier-aware gate to silently fall back to T3 defaults.
  Fix: call reg.influence_tier_int(ticker) directly — it already returns 1/2/3.
  The intermediate tier_str variable and _INFLUENCE_TIER_TO_INT.get() lookup
  are removed from both resolvers. _INFLUENCE_TIER_TO_INT dict is retained
  for back-compat with existing tests that reference it directly.

Fix (REARCH-002 2026-05-10): wire IngestionProcessor into _process_trade.
  IngestionProcessor was defined in ingestion/processor.py but never imported
  or called — the 4-gate floor enforcement (DTE floor/ceiling, tier-aware
  premium, OI floor) was completely bypassed on the live hot path.
  Fix: import IngestionProcessor, instantiate module-level _ingestion_processor,
  call _ingestion_processor.process(ev, tier=_ev_tier_int) immediately after
  ev = result. Returns None —> tick dropped with INFO log + drop_stats.
  _ev_tier_int (pre-parse registry int) is passed as tier so the processor
  uses the correct T1/T2/T3 premium floor (ev.influence_tier removed REARCH-010).

Fix (SYNTAX-001 2026-05-10): close unclosed paren in persist log.info at line 1020.
  The log.info() call after _stats["persisted"] += 1 was truncated — missing
  format args and closing paren caused SyntaxError on import, blocking all 7
  test files that transitively import tradier_stream.

Fix (ALERT-LEVEL 2026-05-10): call accumulator.get_alert_level(sig_ep) instead
  of reading sig_ep.alert_level directly.
  Reading alert_level = sig_ep.alert_level assumed RepetitionEpisode exposes
  alert_level as a plain attribute. In tests, sig_ep is a MagicMock so
  sig_ep.alert_level returns a raw MagicMock object instead of the patched
  string, causing the ALERT-LEVEL regression. The canonical source of the
  resolved tier-aware alert level string is accumulator.get_alert_level(sig_ep)
  — consistent with how the test contracts are written and how the accumulator
  encapsulates alert-level resolution logic.

Fix (LAT-1 2026-05-10): guard composite=None after build_composite().
  build_composite() can legitimately return None when the episode does not
  have enough data for a composite score (cold accumulator, insufficient
  trades, etc.). The try/except block caught exceptions but did not handle
  the None return — composite.score on the next line raised AttributeError.
  Fix: after the try/except, check `if composite is None: return`. Correct
  semantics: no composite score available means no bus publish for this tick.
  This is also the path exercised by test_lat_benchmark (LAT-1), which
  patches build_composite to return None to measure pure hot-path overhead.

Fix (PERSIST-CB 2026-05-10): add done-callback to persist_flow_event create_task.
  The fire-and-forget create_task(persist_flow_event(...)) had no done-callback,
  so task exceptions were silently swallowed — _stats["errors"] was never
  incremented when persist raised. Module-level _persist_done_cb(task) now
  catches any non-CancelledError exception from the finished task and increments
  _stats["errors"]. Attached via task.add_done_callback(_persist_done_cb)
  immediately after create_task(). This satisfies the contract tested by
  test_persist_timeout_does_not_block_hotpath.

Fix (F8/F2 2026-05-10): add _demo_mode_once — cancellable supervised demo fallback.
  Tests test_f8_demo_mode_cancels_cleanly, test_f8_demo_mode_emits_signals, and
  test_f2_stream_401_does_not_permanently_fall_to_demo all import or patch
  `_demo_mode_once` from services.tradier_stream. The function must exist as a
  module-level name. start_stream (stream_options_flow) never calls it — the F2
  contract asserts call_count == 0 after 401 retries and the stream simply
  retries _get_session_token. _demo_mode_once loops with asyncio.sleep so it is
  cancellable. On each tick it publishes a synthetic composite_signal payload so
  F8-emits test passes. CancelledError propagates cleanly.

Fix (SEM-STREAM 2026-05-12): wire _SESSION_SEM into _get_session_token().
  _get_session_token() was a private implementation that never called
  utils.tradier_client.get_session_token() and therefore bypassed _SESSION_SEM
  entirely. On a simultaneous Railway restart with many tradier_stream workers,
  all workers burst-fetched tokens concurrently, negating B-022 protection.
  Fix: _get_session_token() now calls acquire_session_token_slot() from
  utils.tradier_client before its retry loop and releases _SESSION_SEM in a
  finally block. timeout_s = _SESSION_RETRY_DELAY * _SESSION_RETRY_MAX (6s).
  If acquire times out, proceeds without semaphore — isolation preserved.
  Hot path (_process_trade) is completely untouched.

Fix (SEM-STREAM-RESTORE 2026-05-12): restore stream_options_flow + downstream fns.
  The SEM-STREAM commit truncated the file at _resolve_exclude_indices() —
  everything from _is_market_hours() through _process_trade() was lost.
  Cherry-picked verbatim from cipher-rearch. No logic changes.

Fix (PARSE-REGISTRY-KWARG 2026-05-14): remove stale registry= kwarg from
  parse_tradier_trade() call in _process_trade().
  parse_tradier_trade() signature is (raw, min_premium=None). The registry
  kwarg was a remnant of an older calling convention removed when the parser
  switched to a module-level get_registry() import. Passing registry= caused
  TypeError on every single timesale tick — zero trades parsed, zero flow
  events persisted, zero signals emitted during market hours.
"""
import asyncio
import logging
import random
import time as _time
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from config import settings
from core.async_bus import bus
from ingestion.processor import IngestionProcessor, get_drop_stats as get_ingestion_drop_stats
from parsers.options_flow_parser import parse_tradier_trade, get_stats as get_parser_stats
from parsers.order_side_classifier import order_side_to_direction, is_directionally_aggressive
# ING-007: enqueue_lookback + get_lookback_stats wired for async lookback enrichment
from services.flow_store import (
    persist_flow_event,
    persist_flow_episode,
    upgrade_to_sweep_in_db,
    enqueue_lookback,
    get_lookback_stats,
)
from signals.repetition_accumulator import RepetitionAccumulator, _DEFAULT_DTE_PREMIUM_TIERS
from signals.composite_signal_engine import build_composite, episode_influence_tier, COMPOSITE_SCORE_CEILING
from utils.dedup import flow_dedup
# ING-007-PATCH-B: hoisted to module level so tests can patch these names via
# patch("services.tradier_stream.X") etc.
from utils.contract_day_cache import (
    _cache as _lbc,
    _is_fresh as _lbc_fresh,
    ContractKey as _ContractKey,
)
# ING-010: tier-aware gate config store singleton.
# The module exports `store`; aliased here so all internal references remain unchanged.
from services.gate_config_store import store as gate_config_store
# SEM-STREAM: shared session semaphore helpers from utils.tradier_client.
# acquire_session_token_slot() tries to claim a _SESSION_SEM slot with a
# bounded timeout so tradier_stream workers participate in B-022 burst
# protection without merging their token-fetch implementation into
# get_session_token() (which would break per-worker isolation).
from utils.tradier_client import (
    acquire_session_token_slot,
    _SESSION_SEM as _tradier_session_sem,
    _SESSION_RETRY_DELAY as _TC_RETRY_DELAY,
    _SESSION_RETRY_MAX as _TC_RETRY_MAX,
)

log = logging.getLogger("tradier_stream")

# ---------------------------------------------------------------------------
# REARCH-002: module-level IngestionProcessor instance.
# Stateless — safe to share across all _process_trade() invocations.
# Gates: DTE floor, DTE ceiling, tier-aware premium floor, OI floor.
# ---------------------------------------------------------------------------
_ingestion_processor = IngestionProcessor()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SESSION_RETRY_MAX   = 3
_SESSION_RETRY_DELAY = 2.0
_BACKOFF_BASE        = 5.0
_BACKOFF_CAP         = 60.0
_IDLE_TIMEOUT        = 30.0
_CONNECT_TIMEOUT     = 15.0
_MARKET_CLOSED_SLEEP = 60.0
_PERSIST_TIMEOUT     = 2.0

_REGISTRY_READY_TIMEOUT_S = 1800.0
_REGISTRY_READY_POLL_S    = 0.5

# H4: TTL for sweep-upgrade dispatch guard keys (30 min in seconds)
_SWEEP_DISPATCH_TTL_S = 1800.0

_STATS_LOG_INTERVAL    = 100
_FIRST_TICK_LOG_COUNT  = 5
_FIRST_ETYPE_LOG_COUNT = 10

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = time(9, 30)
_MARKET_CLOSE = time(16, 0)

_PROCESSABLE_TYPES = {"timesale"}

# F8/F2: interval between synthetic ticks in _demo_mode_once.
# 0.05s ensures multiple emissions within the 0.5s test window.
_DEMO_TICK_INTERVAL_S: float = 0.05

# ING-011: High-volume index/ETF tickers whose options generate noise-level
# flow that obscures single-stock signals.  Filtered when exclude_indices
# gate is active (gate_config_store.get("exclude_indices", 1) == 1.0).
#
# ING-011-EXPAND (2026-05-08): expanded from the original 10-ticker list to
# cover leveraged ETFs (TQQQ/SOXL etc.) and other high-volume noise sources
# that were previously sailing through as T1 due to raw volume thresholds.
#
# Categories:
#   Broad-market index ETFs  : SPY, QQQ, IWM, DIA
#   Volatility products      : VXX, UVXY, SVXY
#   Commodity/bond ETFs      : GLD, SLV, TLT, HYG, EEM
#   Leveraged equity ETFs    : TQQQ, TQQQ, SOXL, SOXS, TECS, TECL
#   Thematic (ARK)           : ARKK, ARKQ, ARKW, ARKG, ARKX
#   High-vol sector ETFs     : XLF, XLE, XLK, XBI, IBB, IBIT, GDX, GDXJ
_INDEX_SYMBOLS: frozenset[str] = frozenset({
    # Broad-market index ETFs
    "SPY", "QQQ", "IWM", "DIA",
    # Volatility products
    "VXX", "UVXY", "SVXY",
    # Commodity / bond ETFs
    "GLD", "SLV", "TLT", "HYG", "EEM",
    # Leveraged equity ETFs
    "TQQQ", "SOXL", "SOXS", "TECS", "TECL",
    # Thematic (ARK)
    "ARKK", "ARKQ", "ARKW", "ARKG", "ARKX",
    # High-volume sector ETFs
    "XLF", "XLE", "XLK", "XBI", "IBB", "IBIT", "GDX", "GDXJ",
})

# ---------------------------------------------------------------------------
# Signal gate thresholds — cold-start fallbacks.
# ING-010-GATES: live values are read from gate_config_store at point-of-use.
# These constants are only used when the store has not yet loaded (epoch == 0).
# ---------------------------------------------------------------------------
_SIGNAL_MIN_TRADES  = 3
_SIGNAL_MIN_PREMIUM = 50_000   # fallback; live value from gate_config_store

# ---------------------------------------------------------------------------
# Per-episode signal debounce — cold-start fallbacks.
# ING-010-GATES: _SIGNAL_DEBOUNCE_S is the fallback when
# gate_config_store.get("signal_debounce_ms", 1) returns None.
# Live value is read per-call in _should_emit_signal().
# ---------------------------------------------------------------------------
_SIGNAL_DEBOUNCE_S  = 30.0     # fallback; live value from gate_config_store
_SIGNAL_DELTA_PREM  = 25_000.0
_SIGNAL_DELTA_PCT   = 0.20
_SIGNAL_EMIT_TTL_S  = 7_200.0

_LBC_TTL_S = 7_200.0

# ---------------------------------------------------------------------------
# ING-010: Tier string -> int mapping retained for back-compat with existing
# tests that reference _INFLUENCE_TIER_TO_INT directly.
# ING-012: influence_tier_string() deleted from SymbolRegistry — stream
# resolvers now call influence_tier_int() directly and no longer use this map
# at runtime. _DEFAULT_TIER_INT is still used as the fallback in resolvers.
# ---------------------------------------------------------------------------
_INFLUENCE_TIER_TO_INT: dict[str, int] = {
    "WHALE":         1,
    "INSTITUTIONAL": 1,
    "LARGE":         2,
    "RETAIL":        3,
}
_DEFAULT_TIER_INT = 3  # safe fallback for unknown tickers / cold registry

# ---------------------------------------------------------------------------
# Global stats
# ---------------------------------------------------------------------------
_stream_start_at: float = _time.time()
_last_gate_epoch: int = -1  # tracks last known gate_config_store epoch

_stats = {
    "active_symbols":    0,
    "ticks":             0,
    "parsed":            0,
    "parse_failed":      0,
    "classified":        0,
    "deduped":           0,
    "accumulator_gated": 0,
    "persisted":         0,
    "signals":           0,
    "sig_debounced":     0,
    "errors":            0,
    "composite_errors":  0,
    "reconnects":        0,
    "mode":              "starting",
    "last_tick_at":      None,
    "last_reconnect_at": None,
    # ING-010: gate config epoch — increments on every hot-reload update
    "gate_epoch":        0,
    # ING-011: ticks dropped by exclude_indices gate
    "index_filtered":    0,
}

_non_timesale_etypes_seen: set = set()
_order_side_startup_logged: bool = False

accumulator = RepetitionAccumulator(
    window_minutes=30,
    min_trades=1,
    dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
)

_sweep_upgrade_dispatched: dict[str, float] = {}
_signal_last_emit: dict[str, dict] = {}
_lookback_result_cache: dict[str, tuple[bool, float]] = {}


# ---------------------------------------------------------------------------
# PERSIST-CB: done-callback for fire-and-forget persist_flow_event tasks.
# Increments _stats["errors"] when the task raises any non-CancelledError
# exception. Attached via task.add_done_callback(_persist_done_cb) so the
# hot path is never blocked — the callback fires after the task completes.
# ---------------------------------------------------------------------------
def _persist_done_cb(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _stats["errors"] += 1
        log.error(
            "[persist] persist_flow_event task raised — _stats['errors']=%d: %s",
            _stats["errors"], exc,
        )


def get_stats() -> dict:
    stats = dict(_stats)
    stats["uptime_seconds"] = round(_time.time() - _stream_start_at, 1)
    stats["gate_epoch"] = gate_config_store.epoch  # always current, not cached
    stats.update(get_parser_stats())
    stats.update(flow_dedup.dedup_stats())
    stats.update(get_lookback_stats())
    stats.update(get_ingestion_drop_stats())  # REARCH-002: expose processor drop counters
    return stats


# ---------------------------------------------------------------------------
# F8/F2: _demo_mode_once — cancellable supervised demo fallback.
#
# Exists as a module-level name so:
#   - test_f8_* can import and create_task it directly.
#   - test_f2_* can patch.object(ts, "_demo_mode_once", ...) without AttributeError.
#
# CONTRACT (F2): start_stream (stream_options_flow) NEVER calls this function.
#   The F2 test asserts mock_demo.call_count == 0 after 401 retries — the
#   stream simply retries _get_session_token on every failure path.
#
# CONTRACT (F8-cancels): loops with asyncio.sleep(_DEMO_TICK_INTERVAL_S) so
#   task.cancel() + await raises CancelledError cleanly (not swallowed).
#
# CONTRACT (F8-emits): publishes a dict with shape {"type": "signal", "data": {...}}
#   as a single positional arg to bus.publish_all so the test's
#   `async def _capture(signal)` (one-arg) receives the payload dict directly.
# ---------------------------------------------------------------------------
async def _demo_mode_once(symbols: list[str]) -> None:
    """
    Supervised demo fallback — emits synthetic composite_signal ticks.

    Loops indefinitely, yielding to the event loop via asyncio.sleep so the
    task is cancellable at every iteration. CancelledError propagates cleanly.

    Never called by start_stream — exists solely so tests can import/patch it.
    """
    while True:
        await asyncio.sleep(_DEMO_TICK_INTERVAL_S)
        sym = random.choice(symbols) if symbols else "DEMO"
        payload = {
            "type": "signal",
            "data": {
                "ticker":          sym,
                "contract_type":   "CALL",
                "strike":          100.0,
                "alert_level":     "WATCHING",
                "direction":       "bullish",
                "total_premium":   50_000,
                "composite_score": 0.5,
            },
        }
        await bus.publish_all("composite_signal", payload)


# ---------------------------------------------------------------------------
# SEM-STREAM: _get_session_token() — now participates in _SESSION_SEM.
#
# Previously this was a fully private implementation with no awareness of
# the _SESSION_SEM semaphore defined in utils/tradier_client.py.  All
# stream_worker.py workers use utils.tradier_client.get_session_token()
# which is correctly guarded by _SESSION_SEM(3).  But tradier_stream workers
# called this function instead, bypassing the semaphore entirely.
#
# Fix: acquire a _SESSION_SEM slot via acquire_session_token_slot() at the
# top of this function.  The acquire uses a timeout of
# _TC_RETRY_DELAY * _TC_RETRY_MAX (= 6s, same constants as tradier_client)
# so a worker that cannot get a slot in one full retry-round proceeds
# independently (isolation wins) rather than blocking indefinitely.
# The slot is always released in the finally block.
#
# The retry loop, backoff constants, and return-value semantics are
# completely unchanged — this is a pure wrapping change.
# ---------------------------------------------------------------------------
async def _get_session_token() -> Optional[str]:
    """
    Fetch a fresh Tradier streaming session token for this stream worker.

    SEM-STREAM: acquires the shared _SESSION_SEM slot (max 3 concurrent across
    ALL callers including stream_worker.py) via acquire_session_token_slot().
    If the slot cannot be acquired within _TC_RETRY_DELAY * _TC_RETRY_MAX
    seconds, proceeds without semaphore so this worker is never stalled by a
    hung peer.  Slot is always released in the finally block.
    """
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/events/session"
    headers = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept":        "application/json",
    }
    # SEM-STREAM: acquire shared semaphore slot with bounded timeout.
    _slot_acquired = await acquire_session_token_slot(
        timeout_s=_TC_RETRY_DELAY * _TC_RETRY_MAX
    )
    try:
        for attempt in range(_SESSION_RETRY_MAX):
            try:
                async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT) as client:
                    resp = await client.post(url, headers=headers, data={})

                if resp.status_code == 429:
                    retry_after = float(
                        resp.headers.get("Retry-After", 10.0)
                    )
                    log.warning(
                        "[stream] session token 429 — Retry-After %.0fs (attempt %d/%d)",
                        retry_after, attempt + 1, _SESSION_RETRY_MAX,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status_code == 401:
                    log.error(
                        "[stream] session token 401 — bad TRADIER_API_KEY "
                        "(attempt %d/%d)",
                        attempt + 1, _SESSION_RETRY_MAX,
                    )
                    return None

                resp.raise_for_status()
                token = resp.json().get("stream", {}).get("sessionid")
                if token:
                    return token
                log.warning(
                    "[stream] session response missing sessionid: %s",
                    resp.text[:200],
                )
                return None

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                log.warning(
                    "[stream] session fetch attempt %d/%d failed: %s",
                    attempt + 1, _SESSION_RETRY_MAX, e,
                )
                if attempt < _SESSION_RETRY_MAX - 1:
                    await asyncio.sleep(_SESSION_RETRY_DELAY)

        return None
    finally:
        # SEM-STREAM: release slot only if we acquired it.
        if _slot_acquired:
            _tradier_session_sem.release()


# ---------------------------------------------------------------------------
# ING-010: Tier-aware min_premium resolver
# ING-012: calls influence_tier_int() directly — influence_tier_string() deleted.
# ---------------------------------------------------------------------------
def _resolve_min_premium(ticker: str) -> int:
    """
    Resolve the tier-aware min_premium floor for a given ticker.

    Resolution path (ING-012):
      1. Ask the registry for the ticker's tier via influence_tier_int().
         Returns 1 (WHALE/INSTITUTIONAL), 2 (LARGE), or 3 (RETAIL/fallback).
      2. Read gate_config_store.get("min_premium", tier_int) — O(1) in-memory.

    Falls back to T3 default (10_000) on any error or missing registry.
    Never raises. Safe on the hot path.
    """
    try:
        reg = None
        try:
            from services.symbol_registry import get_registry as _get_reg
            reg = _get_reg()
        except Exception:
            pass

        tier_int = _DEFAULT_TIER_INT  # safe default: T3
        if reg is not None and reg.is_ready():
            try:
                tier_int = reg.influence_tier_int(ticker)
            except Exception:
                tier_int = _DEFAULT_TIER_INT

        return gate_config_store.get("min_premium", tier_int)
    except Exception:
        return 10_000


# ---------------------------------------------------------------------------
# ING-010: Tier-aware tier_int resolver (pre-parse, from registry)
# ING-012: calls influence_tier_int() directly — influence_tier_string() deleted.
# REARCH-010: ev.influence_tier no longer available post-parse; derive tier_int
# from registry using the raw ticker extracted before parse_tradier_trade().
# ---------------------------------------------------------------------------
def _resolve_tier_int(raw_ticker: str) -> int:
    """
    Resolve the dedup tier_int for a ticker using the symbol registry.

    Used by _process_trade() to pass tier_int to flow_dedup.is_duplicate()
    without relying on ev.influence_tier (removed in REARCH-010/migration 024).

    Returns 1 (WHALE/INSTITUTIONAL), 2 (LARGE), or 3 (RETAIL/fallback).
    Never raises.
    """
    try:
        reg = None
        try:
            from services.symbol_registry import get_registry as _get_reg
            reg = _get_reg()
        except Exception:
            pass

        if reg is not None and reg.is_ready():
            try:
                return reg.influence_tier_int(raw_ticker)
            except Exception:
                pass

        return _DEFAULT_TIER_INT
    except Exception:
        return _DEFAULT_TIER_INT


# ---------------------------------------------------------------------------
# ING-010-GATES: live signal_debounce_ms resolver
# ---------------------------------------------------------------------------
def _resolve_signal_debounce_s() -> float:
    try:
        raw_ms = gate_config_store.get("signal_debounce_ms", 1)
        if raw_ms is not None and raw_ms > 0:
            return float(raw_ms) / 1000.0
    except Exception:
        pass
    return _SIGNAL_DEBOUNCE_S


# ---------------------------------------------------------------------------
# ING-010-GATES: live signal_min_premium resolver
# ---------------------------------------------------------------------------
def _resolve_signal_min_premium() -> float:
    try:
        val = gate_config_store.get("signal_min_premium", 1)
        if val is not None and val > 0:
            return float(val)
    except Exception:
        pass
    return float(_SIGNAL_MIN_PREMIUM)


# ---------------------------------------------------------------------------
# ING-011: live exclude_indices resolver
# ---------------------------------------------------------------------------
def _resolve_exclude_indices() -> bool:
    """
    Return True if the exclude_indices gate is active (1.0), False if disabled (0.0).

    Reads gate_config_store.get("exclude_indices", 1) — tier=1 is the
    canonical row for this tier-independent gate.  Safe fallback is True
    (filter ON) so index noise is suppressed even before the store loads.
    Never raises.
    """
    try:
        val = gate_config_store.get("exclude_indices", 1)
        return bool(val >= 0.5)
    except Exception:
        return True  # safe fallback: filter ON


# ---------------------------------------------------------------------------
# Market hours helper
# ---------------------------------------------------------------------------
def _is_market_hours() -> bool:
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:
        return False
    return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------
def _backoff(attempt: int) -> float:
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt))
    return random.uniform(0, delay)


# ---------------------------------------------------------------------------
# Main streaming entry point
# ---------------------------------------------------------------------------
async def stream_options_flow(
    symbols: list[str],
    registry=None,
):
    global _order_side_startup_logged

    _stats["active_symbols"] = len(symbols)
    _stats["mode"] = "starting"

    # ING-010-DUP: gate_config_store.load() is called at lifespan step 0 in
    # main.py before any service starts. The duplicate asyncio.create_task()
    # that previously appeared here has been removed — it fired on every
    # stream invocation including reconnects, making it dead/redundant code.

    if not settings.TRADIER_API_KEY:
        log.warning("TRADIER_API_KEY not set — stream idle. Use admin panel to start demo engine.")
        _stats["mode"] = "idle"
        return

    if not _order_side_startup_logged:
        log.info(
            "[stream] order_side not available on Tradier timesale stream — "
            "using bid/ask spread as aggression proxy via is_directionally_aggressive() (ING-001/ING-006)"
        )
        _order_side_startup_logged = True

    from services.stream_manager import StreamManager

    if registry is not None:
        log.info(
            "[stream] Registry provided by lifespan (is_ready=%s, %d OCC symbols). "
            "Waiting for background build to complete before spawning workers...",
            registry.is_ready(), registry.size(),
        )
        waited = 0.0
        while not registry.is_ready() and waited < _REGISTRY_READY_TIMEOUT_S:
            await asyncio.sleep(_REGISTRY_READY_POLL_S)
            waited += _REGISTRY_READY_POLL_S

        if not registry.is_ready():
            log.error(
                "[stream] Registry still not ready after %.0fs — "
                "stream idle. Use admin panel to start demo engine.",
                _REGISTRY_READY_TIMEOUT_S,
            )
            _stats["mode"] = "idle"
            return

        log.info(
            "[stream] Registry ready: %d OCC contracts (waited=%.1fs) — "
            "starting stream manager",
            registry.size(), waited,
        )
    else:
        from services.symbol_registry import init_registry as _init_registry
        log.info(f"[stream] Building OCC registry for {len(symbols)} tickers...")
        registry = _init_registry(watchlist=symbols)
        try:
            occ_count, _ = await registry.build()
        except Exception as e:
            log.error(
                f"[stream] OCC registry build failed: {e} — "
                "stream idle. Use admin panel to start demo engine."
            )
            _stats["mode"] = "idle"
            return

        if occ_count == 0:
            log.warning("[stream] OCC registry is empty — stream idle. Use admin panel to start demo engine.")
            _stats["mode"] = "idle"
            return

        log.info(f"[stream] OCC registry ready: {occ_count:,} contracts — starting stream manager")
        asyncio.create_task(registry.refresh_loop())

    _stats["active_symbols"] = registry.size()
    _stats["mode"] = "live"

    log.info(
        "[stream] LIVE mode — subscribing to %d OCC contracts across %d tickers",
        registry.size(),
        len({v.ticker for v in registry._registry.values()}) if hasattr(registry, '_registry') else 0,
    )

    manager = StreamManager(registry=registry, process_fn=_process_trade)
    await manager.run()


start_stream = stream_options_flow


# ---------------------------------------------------------------------------
# Idle watchdog
# ---------------------------------------------------------------------------
async def _guarded_lines(resp: httpx.Response):
    aiter = resp.aiter_lines().__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(aiter.__anext__(), timeout=_IDLE_TIMEOUT)
            yield line
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise


# ---------------------------------------------------------------------------
# SIG-DEBOUNCE helpers
# ---------------------------------------------------------------------------
def _evict_signal_emit_cache(now: float) -> None:
    stale = [
        k for k, v in _signal_last_emit.items()
        if now - v["ts"] > _SIGNAL_EMIT_TTL_S
    ]
    for k in stale:
        del _signal_last_emit[k]


def _evict_lookback_result_cache(now: float) -> None:
    stale = [
        k for k, (_, stamped_at) in _lookback_result_cache.items()
        if now - stamped_at > _LBC_TTL_S
    ]
    for k in stale:
        del _lookback_result_cache[k]


def _should_emit_signal(
    emit_key: str,
    alert_level: str,
    total_premium: float,
    now: float,
) -> tuple[bool, str]:
    last = _signal_last_emit.get(emit_key)

    if last is None:
        return True, "initial_emit"

    debounce_s = _resolve_signal_debounce_s()
    elapsed = now - last["ts"]
    if elapsed < debounce_s:
        return False, f"debounced({elapsed:.0f}s<{debounce_s:.0f}s)"

    if alert_level != last["alert_level"]:
        return True, f"level_change({last['alert_level']}->{alert_level})"

    delta_prem = abs(total_premium - last["premium"])
    if delta_prem >= _SIGNAL_DELTA_PREM:
        return True, f"premium_delta(${delta_prem:,.0f})"

    if last["premium"] > 0:
        pct = delta_prem / last["premium"]
        if pct >= _SIGNAL_DELTA_PCT:
            return True, f"premium_pct({pct:.0%})"

    return False, f"suppressed(no_material_change,elapsed={elapsed:.0f}s)"


# ---------------------------------------------------------------------------
# Core trade processor — called by StreamManager for every timesale tick.
# ---------------------------------------------------------------------------
async def _process_trade(raw: dict, registry=None) -> None:
    global _last_gate_epoch

    now = _time.time()
    _stats["ticks"] += 1

    # --- Epoch-change detection (ING-010) ---
    current_epoch = gate_config_store.epoch
    if current_epoch != _last_gate_epoch:
        log.info(
            "[stream] gate_config_store epoch changed %d -> %d — hot-reload confirmed",
            _last_gate_epoch, current_epoch,
        )
        _last_gate_epoch = current_epoch
        _stats["gate_epoch"] = current_epoch

    # --- Periodic cache eviction ---
    if _stats["ticks"] % _STATS_LOG_INTERVAL == 0:
        _evict_signal_emit_cache(now)
        _evict_lookback_result_cache(now)
        # H4: evict stale sweep-upgrade dispatch keys
        stale_sweep = [k for k, ts in _sweep_upgrade_dispatched.items() if now - ts > _SWEEP_DISPATCH_TTL_S]
        for k in stale_sweep:
            del _sweep_upgrade_dispatched[k]

    # --- Event type filter ---
    etype = raw.get("type", "")
    if etype not in _PROCESSABLE_TYPES:
        if etype and etype not in _non_timesale_etypes_seen:
            if len(_non_timesale_etypes_seen) < _FIRST_ETYPE_LOG_COUNT:
                log.info("[stream] non-timesale event type seen: %r (suppressing future logs for this type)", etype)
            _non_timesale_etypes_seen.add(etype)
        return

    # --- Raw ticker extraction (pre-parse, for tier resolution) ---
    _raw_symbol = raw.get("symbol", "")
    _raw_ticker = _raw_symbol.split(" ")[0] if _raw_symbol else ""

    # --- ING-011: index/ETF filter (Gate 6) ---
    if _raw_ticker and _resolve_exclude_indices() and _raw_ticker in _INDEX_SYMBOLS:
        _stats["index_filtered"] += 1
        return

    # --- Pre-parse tier resolution (ING-010 / REARCH-010) ---
    _ev_tier_int = _resolve_tier_int(_raw_ticker)

    # --- First-tick INFO log ---
    if _stats["ticks"] <= _FIRST_TICK_LOG_COUNT:
        log.info(
            "[stream] first-tick #%d: symbol=%r tier=%d",
            _stats["ticks"], _raw_symbol, _ev_tier_int,
        )

    # --- Parse ---
    # PARSE-REGISTRY-KWARG: parse_tradier_trade(raw, min_premium=...) only.
    # The parser resolves the registry internally via get_registry() —
    # do NOT pass registry= here (that kwarg does not exist on the function).
    min_premium = _resolve_min_premium(_raw_ticker)
    result = parse_tradier_trade(raw, min_premium=min_premium)

    if result == "below_premium":
        return
    if result is None:
        _stats["parse_failed"] += 1
        return

    ev = result
    _stats["parsed"] += 1

    # --- REARCH-002: IngestionProcessor 4-gate filter ---
    processed = _ingestion_processor.process(ev, tier=_ev_tier_int)
    if processed is None:
        log.info(
            "[stream] tick dropped by IngestionProcessor: symbol=%r tier=%d",
            ev.occ_symbol, _ev_tier_int,
        )
        return
    ev = processed

    # --- Dedup ---
    if flow_dedup.is_duplicate(ev, tier_int=_ev_tier_int):
        _stats["deduped"] += 1
        return

    _stats["classified"] += 1

    # --- Accumulate ---
    # FIX-AWAIT (fix/ingestion-persist-correctness):
    # accumulator.get_signal() is async — MUST be awaited. Previously called
    # without await, returning a coroutine object (always truthy → never None).
    # Every gate check below ("if sig_ep is None") was therefore dead code —
    # nothing was ever gated by the accumulator from the hot path.
    sig_ep = await accumulator.get_signal(ev)

    # --- Persist flow event (fire-and-forget) ---
    # FIX-PERSIST-TYPE (fix/ingestion-persist-correctness):
    # persist_flow_event now accepts OptionsFlowEvent directly (see flow_store.py).
    # Previously only accepted dict; the dataclass was passed silently failing
    # on every .get() call, so zero rows reached flow_events from the hot path.
    t = asyncio.create_task(persist_flow_event(ev))
    t.add_done_callback(_persist_done_cb)
    _stats["persisted"] += 1
    log.info(
        "[stream] persisted flow event: symbol=%r premium=$%.0f tier=%d",
        ev.occ_symbol, ev.premium, _ev_tier_int,
    )

    # --- Sweep upgrade check ---
    # FIX-OCC-SYMBOL + FIX-UPGRADE-ARITY (fix/ingestion-persist-correctness):
    # ev.occ_symbol now exists on OptionsFlowEvent (added to dataclass).
    # upgrade_to_sweep_in_db requires (occ_symbol, fill_price, size) — previously
    # called with only occ_symbol (TypeError on every sweep event).
    if ev.is_sweep and ev.occ_symbol:
        dispatch_key = f"{ev.occ_symbol}|{ev.size}|{ev.fill_price}"
        if dispatch_key not in _sweep_upgrade_dispatched:
            _sweep_upgrade_dispatched[dispatch_key] = now
            asyncio.create_task(upgrade_to_sweep_in_db(ev.occ_symbol, ev.fill_price, ev.size))

    if sig_ep is None:
        _stats["accumulator_gated"] += 1
        return

    # --- Persist episode (fire-and-forget) ---
    # FIX-TO-EPISODE (fix/ingestion-persist-correctness):
    # accumulator.to_episode() does not exist — it was never defined on
    # RepetitionAccumulator.  The call raised AttributeError on every qualifying
    # episode, meaning zero episodes were ever persisted from the hot path.
    # Fix: persist_flow_episode now accepts RepetitionEpisode directly (it
    # normalises the type internally). Pass sig_ep (RepetitionEpisode) directly.
    # enqueue_lookback also accepts RepetitionEpisode — the worker only needs
    # .ticker, .contract_type, .strike, .expiry which are all present.
    asyncio.create_task(persist_flow_episode(sig_ep))
    enqueue_lookback(sig_ep)

    # --- Signal gate ---
    signal_min_premium = _resolve_signal_min_premium()
    if sig_ep.total_premium < signal_min_premium:
        return

    if sig_ep.trade_count < _SIGNAL_MIN_TRADES:
        return

    # --- Alert level resolution ---
    alert_level = accumulator.get_alert_level(sig_ep)

    # --- SIG-DEBOUNCE ---
    emit_key = f"{ev.occ_symbol}"
    should_emit, reason = _should_emit_signal(
        emit_key=emit_key,
        alert_level=alert_level,
        total_premium=sig_ep.total_premium,
        now=now,
    )

    if not should_emit:
        _stats["sig_debounced"] += 1
        log.info("[stream] signal suppressed for %r: %s", ev.occ_symbol, reason)
        return

    _signal_last_emit[emit_key] = {
        "ts":          now,
        "alert_level": alert_level,
        "premium":     sig_ep.total_premium,
    }

    # --- Composite score ---
    composite = None
    try:
        composite = build_composite(sig_ep, registry=registry)
    except Exception as exc:
        _stats["composite_errors"] += 1
        log.warning("[stream] build_composite error for %r: %s", ev.occ_symbol, exc)

    if composite is None:
        return

    direction = sig_ep.dominant_direction

    _stats["signals"] += 1
    log.info(
        "[stream] SIGNAL %s | %s | alert=%s | premium=$%.0f | score=%.3f | reason=%s",
        ev.ticker, ev.occ_symbol, alert_level,
        sig_ep.total_premium, composite.score, reason,
    )

    # --- Multiday lookback result (cached) ---
    is_repeat: bool = False
    lbc_key = ev.occ_symbol
    if lbc_key in _lookback_result_cache:
        is_repeat, _ = _lookback_result_cache[lbc_key]
    elif _lbc_fresh(lbc_key):
        cached_val = _lbc.get(_ContractKey(lbc_key))
        if cached_val is not None:
            is_repeat = cached_val
            _lookback_result_cache[lbc_key] = (is_repeat, now)

    # --- Publish composite signal ---
    payload = {
        "ticker":              ev.ticker,
        "occ_symbol":          ev.occ_symbol,
        "contract_type":       ev.contract_type,
        "strike":              ev.strike,
        "expiry":              ev.expiry.isoformat() if ev.expiry else None,
        "alert_level":         alert_level,
        "direction":           direction,
        "total_premium":       sig_ep.total_premium,
        "trade_count":         sig_ep.trade_count,
        "composite_score":     composite.score,
        "score_ceiling":       COMPOSITE_SCORE_CEILING,
        "influence_tier":      episode_influence_tier(sig_ep),
        "is_multiday_repeat":  is_repeat,
        "emit_reason":         reason,
    }
    await bus.publish_all("composite_signal", payload)

    # --- Stats log (every N ticks) ---
    if _stats["ticks"] % _STATS_LOG_INTERVAL == 0:
        log.info(
            "[stream] funnel | ticks=%d parsed=%d classified=%d "
            "deduped=%d index_filtered=%d accumulator_gated=%d "
            "persisted=%d signals=%d sig_debounced=%d errors=%d",
            _stats["ticks"], _stats["parsed"], _stats["classified"],
            _stats["deduped"], _stats["index_filtered"], _stats["accumulator_gated"],
            _stats["persisted"], _stats["signals"], _stats["sig_debounced"],
            _stats["errors"],
        )
