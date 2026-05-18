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
    registry.symbol_tier(ticker) -> gate_config_store.get("min_premium", tier)
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

Fix (GATE-001 2026-05-16): exclude_indices as hardcoded first gate, pre-tier-lookup.
  Gate 6 in _process_trade() is now a two-layer check:
    Layer 1 (hardcoded): ingestion.filters.is_etf_noise_symbol(_raw_ticker)
      — fires ALWAYS, independent of gate_config_store, OCC registry, or any
      DB state. Cannot be disabled by config drift, stale hot-reload, or a
      Supabase outage. This is the hard backstop.
    Layer 2 (config-driven): _resolve_exclude_indices() + _INDEX_SYMBOLS check
      — retained for any tickers not in the hardcoded blocklist, controlled
      by gate_configs.exclude_indices. Defense-in-depth.
  Both layers fire before ANY tier lookup or parse_tradier_trade() call.
  _stats["index_filtered"] incremented on either hit.

Fix (FIX-2 2026-05-16): use symbol_tier from OCC registry for tier resolution.
  _resolve_tier_int() and _resolve_min_premium() previously called
  reg.influence_tier_int(ticker) which derived tier from the notional_tier
  (dollar-volume bucket). This misclassified high-notional retail tickers
  as T1 and applied the whale premium floor, effectively ungating them.

  Fix: both resolvers now call reg.symbol_tier(ticker) — the pre-computed
  structural tier from the OCC contract metadata — as the primary lookup.
  Falls back to reg.influence_tier_int(ticker) if symbol_tier() is not
  available on the registry (version guard for rolling deploys).
  Ultimate fallback remains _DEFAULT_TIER_INT = 3 (safe, conservative).

  Impact:
    - T1 premium floor now only applied to genuine structural T1 symbols.
    - T2/T3 symbols receive correct $15k/$10k floors respectively.
    - Dedup window dispatch correctly uses symbol_tier.
    - No hot-path performance change — O(1) dict lookup in the registry.

Fix (DIRECTION-KWARG 2026-05-18): remove stale price/bid/ask kwargs from
  order_side_to_direction() call in Gate 4 of _process_trade().
  order_side_to_direction(order_side, contract_type) takes exactly two positional
  args. The call was incorrectly passing price=ev.fill_price, bid=ev.bid,
  ask=ev.ask — kwargs that belong to is_directionally_aggressive(), not this
  function. This caused TypeError on every single timesale tick, blocking all
  flow from reaching flow_events. Fix: pass ev.order_side and ev.contract_type.
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
from ingestion.filters import is_etf_noise_symbol
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
from utils.tradier_client import (
    acquire_session_token_slot,
    _SESSION_SEM as _tradier_session_sem,
    _SESSION_RETRY_DELAY as _TC_RETRY_DELAY,
    _SESSION_RETRY_MAX as _TC_RETRY_MAX,
)

log = logging.getLogger("tradier_stream")

# ---------------------------------------------------------------------------
# REARCH-002: module-level IngestionProcessor instance.
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

_SWEEP_DISPATCH_TTL_S = 1800.0

_STATS_LOG_INTERVAL    = 100
_FIRST_TICK_LOG_COUNT  = 5
_FIRST_ETYPE_LOG_COUNT = 10

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = time(9, 30)
_MARKET_CLOSE = time(16, 0)

_PROCESSABLE_TYPES = {"timesale"}

_DEMO_TICK_INTERVAL_S: float = 0.05

# ING-011 / GATE-001: High-volume index/ETF tickers.
# Layer 2 (config-driven) uses this set.
# Layer 1 (hardcoded) uses is_etf_noise_symbol() from ingestion/filters.py.
# When adding tickers, update BOTH this set AND _ETF_NOISE_BLOCKLIST.
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
# ---------------------------------------------------------------------------
_SIGNAL_MIN_TRADES  = 3
_SIGNAL_MIN_PREMIUM = 50_000

_SIGNAL_DEBOUNCE_S  = 30.0
_SIGNAL_DELTA_PREM  = 25_000.0
_SIGNAL_DELTA_PCT   = 0.20
_SIGNAL_EMIT_TTL_S  = 7_200.0

_LBC_TTL_S = 7_200.0

# ING-010 / ING-012: retained for back-compat with tests.
_INFLUENCE_TIER_TO_INT: dict[str, int] = {
    "WHALE":         1,
    "INSTITUTIONAL": 1,
    "LARGE":         2,
    "RETAIL":        3,
}
_DEFAULT_TIER_INT = 3

# ---------------------------------------------------------------------------
# Global stats
# ---------------------------------------------------------------------------
_stream_start_at: float = _time.time()
_last_gate_epoch: int = -1

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
    "gate_epoch":        0,
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
    stats["gate_epoch"] = gate_config_store.epoch
    stats.update(get_parser_stats())
    stats.update(flow_dedup.dedup_stats())
    stats.update(get_lookback_stats())
    stats.update(get_ingestion_drop_stats())
    return stats


async def _demo_mode_once(symbols: list[str]) -> None:
    """
    Supervised demo fallback — emits synthetic composite_signal ticks.
    Cancellable. Never called by start_stream.
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
# FIX-2: Tier-aware resolvers — use symbol_tier (structural) not influence_tier
# (notional). Falls back to influence_tier_int if symbol_tier not available
# (registry version guard). Ultimate fallback: _DEFAULT_TIER_INT = 3.
# ---------------------------------------------------------------------------
def _resolve_min_premium(ticker: str) -> int:
    """
    Resolve the tier-aware min_premium floor for a given ticker.

    FIX-2 resolution path:
      1. reg.symbol_tier(ticker)     — structural tier from OCC metadata (preferred)
      2. reg.influence_tier_int(ticker) — notional-volume proxy (legacy fallback)
      3. _DEFAULT_TIER_INT = 3       — safe conservative floor
      4. gate_config_store.get("min_premium", tier_int) — O(1) in-memory read

    Never raises. Safe on the hot path.
    """
    try:
        reg = None
        try:
            from services.symbol_registry import get_registry as _get_reg
            reg = _get_reg()
        except Exception:
            pass

        tier_int = _DEFAULT_TIER_INT
        if reg is not None and reg.is_ready():
            try:
                # FIX-2: prefer symbol_tier (structural), fall back to
                # influence_tier_int (notional) for old registry builds.
                if hasattr(reg, "symbol_tier"):
                    tier_int = reg.symbol_tier(ticker)
                else:
                    tier_int = reg.influence_tier_int(ticker)
            except Exception:
                tier_int = _DEFAULT_TIER_INT

        return gate_config_store.get("min_premium", tier_int)
    except Exception:
        return 10_000


def _resolve_tier_int(raw_ticker: str) -> int:
    """
    Resolve the dedup tier_int for a ticker using the symbol registry.

    FIX-2 resolution path:
      1. reg.symbol_tier(ticker)        — structural tier (preferred)
      2. reg.influence_tier_int(ticker) — notional-volume proxy (legacy fallback)
      3. _DEFAULT_TIER_INT = 3          — safe conservative floor

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
                # FIX-2: prefer symbol_tier (structural), fall back to
                # influence_tier_int (notional) for old registry builds.
                if hasattr(reg, "symbol_tier"):
                    return reg.symbol_tier(raw_ticker)
                return reg.influence_tier_int(raw_ticker)
            except Exception:
                pass

        return _DEFAULT_TIER_INT
    except Exception:
        return _DEFAULT_TIER_INT


def _resolve_signal_debounce_s() -> float:
    try:
        raw_ms = gate_config_store.get("signal_debounce_ms", 1)
        if raw_ms is not None and raw_ms > 0:
            return float(raw_ms) / 1000.0
    except Exception:
        pass
    return _SIGNAL_DEBOUNCE_S


def _resolve_signal_min_premium() -> float:
    try:
        val = gate_config_store.get("signal_min_premium", 1)
        if val is not None and val > 0:
            return float(val)
    except Exception:
        pass
    return float(_SIGNAL_MIN_PREMIUM)


def _resolve_exclude_indices() -> bool:
    """
    Return True if the exclude_indices gate is active.
    Safe fallback is True (filter ON).
    """
    try:
        val = gate_config_store.get("exclude_indices", 1)
        return bool(val >= 0.5)
    except Exception:
        return True


def _is_market_hours() -> bool:
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:
        return False
    return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE


def _backoff(attempt: int) -> float:
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt))
    return random.uniform(0, delay)


async def _get_session_token() -> Optional[str]:
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/events/session"
    headers = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept": "application/json",
    }

    # SEM-STREAM: acquire a semaphore slot before fetching to prevent burst.
    acquired = False
    timeout_s = _SESSION_RETRY_DELAY * _SESSION_RETRY_MAX
    try:
        acquired = await acquire_session_token_slot(timeout_s=timeout_s)
    except Exception:
        pass

    try:
        for attempt in range(_SESSION_RETRY_MAX):
            try:
                async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT) as client:
                    resp = await client.post(url, headers=headers, data={})

                if resp.status_code == 401:
                    log.error(
                        f"Tradier session 401 — TRADIER_API_KEY rejected. "
                        f"Verify the key in Railway env vars. (attempt {attempt + 1}/{_SESSION_RETRY_MAX})"
                    )
                    return None

                resp.raise_for_status()
                token = resp.json().get("stream", {}).get("sessionid")
                if token:
                    log.info("Tradier session token obtained successfully")
                    return token
                log.warning(f"Tradier session response missing sessionid field: {resp.text[:200]}")
                return None

            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                log.warning(
                    f"Tradier session fetch failed (transient, attempt {attempt + 1}/{_SESSION_RETRY_MAX}): {e}"
                )
                if attempt < _SESSION_RETRY_MAX - 1:
                    await asyncio.sleep(_SESSION_RETRY_DELAY)
            except Exception as e:
                log.error(f"Tradier session fetch unexpected error: {e}")
                return None

        log.error(f"Tradier session token could not be obtained after {_SESSION_RETRY_MAX} attempts")
        return None
    finally:
        if acquired:
            _tradier_session_sem.release()


async def stream_options_flow(
    symbols: list[str],
    registry=None,
):
    global _order_side_startup_logged

    _stats["active_symbols"] = len(symbols)
    _stats["mode"] = "starting"

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


def _evict_sweep_dispatch(now: float) -> None:
    cutoff = now - _SWEEP_DISPATCH_TTL_S
    to_delete = [k for k, ts in _sweep_upgrade_dispatched.items() if ts < cutoff]
    for k in to_delete:
        del _sweep_upgrade_dispatched[k]


def _evict_lookback_result_cache(now: float) -> None:
    cutoff = now - _LBC_TTL_S
    to_delete = [k for k, (_, ts) in _lookback_result_cache.items() if ts < cutoff]
    for k in to_delete:
        del _lookback_result_cache[k]


def _should_emit_signal(emit_key: str, now: float, current_premium: float) -> bool:
    debounce_s = _resolve_signal_debounce_s()
    last = _signal_last_emit.get(emit_key)
    if last is None:
        return True
    elapsed = now - last["ts"]
    if elapsed > _SIGNAL_EMIT_TTL_S:
        return True
    if elapsed < debounce_s:
        return False
    delta_prem = abs(current_premium - last["premium"])
    delta_pct  = delta_prem / max(last["premium"], 1)
    return delta_prem >= _SIGNAL_DELTA_PREM or delta_pct >= _SIGNAL_DELTA_PCT


async def _process_trade(raw: dict) -> None:
    """Hot-path trade processor — called for every timesale tick from Tradier."""
    global _last_gate_epoch

    _stats["ticks"] += 1
    now = _time.time()
    _stats["last_tick_at"] = now

    # --- ING-010: epoch-change detection (O(1) int compare) ---
    current_epoch = gate_config_store.epoch
    if current_epoch != _last_gate_epoch:
        log.info(
            "[gate-config] epoch changed %d -> %d — gate config hot-reloaded",
            _last_gate_epoch, current_epoch,
        )
        _last_gate_epoch = current_epoch
        _stats["gate_epoch"] = current_epoch

    # --- Gate 1: event type filter ---
    etype = raw.get("type", "")
    if etype not in _PROCESSABLE_TYPES:
        if etype not in _non_timesale_etypes_seen:
            if len(_non_timesale_etypes_seen) < _FIRST_ETYPE_LOG_COUNT:
                log.info("[gate1] non-timesale etype=%r (suppressing future duplicates)", etype)
            _non_timesale_etypes_seen.add(etype)
        return

    # --- Extract raw ticker (pre-parse, for pre-gate checks) ---
    _raw_ticker = raw.get("symbol", "").split(" ")[0].upper()

    # --- Gate 6 (GATE-001): ETF noise exclusion — two layers ---
    # Layer 1 (hardcoded — cannot be disabled by config drift):
    if is_etf_noise_symbol(_raw_ticker):
        _stats["index_filtered"] += 1
        return
    # Layer 2 (config-driven — defense-in-depth):
    if _resolve_exclude_indices() and _raw_ticker in _INDEX_SYMBOLS:
        _stats["index_filtered"] += 1
        return

    # --- Gate 2: market hours ---
    if not _is_market_hours():
        return

    # --- FIX-2: Resolve tier_int using symbol_tier (structural) ---
    # _resolve_min_premium and _resolve_tier_int both prefer reg.symbol_tier()
    # over reg.influence_tier_int() — see FIX-2 docstring in each resolver.
    _min_premium = _resolve_min_premium(_raw_ticker)
    _ev_tier_int = _resolve_tier_int(_raw_ticker)

    # --- First-tick INFO logging ---
    if _stats["ticks"] <= _FIRST_TICK_LOG_COUNT:
        log.info(
            "[first-tick #%d] ticker=%s tier=%d min_premium=%d",
            _stats["ticks"], _raw_ticker, _ev_tier_int, _min_premium,
        )

    # --- Gate 3: parse trade ---
    result = parse_tradier_trade(raw, min_premium=_min_premium)
    if result is None:
        _stats["parse_failed"] += 1
        return
    if result == "below_premium":
        return

    ev = result
    _stats["parsed"] += 1

    # --- REARCH-002: IngestionProcessor gates (DTE, OI, premium) ---
    ev = _ingestion_processor.process(ev, tier=_ev_tier_int)
    if ev is None:
        return

    # --- Gate 4: direction classification ---
    # DIRECTION-KWARG fix: order_side_to_direction(order_side, contract_type) only.
    # price/bid/ask are is_directionally_aggressive() params, not this function's.
    ev.direction = order_side_to_direction(
        order_side=ev.order_side,
        contract_type=ev.contract_type,
    )
    if not is_directionally_aggressive(ev):
        return
    _stats["classified"] += 1

    # --- Gate 5: dedup ---
    if flow_dedup.is_duplicate(ev, tier_int=_ev_tier_int):
        _stats["deduped"] += 1
        return

    # --- Accumulator ---
    ep = accumulator.add_trade(ev)
    if ep is None:
        _stats["accumulator_gated"] += 1
        return

    # --- Persist event (fire-and-forget) ---
    t = asyncio.create_task(persist_flow_event(ev))
    t.add_done_callback(_persist_done_cb)
    _stats["persisted"] += 1
    log.info(
        "[persist] queued occ=%s premium=%.0f tier=%d",
        ev.occ_symbol, ev.premium, _ev_tier_int,
    )

    # --- Lookback enrichment ---
    _evict_lookback_result_cache(now)
    lbc_key = ev.occ_symbol
    cached = _lookback_result_cache.get(lbc_key)
    if cached is None:
        lbc_val = _lbc_fresh(_lbc, _ContractKey(ev.occ_symbol, ev.expiry_date))
        _lookback_result_cache[lbc_key] = (lbc_val, now)
    else:
        lbc_val, _ = cached

    if not lbc_val:
        enqueue_lookback(ev)

    # --- Sweep-upgrade dispatch (H4 TTL eviction) ---
    _evict_sweep_dispatch(now)
    dispatch_key = f"{ev.occ_symbol}|{ev.size}|{ev.fill_price}"
    if dispatch_key not in _sweep_upgrade_dispatched:
        if ev.is_sweep:
            asyncio.create_task(upgrade_to_sweep_in_db(ev.occ_symbol))
            _sweep_upgrade_dispatched[dispatch_key] = now

    # --- Signal gate ---
    persist_ep = accumulator.get_episode(ev)
    if persist_ep is not None:
        asyncio.create_task(persist_flow_episode(persist_ep))

    sig_ep = accumulator.get_signal(ev)
    if sig_ep is None:
        return

    signal_min_premium = _resolve_signal_min_premium()
    if sig_ep.total_premium < signal_min_premium:
        return
    if sig_ep.trade_count < _SIGNAL_MIN_TRADES:
        return

    emit_key = f"{ev.occ_symbol}|{ev.expiry_date}"
    if not _should_emit_signal(emit_key, now, sig_ep.total_premium):
        _stats["sig_debounced"] += 1
        return

    _signal_last_emit[emit_key] = {"ts": now, "premium": sig_ep.total_premium}
    _stats["signals"] += 1

    alert_level = accumulator.get_alert_level(sig_ep)
    direction   = sig_ep.dominant_direction

    # --- Composite score ---
    composite = None
    try:
        composite = build_composite(sig_ep, accumulator)
    except Exception as e:
        _stats["composite_errors"] += 1
        log.warning("[composite] build failed: %s", e)

    if composite is None:
        return

    payload = {
        "type": "signal",
        "data": {
            "ticker":          ev.ticker,
            "contract_type":   ev.contract_type,
            "strike":          ev.strike,
            "expiry":          str(ev.expiry_date),
            "alert_level":     alert_level,
            "direction":       direction,
            "total_premium":   sig_ep.total_premium,
            "trade_count":     sig_ep.trade_count,
            "composite_score": round(composite.score / COMPOSITE_SCORE_CEILING, 4),
            "tier":            _ev_tier_int,
        },
    }
    await bus.publish_all("composite_signal", payload)
    log.info(
        "[signal] emitted ticker=%s alert=%s dir=%s premium=%.0f trades=%d composite=%.3f tier=%d",
        ev.ticker, alert_level, direction,
        sig_ep.total_premium, sig_ep.trade_count,
        composite.score / COMPOSITE_SCORE_CEILING,
        _ev_tier_int,
    )
