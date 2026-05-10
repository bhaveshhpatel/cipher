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
# patch("services.tradier_stream._lbc") etc.
from utils.contract_day_cache import (
    _cache as _lbc,
    _is_fresh as _lbc_fresh,
    ContractKey as _ContractKey,
)
# ING-010: tier-aware gate config store singleton.
# The module exports `store`; aliased here so all internal references remain unchanged.
from services.gate_config_store import store as gate_config_store

log = logging.getLogger("tradier_stream")

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


def get_stats() -> dict:
    stats = dict(_stats)
    stats["uptime_seconds"] = round(_time.time() - _stream_start_at, 1)
    stats["gate_epoch"] = gate_config_store.epoch  # always current, not cached
    stats.update(get_parser_stats())
    stats.update(flow_dedup.dedup_stats())
    stats.update(get_lookback_stats())
    return stats


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
# Session token
# ---------------------------------------------------------------------------
async def _get_session_token() -> Optional[str]:
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/events/session"
    headers = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept": "application/json",
    }

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
        return True, "initial_crossing"

    if last["alert_level"] != alert_level:
        return True, f"alert_escalation:{last['alert_level']}->{alert_level}"

    debounce_s = _resolve_signal_debounce_s()

    elapsed = now - last["ts"]
    if elapsed >= debounce_s:
        delta      = total_premium - last["premium"]
        pct_floor  = last["premium"] * _SIGNAL_DELTA_PCT
        threshold  = max(_SIGNAL_DELTA_PREM, pct_floor)
        if delta >= threshold:
            return True, f"premium_growth:+${delta:,.0f} (>= ${threshold:,.0f})"

    return False, (
        f"debounced: elapsed={elapsed:.1f}s/{debounce_s:.0f}s "
        f"delta=${total_premium - last['premium']:,.0f} "
        f"alert={alert_level}"
    )


# ---------------------------------------------------------------------------
# Trade processor
# ---------------------------------------------------------------------------
async def _process_trade(raw: dict):
    """
    Process a raw Tradier stream event (filter=timesale).

    ING-010 addition: tier-aware min_premium resolution.
      Before calling parse_tradier_trade(), quick-extract the raw ticker
      from the OCC symbol or underlying field and resolve the tier-aware
      premium floor via _resolve_min_premium(). This is a O(1) dict
      lookup after the registry tier int is known — zero async I/O.
      The resolved floor is passed as min_premium= to parse_tradier_trade().

    ING-010-GATES addition: signal_min_premium resolved live per-tick from
      gate_config_store (T1 canonical row). Fallback: _SIGNAL_MIN_PREMIUM.

    ING-010-DEDUP addition: tier_int threaded into flow_dedup.is_duplicate()
      so DedupCache reads dedup_window_ms per tier. Derived via _resolve_tier_int()
      from the pre-parse _raw_ticker (registry lookup). Not from ev.influence_tier
      which no longer exists on OptionsFlowEvent post REARCH-010/migration 024.

    ING-011 addition: exclude_indices gate.
      After _raw_ticker is extracted (pre-parse), if _resolve_exclude_indices()
      returns True and _raw_ticker is in _INDEX_SYMBOLS, the tick is dropped
      immediately. No parse cost is incurred. _stats["index_filtered"] is
      incremented and a DEBUG log fires. Gate can be toggled live via the
      PATCH /gates/exclude_indices/1 admin endpoint without stream restart.

    ING-012 (2026-05-10): _resolve_min_premium and _resolve_tier_int now call
      reg.influence_tier_int() directly. influence_tier_string() was deleted.

    EPISODE-GATE-HOIST (2026-05-08): persist_flow_episode fires before signal gate.
      Episode persistence is decoupled from signal emission. persist_flow_episode
      is scheduled as asyncio.create_task() immediately after alert_level and
      direction are resolved — before the signal_min_premium and SIG-DEBOUNCE
      checks. This ensures every accumulator Gate-2 crossing is recorded even
      when the signal_min_premium gate or debounce suppresses bus emission.

    REARCH-010 (2026-05-09): influence_tier, conviction_score, is_golden_sweep
      removed from persist_flow_event() dict and debug log. These columns were
      dropped from options_flow_events in migration 024.

    C008 fix (2026-05-05): decouple persist gate from signal gate.
    PBE-BLOCKING-1 fix (2026-05-06): persist_flow_episode is fire-and-forget.
    """
    global _last_gate_epoch

    _stats["ticks"] += 1
    tick_n = _stats["ticks"]

    # ING-010: detect gate config hot-reload — log once per epoch change.
    current_epoch = gate_config_store.epoch
    if current_epoch != _last_gate_epoch:
        if _last_gate_epoch >= 0:
            log.info(
                "[ING-010] gate_config_store epoch changed %d -> %d — "
                "tier-aware floors updated on hot path",
                _last_gate_epoch, current_epoch,
            )
        _last_gate_epoch = current_epoch
        _stats["gate_epoch"] = current_epoch

    if tick_n <= _FIRST_TICK_LOG_COUNT:
        log.info(
            "[stream] FIRST-TICK #%d raw=%r",
            tick_n, {k: v for k, v in raw.items() if k != "data"},
        )

    if tick_n % _STATS_LOG_INTERVAL == 0:
        _parser_stats = get_parser_stats()
        log.info(
            "[flow-funnel] ticks=%d parsed=%d parse_failed=%d below_min_premium=%d "
            "index_filtered=%d deduped=%d classified=%d accumulator_gated=%d "
            "persisted=%d signals=%d sig_debounced=%d gate_epoch=%d",
            tick_n,
            _stats["parsed"],
            _stats["parse_failed"],
            _parser_stats["below_min_premium"],
            _stats["index_filtered"],
            _stats["deduped"],
            _stats["classified"],
            _stats["accumulator_gated"],
            _stats["persisted"],
            _stats["signals"],
            _stats["sig_debounced"],
            current_epoch,
        )

    event_type = raw.get("type", "")

    if event_type not in _PROCESSABLE_TYPES:
        if event_type not in _non_timesale_etypes_seen and len(_non_timesale_etypes_seen) < _FIRST_ETYPE_LOG_COUNT:
            _non_timesale_etypes_seen.add(event_type)
            log.info("[flow] non-timesale event_type=%r (tick #%d) — skipping", event_type, tick_n)
        else:
            log.debug("[flow] non-timesale event_type=%r — skipping", event_type)
        return

    if event_type in raw:
        trade_payload = raw[event_type]
        if not isinstance(trade_payload, dict):
            return
    else:
        trade_payload = raw

    # ING-010: resolve tier-aware premium floor before parse.
    _raw_ticker = (
        trade_payload.get("underlying")
        or trade_payload.get("symbol", "").split()[0]
        or ""
    )

    # ING-011: exclude_indices gate — drop index ETF flow before parse.
    if _raw_ticker and _raw_ticker.upper() in _INDEX_SYMBOLS and _resolve_exclude_indices():
        _stats["index_filtered"] += 1
        log.debug(
            "[ING-011] index_filtered ticker=%s tick=%d (total_filtered=%d)",
            _raw_ticker, tick_n, _stats["index_filtered"],
        )
        return

    _tier_min_premium = _resolve_min_premium(_raw_ticker) if _raw_ticker else None

    # REARCH-010: derive tier_int from pre-parse registry lookup.
    # ev.influence_tier is no longer available (dropped in migration 024).
    _ev_tier_int: int = _resolve_tier_int(_raw_ticker) if _raw_ticker else _DEFAULT_TIER_INT

    result = parse_tradier_trade(trade_payload, min_premium=_tier_min_premium)
    if result == "below_premium":
        return
    if result is None:
        _stats["parse_failed"] += 1
        log.info(
            "[flow] parse_tradier_trade returned None for symbol=%r "
            "(size=%s bid=%s ask=%s last=%s) — tick dropped",
            trade_payload.get("symbol"),
            trade_payload.get("size"),
            trade_payload.get("bid"),
            trade_payload.get("ask"),
            trade_payload.get("last"),
        )
        return
    ev = result

    _stats["parsed"] += 1

    occ_symbol = trade_payload.get("symbol", "")
    exchange   = trade_payload.get("exch") or trade_payload.get("exchange", "")
    arrival_ts = _time.time()

    if flow_dedup.is_duplicate(
        occ_symbol,
        size=ev.size,
        fill=ev.fill_price,
        exchange=exchange,
        ts=arrival_ts,
        tier_int=_ev_tier_int,
    ):
        _stats["deduped"] += 1
        log.info(
            "[dedup] dropped duplicate #%d: %s size=%d fill=%.2f exch=%s tier=%d",
            _stats["deduped"], occ_symbol, ev.size, ev.fill_price, exchange, _ev_tier_int,
        )

        exch_count = flow_dedup.get_exchange_count(occ_symbol, ev.size, ev.fill_price)
        if exch_count == flow_dedup._sweep_min:
            dispatch_key = f"{occ_symbol}|{ev.size}|{ev.fill_price:.2f}"

            now = _time.time()
            stale_keys = [
                k for k, ts in _sweep_upgrade_dispatched.items()
                if now - ts > _SWEEP_DISPATCH_TTL_S
            ]
            for k in stale_keys:
                del _sweep_upgrade_dispatched[k]

            if dispatch_key not in _sweep_upgrade_dispatched:
                _sweep_upgrade_dispatched[dispatch_key] = now
                log.info(
                    f"[sweep] threshold just crossed — retroactive upgrade: "
                    f"{occ_symbol} size={ev.size} fill={ev.fill_price} "
                    f"exchanges={exch_count}"
                )
                asyncio.create_task(
                    upgrade_to_sweep_in_db(
                        occ_symbol=occ_symbol,
                        fill_price=ev.fill_price,
                        size=ev.size,
                    )
                )

        return

    if flow_dedup.is_sweep(occ_symbol, ev.size, ev.fill_price):
        real_exch_count = flow_dedup.get_exchange_count(occ_symbol, ev.size, ev.fill_price)
        if ev.trade_type != "SWEEP":
            ev.trade_type = "SWEEP"
        ev.exchange_count = real_exch_count

    _stats["classified"] += 1
    _stats["last_tick_at"] = _time.time()

    # REARCH-010: conviction_score and influence_tier removed from OptionsFlowEvent
    # (columns dropped in migration 024). Log only fields that still exist on the model.
    log.debug(
        f"[flow] {ev.ticker} {ev.contract_type} "
        f"${ev.strike:.2f} {ev.expiry} dte={ev.dte} "
        f"| fill={ev.fill_price} bid={ev.bid} ask={ev.ask} size={ev.size} "
        f"| prem=${ev.premium:,.0f} "
        f"| ba={ev.bid_ask_class} aggressive={ev.is_aggressive} "
        f"| type={ev.trade_type} exch={exchange} exch_count={ev.exchange_count} "
        f"| sentiment={ev.sentiment} "
        f"| occ={occ_symbol} "
        f"| synthetic_quote={ev.is_synthetic_quote}"
    )

    persist_ep = await accumulator.ingest_tick(ev)
    sig_ep     = await accumulator.get_signal(ev)

    if not persist_ep:
        _stats["accumulator_gated"] += 1
        log.info(
            "[accumulator] gated %s %s $%.0f dte=%d prem=$%.0f "
            "(below DTE-adjusted premium floor)",
            ev.ticker, ev.contract_type, ev.strike, ev.dte, ev.premium,
        )
        return

    _order_side = getattr(ev, "order_side", None) or "UNKNOWN"

    _contract_key = _ContractKey(ev.ticker, ev.contract_type, ev.strike, ev.expiry)
    enqueue_lookback(_contract_key)

    emit_key = f"{ev.ticker}|{ev.contract_type}|{ev.strike}|{ev.expiry}"
    _multi_day_min_days: int = getattr(accumulator, "_multi_day_min_days", 2)
    try:
        _lbc_entry = _lbc.get(_contract_key)
        _is_repeat_now: bool = (
            _lbc_entry is not None
            and _lbc_fresh(_lbc_entry)
            and _lbc_entry.prior_days_active >= _multi_day_min_days
        )
    except Exception:
        _is_repeat_now = False

    _now = _time.time()
    if _is_repeat_now:
        _lookback_result_cache[emit_key] = (True, _now)
    elif emit_key not in _lookback_result_cache:
        _lookback_result_cache[emit_key] = (False, _now)
    _is_multi_day_repeat: bool = _lookback_result_cache[emit_key][0]

    _strong_sentiment = is_directionally_aggressive(
        getattr(ev, "bid_ask_class", ""), ev.contract_type
    )

    _execution_mechanic = getattr(ev, "execution_mechanic", None)
    if _execution_mechanic is None:
        log.debug(
            "[composite] execution_mechanic unavailable for %s — defaulting to AMBIGUOUS_LONG",
            occ_symbol,
        )
        _execution_mechanic = "AMBIGUOUS_LONG"

    _stats["persisted"] += 1
    log.info(
        "[persist] %s %s $%.0f %s fill=%.2f size=%d prem=$%.0f type=%s",
        ev.ticker, ev.contract_type, ev.strike, ev.expiry,
        ev.fill_price, ev.size, ev.premium, ev.trade_type,
    )

    try:
        await asyncio.wait_for(
            persist_flow_event({
                "ticker":               ev.ticker,
                "contract_type":        ev.contract_type,
                "strike":               ev.strike,
                "expiry":               ev.expiry,
                "dte":                  ev.dte,
                "fill_price":           ev.fill_price,
                "bid":                  ev.bid,
                "ask":                  ev.ask,
                "size":                 ev.size,
                "premium":              ev.premium,
                "trade_type":           ev.trade_type,
                "bid_ask_class":        ev.bid_ask_class,
                "is_aggressive":        ev.is_aggressive,
                "sentiment":            ev.sentiment,
                "exchange_count":       ev.exchange_count,
                "fill_count":           ev.fill_count,
                "open_interest":        ev.open_interest,
                "iv":                   ev.iv,
                "underlying_price":     ev.underlying_price,
                "occ_symbol":           occ_symbol,
                "is_synthetic_quote":   ev.is_synthetic_quote,
                "order_side":           _order_side,
                "strong_sentiment":     _strong_sentiment,
                "execution_mechanic":   _execution_mechanic,
            }),
            timeout=_PERSIST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        _stats["errors"] += 1
        log.warning(
            f"[stream] persist_flow_event timed out after {_PERSIST_TIMEOUT}s "
            f"for {ev.ticker} — tick dropped. Check Supabase latency."
        )
        return

    if not sig_ep:
        return

    # Resolve alert_level and direction from sig_ep. These are needed by both
    # persist_flow_episode (unconditional) and the signal emission path below.
    alert_level = accumulator.get_alert_level(sig_ep.total_premium)
    direction   = sig_ep.dominant_direction

    ep_summary = (
        f"{sig_ep.ticker} {sig_ep.contract_type} ${sig_ep.strike:.0f} {sig_ep.expiry} "
        f"trades={sig_ep.trade_count} prem=${sig_ep.total_premium:,.0f}"
    )

    # EPISODE-GATE-HOIST: fire persist_flow_episode BEFORE the signal_min_premium
    # gate and SIG-DEBOUNCE check. Episode persistence is decoupled from signal
    # emission — every accumulator Gate-2 crossing must be recorded regardless of
    # whether the signal bus fires. Fixes test_process_trade_episode_direction_put_
    # is_repeat_sell (SPY PUT $60k < $75k signal_min_premium T1 default).
    asyncio.create_task(
        persist_flow_episode({
            "ticker":              sig_ep.ticker,
            "direction":           direction,
            "contract_type":       sig_ep.contract_type,
            "strike":              sig_ep.strike,
            "expiry":              sig_ep.expiry,
            "total_premium":       sig_ep.total_premium,
            "trade_count":         sig_ep.trade_count,
            "alert_level":         alert_level,
            "is_accelerating":     sig_ep.is_accelerating,
            "is_multi_day_repeat": _is_multi_day_repeat,
            "seed_episode":        ep_summary,
            "timestamp":           ev.timestamp.isoformat(),
        })
    )

    # Signal gate: suppress bus emission for low-premium episodes.
    # persist_flow_episode has already fired above — this gate only controls
    # whether downstream consumers (WebSocket, composite engine) are notified.
    _live_signal_min_premium = _resolve_signal_min_premium()

    if sig_ep.trade_count < _SIGNAL_MIN_TRADES or sig_ep.total_premium <= _live_signal_min_premium:
        log.debug(
            "[signal-gate] suppressed %s %s — trades=%d (min=%d) prem=$%.0f (min=$%.0f)",
            sig_ep.ticker, sig_ep.contract_type,
            sig_ep.trade_count, _SIGNAL_MIN_TRADES,
            sig_ep.total_premium, _live_signal_min_premium,
        )
        return

    now_ts = _time.time()
    _evict_signal_emit_cache(now_ts)
    _evict_lookback_result_cache(now_ts)

    should_emit, reason = _should_emit_signal(
        emit_key, alert_level, sig_ep.total_premium, now_ts
    )

    if not should_emit:
        _stats["sig_debounced"] += 1
        log.debug(
            "[signal-debounce] suppressed %s %s $%.0f %s — %s",
            sig_ep.ticker, sig_ep.contract_type, sig_ep.strike, sig_ep.expiry,
            reason,
        )
        return

    _signal_last_emit[emit_key] = {
        "alert_level": alert_level,
        "premium":     sig_ep.total_premium,
        "ts":          now_ts,
    }

    log.info(
        "[signal] %s %s | alert=%s | trades=%d | total_prem=$%.0f "
        "| accel=%s | multi_day=%s | reason=%s | %s",
        sig_ep.ticker, sig_ep.contract_type,
        alert_level,
        sig_ep.trade_count,
        sig_ep.total_premium,
        sig_ep.is_accelerating,
        _is_multi_day_repeat,
        reason,
        ep_summary,
    )

    try:
        composite = build_composite(sig_ep, accumulator)
    except Exception as e:
        _stats["composite_errors"] += 1
        log.error(f"[signal] build_composite failed for {sig_ep.ticker}: {e}")
        composite = None

    signal = {
        "type": "signal",
        "data": {
            "ticker":              sig_ep.ticker,
            "direction":           direction,
            "contract_type":       sig_ep.contract_type,
            "strike":              sig_ep.strike,
            "expiry":              sig_ep.expiry,
            "total_premium":       sig_ep.total_premium,
            "trade_count":         sig_ep.trade_count,
            "alert_level":         alert_level,
            "is_accelerating":     sig_ep.is_accelerating,
            "is_multi_day_repeat": _is_multi_day_repeat,
            "seed_episode":        ep_summary,
            "timestamp":           ev.timestamp.isoformat(),
        },
    }
    _stats["signals"] += 1
    await bus.publish_all(signal)

    if composite is not None:
        composite_msg = {
            "type": "composite_signal",
            "data": {
                "signal": {
                    "ticker":                  composite.ticker,
                    "recommendation":          composite.recommendation,
                    "composite_score":         composite.composite_score,
                    "composite_score_ceiling": COMPOSITE_SCORE_CEILING,
                    "flow_score":              composite.flow_score,
                    "backtest_score":          composite.backtest_score,
                    "volume_premium_factor":   composite.volume_premium_factor,
                    "premium_tier_score":      composite.premium_tier_score,
                    "reasoning":               composite.reasoning,
                    "alert_level":             alert_level,
                    "order_side":              _order_side,
                    "strong_sentiment":        _strong_sentiment,
                    "execution_mechanic":      _execution_mechanic,
                },
                "episode": {
                    "contract_type":   sig_ep.contract_type,
                    "direction":       direction,
                    "influence_tier":  episode_influence_tier(sig_ep),
                    "total_premium":   sig_ep.total_premium,
                    "trade_count":     sig_ep.trade_count,
                    "is_accelerating": sig_ep.is_accelerating,
                    "timestamp":       ev.timestamp.isoformat(),
                },
            },
        }
        await bus.publish_all(composite_msg)


# ---------------------------------------------------------------------------
# Demo mode
# ---------------------------------------------------------------------------
async def _demo_mode_once(symbols: list[str]):
    import datetime as dt
    rng     = random.Random(42)
    tickers = symbols or ["AAPL", "TSLA", "NVDA", "SPY", "QQQ", "MSFT", "AMZN", "META"]
    levels  = ["CONVICTION", "STRONG_SIGNAL", "ALERT", "WATCH"]
    demo_expiry = "2026-06-20"

    log.info("Demo mode active — emitting synthetic signals")
    try:
        while True:
            await asyncio.sleep(rng.uniform(2, 6))
            ticker    = rng.choice(tickers)
            prem      = rng.randint(100_000, 8_000_000)
            ctype     = rng.choice(["CALL", "PUT"])
            strike    = round(rng.uniform(100, 500), 0)
            fill      = round(rng.uniform(1.0, 15.0), 2)
            bid       = round(fill * 0.99, 2)
            ask       = round(fill * 1.01, 2)
            size      = rng.randint(10, 500)
            dte       = rng.randint(1, 60)

            order_side_demo = rng.choices(["BUY", "SELL", "UNKNOWN"], weights=[60, 25, 15])[0]
            direction = order_side_to_direction(order_side_demo, ctype)

            signal = {
                "type": "signal",
                "data": {
                    "ticker":              ticker,
                    "direction":           direction,
                    "contract_type":       ctype,
                    "strike":              strike,
                    "expiry":              demo_expiry,
                    "total_premium":       prem,
                    "trade_count":         rng.randint(3, 25),
                    "alert_level":         rng.choices(levels, weights=[5, 15, 30, 50])[0],
                    "is_accelerating":     rng.random() < 0.2,
                    "is_multi_day_repeat": False,
                    "seed_episode":        f"Demo: {ticker} synthetic flow",
                    "timestamp":           dt.datetime.utcnow().isoformat(),
                },
            }
            _stats["ticks"]      += 1
            _stats["classified"] += 1
            _stats["signals"]    += 1
            _stats["last_tick_at"] = _time.time()
            await bus.publish_all(signal)

            composite_score = round(rng.uniform(0.40, 0.85), 3)
            rec = "BUY"  if composite_score >= 0.65 and ctype == "CALL" else \
                  "SELL" if composite_score >= 0.65 and ctype == "PUT"  else "HOLD"

            composite_msg = {
                "type": "composite_signal",
                "data": {
                    "signal": {
                        "ticker":                  ticker,
                        "recommendation":          rec,
                        "composite_score":         composite_score,
                        "composite_score_ceiling": COMPOSITE_SCORE_CEILING,
                        "flow_score":              round(rng.uniform(0.4, 0.9), 3),
                        "backtest_score":          0.0,
                        "volume_premium_factor":   round(rng.uniform(0.3, 0.8), 3),
                        "premium_tier_score":      round(rng.uniform(0.0, 1.0), 3),
                        "reasoning":               f"Demo synthetic signal for {ticker}",
                        "alert_level":             rng.choices(levels, weights=[5, 15, 30, 50])[0],
                        "order_side":              order_side_demo,
                        "strong_sentiment":        order_side_demo in ("BUY", "SELL"),
                        "execution_mechanic":      "DIRECTIONAL_LONG" if ctype == "CALL" else "DIRECTIONAL_SHORT",
                    },
                    "episode": {
                        "contract_type":   ctype,
                        "direction":       direction,
                        "influence_tier":  rng.choice(["WHALE", "INSTITUTIONAL", "LARGE", "RETAIL"]),
                        "total_premium":   prem,
                        "trade_count":     rng.randint(3, 25),
                        "is_accelerating": rng.random() < 0.2,
                        "timestamp":       dt.datetime.utcnow().isoformat(),
                    },
                },
            }
            await bus.publish_all(composite_msg)

            _ = (bid, ask, size, dte)
    except asyncio.CancelledError:
        log.info("Demo mode cancelled — live stream connection established")
        raise


async def _demo_mode(symbols: list[str]):
    _stats["mode"] = "demo"
    await _demo_mode_once(symbols)
