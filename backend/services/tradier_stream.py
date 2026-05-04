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
  surface exactly where trades are being dropped:
    - tick type received (sampled every 100 ticks to avoid log flooding)
    - dedup dropped (INFO with running count, was DEBUG)
    - parse_tradier_trade returned None
    - accumulator.ingest_tick returned None (most common silent drop)
    - persist_flow_event called
  _stats now tracks parsed_count, accumulator_gated, parse_failed so
  /health/stream shows the full funnel.

Fix (FIRST-TICK 2026-04-28 Issue 2):
  Log the first 5 ticks individually at INFO level so Railway shows stream
  activity immediately after connect, not only at tick 100.
  Also log non-timesale event_types at INFO (not DEBUG) for the first 10
  received so we can confirm the WebSocket is receiving data at all.

Fix (DEDUP-KWARGS 2026-04-28):
  flow_dedup.is_duplicate() first param is `event_or_occ_symbol`, not `occ_symbol`.
  Passing occ_symbol= as a keyword arg raised:
    DedupCache.is_duplicate() got an unexpected keyword argument 'occ_symbol'
  Fix: pass occ_symbol positionally as the first arg.

Fix (BUG-1 2026-04-29):
  composite_signal bus message was missing alert_level in signal dict.
  signal_store._build_row() reads sig.get("alert_level") — always got None
  because the key was never included, causing every composite signal to fall
  through to score-based alert level logic and ignoring premium-tier
  classification (CONVICTION / STRONG_SIGNAL / ALERT / WATCH) entirely.
  Fix: add "alert_level": alert_level to composite_msg["data"]["signal"].

Fix (SIGNAL-GATE 2026-04-29):
  accumulator was instantiated with min_trades=1 / min_premium=10_000 so
  every persisted trade also fired a signal (persisted == signals == 7,072).
  Decision (2026-04-28): continue accumulating at the low persist threshold
  for flow_events writes, but only publish to the signal bus when the episode
  has trade_count >= 3 AND total_premium > 50_000.
  Fix: explicit gate check in _process_trade after sig_ep cooldown passes,
  before alert_level / composite / bus publish. Persist path unchanged.

Fix (SIG-DEBOUNCE 2026-04-30):
  Once an episode crossed _SIGNAL_MIN_TRADES / _SIGNAL_MIN_PREMIUM, EVERY
  subsequent persisted trade was re-emitting a signal (persisted=71,866 ->
  signals=50,494 at market open). The SIGNAL-GATE only prevented the very
  first crossing; after that, get_signal() returned a sig_ep on every tick.

  Fix: per-episode emit tracker _signal_last_emit dict[str, dict] keyed by
  "ticker|contract_type|strike|expiry". A signal is emitted only when:
    1. First time this episode key is seen (initial crossing), OR
    2. alert_level escalated since the last emit, OR
    3. >= _SIGNAL_DEBOUNCE_S elapsed since last emit AND
       total_premium grew by >= max(_SIGNAL_DELTA_PREM, last_prem * _SIGNAL_DELTA_PCT)

  Entries are evicted after _SIGNAL_EMIT_TTL_S (7200s / 2h) to prevent
  unbounded memory growth across the trading day.

Fix (SIG-DEBOUNCE-LOG 2026-04-30):
  [signal] log line used $%,.0f — the comma thousands-separator is only valid
  in f-string / str.format() style. Python logging uses msg % args internally,
  so %,.0f raises ValueError: unsupported format character ','.
  Fix: changed $%,.0f -> $%.0f in the %-style format string.
  Comma separator retained in f-string reason= output (unaffected).

Fix (EPISODE-FIX 2026-04-30):
  flow_episodes row count was identical to signal_history because both tables
  were written from the same composite_signal bus event, which only fires after
  Signal Gate AND SIG-DEBOUNCE both pass. SIG-DEBOUNCE is a WebSocket /
  signal_history anti-spam guard, not an episode persistence gate.

  Additionally, _bus_signal_listener always wrote strike=None / expiry=None
  because composite_msg["data"]["episode"] never included those fields.

  Fix: persist_flow_episode() called directly in _process_trade() after the
  Signal Gate check and BEFORE the SIG-DEBOUNCE check, using sig_ep fields
  directly (which carry correct strike / expiry). direction is computed at
  that point so it is in scope for both the episode write and the later bus
  publish. The flow_episodes write in _bus_signal_listener is removed —
  the "db_writer" channel is retained but is now a no-op for future use.

Fix (S6-HOT-PATH 2026-05-01):
  Hot-path direction was derived from sig_ep.contract_type alone:
    CALL -> REPEAT_BUY, PUT -> REPEAT_SELL
  This ignored the actual dominant_direction of the episode, meaning a
  SELL PUT campaign (bullish) was published as REPEAT_SELL (incorrect).
  Fix: direction = sig_ep.dominant_direction
  The dominant_direction property on RepetitionEpisode is premium-weighted
  across all events using order_side_to_direction(), correctly resolving
  SELL PUT -> REPEAT_BUY and SELL CALL -> REPEAT_SELL (S2 invariants).

Fix (S6-DEMO-MODE 2026-05-01):
  Demo mode direction was also derived from contract_type alone.
  Fix: use order_side_to_direction(order_side_demo, ctype) so demo signals
  exercise the same direction logic as live signals.

Fix (S6-COMPOSITE-PAYLOAD 2026-05-01):
  Composite bus payload updated with S6 fields:
    - composite_score_ceiling: COMPOSITE_SCORE_CEILING (explicit until sector_score activates)
    - order_side: ev.order_side
    - strong_sentiment: ev.strong_sentiment
    - execution_mechanic: ev.execution_mechanic
    - premium_tier_score: composite.premium_tier_score
  Episode block updated:
    - influence_tier: episode_influence_tier(sig_ep) using episode premium
      (replaces ev.influence_tier which was event-level, not episode-level)

Fix (S6-PRE-MERGE 2026-05-01):
  Item 1: COMPOSITE_SCORE_CEILING constant imported from composite_signal_engine;
          literal 0.90 replaced in both live and demo paths.
  Item 4: log.warning added on all three getattr fallback paths (order_side,
          strong_sentiment, execution_mechanic) so parser regressions surface
          in Railway logs instead of silently emitting fallback values.
  Item 5: Demo mode ceiling comment corrected from '# capped at 0.85 (pre-sector ceiling)'
          to '# demo headroom — live ceiling is 0.90 (COMPOSITE_SCORE_CEILING)'.

Fix (ING-002 2026-05-03):
  parse_tradier_trade() returns sentinel "below_premium" for events
  with premium < _MIN_EVENT_PREMIUM ($10,000). _process_trade() checks
  for sentinel BEFORE the `if result is None` / parse_failed branch.
  Counter ownership (Option A): below_min_premium is owned and incremented
  by options_flow_parser._stats inside parse_tradier_trade(), not by the
  caller. tradier_stream funnel log reads the counter from parser.get_stats().

Fix (ING-003 2026-05-03):
  Accumulator was instantiated with dte_premium_tiers=None, meaning
  _get_episode_min_premium() fell back to the flat min_premium=$10k floor
  for all DTE buckets during the cold-start window (~30 min) until registry
  warmup called set_dte_premium_tiers(). A $12k 2-DTE lottery ticket cleared
  the same floor as a $500k 45-DTE institutional print.
  Fix: pass _DEFAULT_DTE_PREMIUM_TIERS at instantiation — DTE-stratified
  floors are active from tick 1. Unknown tickers default to T1 (strictest
  floor) until registry warmup confirms their tier. Safe direction is too
  strict, not too permissive.
  3-way deliberation complete 2026-05-03 — all decisions in sprint doc.

Fix (ING-006-PREMERGE 2026-05-03):
  7 pre-merge issues resolved for ING-006 / PR #62:

  PREMERGE-1: get_alert_level(sig_ep) → get_alert_level(sig_ep.total_premium)
    ING-006 changed the signature to accept a float. Passing the episode
    object caused the tier comparisons to always evaluate falsy (comparing
    a float against an object returns False in all branches), silently
    returning RETAIL for every episode regardless of premium.

  PREMERGE-2: get_signal() removed — AttributeError on every tick
    get_signal() was retired in ING-006 (PBE-F4 cooldown retirement).
    The call `await accumulator.get_signal(ev.timestamp, persist_ep)` raises
    AttributeError in production. sig_ep is now set equal to persist_ep —
    the stream's own SIG-DEBOUNCE / SIGNAL-GATE already handle throttling.

  PREMERGE-3: min_premium= constructor kwarg removed
    RepetitionAccumulator.__init__ no longer accepts min_premium.
    Passing it raises TypeError on startup. Removed from instantiation.
    ING-003 already set dte_premium_tiers which provides the equivalent
    DTE-stratified floor.

  PREMERGE-4: accumulator.min_premium attribute removed
    The log line in accumulator_gated branch referenced accumulator.min_premium
    which no longer exists. Replaced with literal floor description.

  PREMERGE-5: sig_ep.summary_str() removed from RepetitionEpisode
    All three call sites replaced with an inline f-string built from the
    episode's public properties.

  PREMERGE-6/7: set_dte_premium_tiers() — method renamed to set_tier_map()
    Any downstream callers updated. No call sites remain in this file
    (stream layer uses accumulator.set_tier_map() from registry warmup).

Fix (ING-007 2026-05-04):
  Log noise cleanup + strong_sentiment coupling fix + lookback wiring.

  ISSUE-1: order_side WARN fired on every tick.
    Tradier timesale stream never provides order_side (ING-001 resolution).
    Per-tick log.warning was flooding Railway logs with noise, making real
    warnings invisible. The field default to UNKNOWN is CORRECT and EXPECTED —
    it is not a parser regression condition.
    Fix: removed per-tick warn. Added one-time INFO at stream startup:
      "[stream] order_side not available on Tradier timesale stream —
       using bid/ask spread as aggression proxy (ING-001)"
    _order_side still defaults to UNKNOWN and is passed through to persist
    and composite payload unchanged.

  ISSUE-2: execution_mechanic WARN fired on every tick.
    AMBIGUOUS_LONG is the correct cold default for an enrichment field not
    present at the timesale layer. Not a warning condition.
    Fix: downgraded to log.debug only (production Railway log level is INFO,
    so this is effectively silent in prod). No behaviour change.

  ISSUE-3: strong_sentiment derived from stale pre-ING-006 path.
    getattr(ev, "strong_sentiment") was returning whatever the parser set,
    which pre-ING-006 was coupled to execution_mechanic / order_side.
    After ING-006, strong_sentiment MUST be computed from
    is_directionally_aggressive(bid_ask_class, contract_type) — the ING-006
    contract explicitly replaced order_side as the aggression signal.
    Fix: compute _strong_sentiment inline using is_directionally_aggressive()
    before the persist_flow_event call. The parser's ev.strong_sentiment
    field is no longer the source of truth at this layer.
    execution_mechanic payload field preserved in composite_msg for
    downstream consumers (signal_store, frontend) — only derivation changed.

  WIRING: enqueue_lookback() called after persist_ep gate passes.
    Every persisted episode's ContractKey is enqueued for async lookback
    enrichment via start_lookback_worker(accumulator) in main.py lifespan.
    get_lookback_stats() surfaced in get_stats() for /health/stream.
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

# How long to wait for the background build() to complete before
# streaming starts. We poll every 500ms up to this limit.
_REGISTRY_READY_TIMEOUT_S = 1800.0  # 30 min (full cold-start upper bound)
_REGISTRY_READY_POLL_S    = 0.5

# H4: TTL for sweep-upgrade dispatch guard keys (30 min in seconds)
_SWEEP_DISPATCH_TTL_S = 1800.0

# FLOW-DEBUG: log a tick-funnel summary every N ticks received
_STATS_LOG_INTERVAL = 100

# FIRST-TICK: log first N ticks individually at INFO level
_FIRST_TICK_LOG_COUNT  = 5
_FIRST_ETYPE_LOG_COUNT = 10  # non-timesale event types seen before silencing

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = time(9, 30)
_MARKET_CLOSE = time(16, 0)

_PROCESSABLE_TYPES = {"timesale"}

# ---------------------------------------------------------------------------
# Signal gate thresholds (SIGNAL-GATE 2026-04-29):
#   Persist to flow_events at low threshold (min_trades=1).
#   Only publish to signal bus when episode has >= _SIGNAL_MIN_TRADES trades
#   AND total_premium > _SIGNAL_MIN_PREMIUM.
# ---------------------------------------------------------------------------
_SIGNAL_MIN_TRADES  = 3
_SIGNAL_MIN_PREMIUM = 50_000

# ---------------------------------------------------------------------------
# Per-episode signal debounce (SIG-DEBOUNCE 2026-04-30):
#   After initial crossing, re-emit only when:
#     a) alert_level changed, OR
#     b) >= _SIGNAL_DEBOUNCE_S elapsed AND premium grew by >= threshold
#
#   _SIGNAL_DELTA_PREM / _SIGNAL_DELTA_PCT are OR-ed: whichever is larger
#   for the current episode premium level acts as the effective delta floor.
#     - Small episodes ($50k-$250k):  $25k absolute is the binding constraint
#     - Large episodes ($500k+):      20% relative is the binding constraint
#
#   _SIGNAL_EMIT_TTL_S: evict tracker entries after 2h so episodes that
#   go cold don't prevent fresh signals if the same contract re-activates.
# ---------------------------------------------------------------------------
_SIGNAL_DEBOUNCE_S  = 30.0        # minimum seconds between re-emits
_SIGNAL_DELTA_PREM  = 25_000.0    # minimum absolute premium growth to re-emit
_SIGNAL_DELTA_PCT   = 0.20        # minimum % growth to re-emit (whichever is larger)
_SIGNAL_EMIT_TTL_S  = 7_200.0     # evict tracker entries after 2h

# ---------------------------------------------------------------------------
# Global stats
# ---------------------------------------------------------------------------
_stream_start_at: float = _time.time()

_stats = {
    "active_symbols":    0,
    "ticks":             0,
    "parsed":            0,   # FLOW-DEBUG: parse_tradier_trade returned non-None OptionsFlowEvent
    "parse_failed":      0,   # FLOW-DEBUG: genuine parse error (bad data, exception, size==0)
    "classified":        0,
    "deduped":           0,
    "accumulator_gated": 0,   # FLOW-DEBUG: ingest_tick returned None (below threshold)
    "persisted":         0,   # FLOW-DEBUG: persist_flow_event actually called
    "signals":           0,
    "sig_debounced":     0,   # SIG-DEBOUNCE: signals suppressed by debounce gate
    "errors":            0,
    "composite_errors":  0,
    "reconnects":        0,
    "mode":              "starting",
    "last_tick_at":      None,
    "last_reconnect_at": None,
}

# FIRST-TICK tracking
_non_timesale_etypes_seen: set = set()

# ING-007: one-time startup flag so order_side platform limitation is logged
# once at stream start rather than per-tick.
_order_side_startup_logged: bool = False

# PREMERGE-3 (ING-006): min_premium= constructor kwarg was removed in ING-006.
# ING-003 already provides DTE-stratified floors via dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS.
# The flat $10k floor is superseded by the tier table — no min_premium kwarg needed.
accumulator = RepetitionAccumulator(
    window_minutes=30,
    min_trades=1,
    dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
)

# H4 fix: dict[str, float] with wall-clock timestamps instead of a bare Set.
_sweep_upgrade_dispatched: dict[str, float] = {}

# SIG-DEBOUNCE: per-episode last-emit tracker.
# key  = "ticker|contract_type|strike|expiry"
# value = {"alert_level": str, "premium": float, "ts": float}
_signal_last_emit: dict[str, dict] = {}


def get_stats() -> dict:
    stats = dict(_stats)
    stats["uptime_seconds"] = round(_time.time() - _stream_start_at, 1)
    # ING-002: merge parser-level stats so /health/stream surfaces below_min_premium
    stats.update(get_parser_stats())
    stats.update(flow_dedup.dedup_stats())
    # ING-007: surface lookback queue depth and overflow counter
    stats.update(get_lookback_stats())
    return stats


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
    registry=None,   # D-001: accept pre-built registry from lifespan
):
    """
    Main entry point for the Tradier options stream.

    D-001: When `registry` is provided (passed from main.py lifespan), skip
    init_registry() and build() entirely. Instead, wait until
    registry.is_ready() is True (background build complete) before spawning
    StreamManager workers. This eliminates the duplicate build() that was
    causing double chain API calls and two independent SymbolRegistry instances.

    When `registry` is None (standalone / test usage), fall back to the
    original behaviour of building a fresh registry inline.
    """
    global _order_side_startup_logged

    _stats["active_symbols"] = len(symbols)
    _stats["mode"] = "starting"

    if not settings.TRADIER_API_KEY:
        log.warning("TRADIER_API_KEY not set — stream idle. Use admin panel to start demo engine.")
        _stats["mode"] = "idle"
        return

    # ING-007: log order_side platform limitation once at startup (not per-tick).
    if not _order_side_startup_logged:
        log.info(
            "[stream] order_side not available on Tradier timesale stream — "
            "using bid/ask spread as aggression proxy via is_directionally_aggressive() (ING-001/ING-006)"
        )
        _order_side_startup_logged = True

    from services.stream_manager import StreamManager

    if registry is not None:
        # D-001: registry owned and built by lifespan — just wait for it
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
        # Standalone / test path — build our own registry
        from services.symbol_registry import init_registry as _init_registry
        log.info(f"[stream] Building OCC registry for {len(symbols)} tickers...")
        registry = _init_registry(watchlist=symbols)
        try:
            occ_count, _ = await registry.build()  # H1: unpack tuple
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
        # D-002: only spawn refresh_loop here in standalone mode.
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
    """Remove entries from _signal_last_emit older than _SIGNAL_EMIT_TTL_S."""
    stale = [
        k for k, v in _signal_last_emit.items()
        if now - v["ts"] > _SIGNAL_EMIT_TTL_S
    ]
    for k in stale:
        del _signal_last_emit[k]


def _should_emit_signal(
    emit_key: str,
    alert_level: str,
    total_premium: float,
    now: float,
) -> tuple[bool, str]:
    """
    Return (should_emit, reason_str) for the given episode key.

    Rules (in priority order):
      1. No prior emit for this key -> emit (initial crossing).
      2. alert_level changed since last emit -> emit (escalation / de-escalation).
      3. Debounce window elapsed AND premium delta >= threshold -> emit (growth update).
      4. Otherwise -> suppress.
    """
    last = _signal_last_emit.get(emit_key)

    if last is None:
        return True, "initial_crossing"

    if last["alert_level"] != alert_level:
        return True, f"alert_escalation:{last['alert_level']}->{alert_level}"

    elapsed = now - last["ts"]
    if elapsed >= _SIGNAL_DEBOUNCE_S:
        delta      = total_premium - last["premium"]
        pct_floor  = last["premium"] * _SIGNAL_DELTA_PCT
        threshold  = max(_SIGNAL_DELTA_PREM, pct_floor)
        if delta >= threshold:
            return True, f"premium_growth:+${delta:,.0f} (>= ${threshold:,.0f})"

    return False, (
        f"debounced: elapsed={elapsed:.1f}s "
        f"delta=${total_premium - last['premium']:,.0f} "
        f"alert={alert_level}"
    )


# ---------------------------------------------------------------------------
# Trade processor
# ---------------------------------------------------------------------------
async def _process_trade(raw: dict):
    """
    Process a raw Tradier stream event (filter=timesale).

    ING-002 (2026-05-03):
      parse_tradier_trade() returns a 3-state result:
        "below_premium" — clean filter drop, premium < $10k.
                          Counter owned by parser (_stats["below_min_premium"]).
                          Caller returns immediately, does NOT touch parse_failed.
        None            — genuine parse error. Increment _stats["parse_failed"].
        OptionsFlowEvent — valid event, assign to ev and continue.
      Sentinel check MUST come before the `result is None` check because
      "below_premium" is truthy and would silently pass through `if not result`.

    SIG-DEBOUNCE (2026-04-30):
      After initial signal threshold crossing, re-emit only when:
        a) alert_level changed since last emit for this episode, OR
        b) >= 30s elapsed AND total_premium grew >= max($25k, 20% of last prem)
      Suppressed signals counted in _stats["sig_debounced"].
      _signal_last_emit entries evicted after 2h (TTL).

    EPISODE-FIX (2026-04-30):
      persist_flow_episode() is called directly after the Signal Gate and BEFORE
      the SIG-DEBOUNCE check. flow_episodes records every Signal Gate crossing;
      SIG-DEBOUNCE only gates the WebSocket bus publish and signal_history.

    S6-HOT-PATH (2026-05-01):
      direction = sig_ep.dominant_direction (episode-level, premium-weighted)
      Replaces naive contract_type -> direction mapping that broke SELL PUT campaigns.

    ING-006-PREMERGE (2026-05-03):
      PREMERGE-2: get_signal() was removed from RepetitionAccumulator (PBE-F4
      cooldown retirement). sig_ep is now set equal to persist_ep — the stream's
      own SIG-DEBOUNCE / SIGNAL-GATE handle all throttling at this layer.
      PREMERGE-1: get_alert_level() now accepts total_premium float, not episode.
      PREMERGE-4: accumulator.min_premium no longer exists — log line updated.
      PREMERGE-5: sig_ep.summary_str() removed — replaced with inline f-string.

    ING-007 (2026-05-04):
      order_side: WARN removed. Field is UNKNOWN by platform design (ING-001).
        Default to UNKNOWN silently. One-time INFO logged at startup instead.
      execution_mechanic: WARN downgraded to DEBUG. AMBIGUOUS_LONG is correct
        cold default — not a regression condition at the timesale layer.
      strong_sentiment: now computed inline from is_directionally_aggressive(
        ev.bid_ask_class, ev.contract_type) per ING-006 contract. Severs the
        stale coupling to ev.strong_sentiment / execution_mechanic from the
        pre-ING-006 parser path.
      enqueue_lookback: ContractKey enqueued for async lookback enrichment after
        persist_ep gate passes. Processed by start_lookback_worker(accumulator)
        running in main.py lifespan.
    """
    _stats["ticks"] += 1
    tick_n = _stats["ticks"]

    # FIRST-TICK: log first N ticks individually so Railway confirms data flow
    if tick_n <= _FIRST_TICK_LOG_COUNT:
        log.info(
            "[stream] FIRST-TICK #%d raw=%r",
            tick_n, {k: v for k, v in raw.items() if k != "data"},
        )

    # FLOW-DEBUG: periodic funnel summary
    if tick_n % _STATS_LOG_INTERVAL == 0:
        _parser_stats = get_parser_stats()
        log.info(
            "[flow-funnel] ticks=%d parsed=%d parse_failed=%d below_min_premium=%d "
            "deduped=%d classified=%d accumulator_gated=%d "
            "persisted=%d signals=%d sig_debounced=%d",
            tick_n,
            _stats["parsed"],
            _stats["parse_failed"],
            _parser_stats["below_min_premium"],
            _stats["deduped"],
            _stats["classified"],
            _stats["accumulator_gated"],
            _stats["persisted"],
            _stats["signals"],
            _stats["sig_debounced"],
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

    # ING-002: 3-state result — sentinel MUST be checked BEFORE None.
    # "below_premium" is truthy; `if not result` would NOT catch it.
    # Counter for "below_premium" is owned by the parser, not here.
    result = parse_tradier_trade(trade_payload)
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

    # DEDUP-KWARGS fix
    if flow_dedup.is_duplicate(
        occ_symbol,
        size=ev.size,
        fill=ev.fill_price,
        exchange=exchange,
        ts=arrival_ts,
    ):
        _stats["deduped"] += 1
        log.info(
            "[dedup] dropped duplicate #%d: %s size=%d fill=%.2f exch=%s",
            _stats["deduped"], occ_symbol, ev.size, ev.fill_price, exchange,
        )

        exch_count = flow_dedup.get_exchange_count(occ_symbol, ev.size, ev.fill_price)
        if exch_count == flow_dedup._sweep_min:
            dispatch_key = f"{occ_symbol}|{ev.size}|{ev.fill_price:.2f}"

            # H4: evict stale entries before membership check
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

    log.debug(
        f"[flow] {ev.ticker} {ev.contract_type} "
        f"${ev.strike:.2f} {ev.expiry} dte={ev.dte} "
        f"| fill={ev.fill_price} bid={ev.bid} ask={ev.ask} size={ev.size} "
        f"| prem=${ev.premium:,.0f} "
        f"| ba={ev.bid_ask_class} aggressive={ev.is_aggressive} "
        f"| type={ev.trade_type} exch={exchange} exch_count={ev.exchange_count} "
        f"| sentiment={ev.sentiment} tier={ev.influence_tier} "
        f"| conviction={ev.conviction_score} occ={occ_symbol} "
        f"| synthetic_quote={ev.is_synthetic_quote}"
    )

    persist_ep = await accumulator.ingest_tick(ev)

    # PREMERGE-2 (ING-006): get_signal() was removed from RepetitionAccumulator
    # (PBE-F4 cooldown retirement — dead state). sig_ep is the same episode
    # returned by ingest_tick. The stream's SIGNAL-GATE + SIG-DEBOUNCE handle
    # all throttling; no per-accumulator cooldown is needed at this layer.
    sig_ep = persist_ep

    if not persist_ep:
        _stats["accumulator_gated"] += 1
        log.info(
            "[accumulator] gated %s %s $%.0f dte=%d prem=$%.0f "
            "(below DTE-adjusted premium floor)",
            ev.ticker, ev.contract_type, ev.strike, ev.dte, ev.premium,
        )
        return

    # ING-007: order_side is UNKNOWN by platform design — not a regression.
    # No per-tick warning. One-time INFO logged at startup in stream_options_flow().
    _order_side = getattr(ev, "order_side", None) or "UNKNOWN"

    # ING-007: enqueue ContractKey for async lookback enrichment (non-blocking).
    # start_lookback_worker(accumulator) in main.py lifespan drains this queue.
    from utils.contract_day_cache import ContractKey as _ContractKey
    enqueue_lookback(_ContractKey(ev.ticker, ev.contract_type, ev.strike, ev.expiry))

    # ING-007: strong_sentiment computed from is_directionally_aggressive() per
    # ING-006 contract. Severs stale coupling to ev.strong_sentiment / execution_mechanic.
    # AT_BID or BELOW_BID on either CALL or PUT = directionally aggressive = strong sentiment.
    _strong_sentiment = is_directionally_aggressive(
        getattr(ev, "bid_ask_class", ""), ev.contract_type
    )

    # ING-007: execution_mechanic AMBIGUOUS_LONG is the correct cold default.
    # Not a warning condition — debug only (silent in prod at INFO log level).
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
                "is_golden_sweep":      ev.is_golden_sweep,
                "sentiment":            ev.sentiment,
                "influence_tier":       ev.influence_tier,
                "conviction_score":     ev.conviction_score,
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

    # SIGNAL-GATE: episode must clear minimum volume before any signal fires
    if sig_ep.trade_count < _SIGNAL_MIN_TRADES or sig_ep.total_premium <= _SIGNAL_MIN_PREMIUM:
        log.debug(
            "[signal-gate] suppressed %s %s — trades=%d (min=%d) prem=$%.0f (min=$%.0f)",
            sig_ep.ticker, sig_ep.contract_type,
            sig_ep.trade_count, _SIGNAL_MIN_TRADES,
            sig_ep.total_premium, _SIGNAL_MIN_PREMIUM,
        )
        return

    # PREMERGE-1 (ING-006): get_alert_level() signature changed to accept
    # total_premium: float. Passing the episode object returned wrong tier
    # (float comparisons against an object always evaluated falsy).
    alert_level = accumulator.get_alert_level(sig_ep.total_premium)

    direction = sig_ep.dominant_direction

    # EPISODE-FIX (2026-04-30): persist flow_episode BEFORE the debounce gate.
    # PREMERGE-5 (ING-006): summary_str() removed from RepetitionEpisode —
    # replaced with inline f-string using public episode properties.
    ep_summary = (
        f"{sig_ep.ticker} {sig_ep.contract_type} ${sig_ep.strike:.0f} {sig_ep.expiry} "
        f"trades={sig_ep.trade_count} prem=${sig_ep.total_premium:,.0f}"
    )
    asyncio.create_task(persist_flow_episode({
        "ticker":          sig_ep.ticker,
        "direction":       direction,
        "contract_type":   sig_ep.contract_type,
        "strike":          sig_ep.strike,
        "expiry":          sig_ep.expiry,
        "total_premium":   sig_ep.total_premium,
        "trade_count":     sig_ep.trade_count,
        "alert_level":     alert_level,
        "is_accelerating": sig_ep.is_accelerating,
        "seed_episode":    ep_summary,
        "timestamp":       ev.timestamp.isoformat(),
    }))

    # SIG-DEBOUNCE: per-episode emit gate — suppress if nothing meaningful changed
    now_ts   = _time.time()
    emit_key = f"{sig_ep.ticker}|{sig_ep.contract_type}|{sig_ep.strike}|{sig_ep.expiry}"

    # Evict stale entries every time we reach this point (amortised cleanup)
    _evict_signal_emit_cache(now_ts)

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

    # Update tracker before publishing
    _signal_last_emit[emit_key] = {
        "alert_level": alert_level,
        "premium":     sig_ep.total_premium,
        "ts":          now_ts,
    }

    log.info(
        "[signal] %s %s | alert=%s | trades=%d | total_prem=$%.0f "
        "| accel=%s | reason=%s | %s",
        sig_ep.ticker, sig_ep.contract_type,
        alert_level,
        sig_ep.trade_count,
        sig_ep.total_premium,
        sig_ep.is_accelerating,
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
            "ticker":          sig_ep.ticker,
            "direction":       direction,
            "contract_type":   sig_ep.contract_type,
            "strike":          sig_ep.strike,
            "expiry":          sig_ep.expiry,
            "total_premium":   sig_ep.total_premium,
            "trade_count":     sig_ep.trade_count,
            "alert_level":     alert_level,
            "is_accelerating": sig_ep.is_accelerating,
            "seed_episode":    ep_summary,
            "timestamp":       ev.timestamp.isoformat(),
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
# Demo mode — DISABLED as automatic fallback (2026-04-25)
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
                    "ticker":          ticker,
                    "direction":       direction,
                    "contract_type":   ctype,
                    "strike":          strike,
                    "expiry":          demo_expiry,
                    "total_premium":   prem,
                    "trade_count":     rng.randint(3, 25),
                    "alert_level":     rng.choices(levels, weights=[5, 15, 30, 50])[0],
                    "is_accelerating": rng.random() < 0.2,
                    "seed_episode":    f"Demo: {ticker} synthetic flow",
                    "timestamp":       dt.datetime.utcnow().isoformat(),
                },
            }
            _stats["ticks"]      += 1
            _stats["classified"] += 1
            _stats["signals"]    += 1
            _stats["last_tick_at"] = _time.time()
            await bus.publish_all(signal)

            composite_score = round(rng.uniform(0.40, 0.85), 3)  # demo headroom — live ceiling is 0.90 (COMPOSITE_SCORE_CEILING)
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
