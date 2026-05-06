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
# patch("services.tradier_stream._lbc") etc. Inline imports inside
# _process_trade() bind to local variables that patch() cannot reach.
# NOTE: _cache and _is_fresh are imported under aliases _lbc / _lbc_fresh here.
# Do not rename those symbols in contract_day_cache.py without updating these imports.
from utils.contract_day_cache import (
    _cache as _lbc,
    _is_fresh as _lbc_fresh,
    ContractKey as _ContractKey,
)

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
# PBE-BLOCKING-1: _PERSIST_EPISODE_TIMEOUT removed. persist_flow_episode is
# fire-and-forget (asyncio.create_task) — see fix note in module docstring.

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

# ---------------------------------------------------------------------------
# Signal gate thresholds
# ---------------------------------------------------------------------------
_SIGNAL_MIN_TRADES  = 3
_SIGNAL_MIN_PREMIUM = 50_000

# ---------------------------------------------------------------------------
# Per-episode signal debounce
# ---------------------------------------------------------------------------
_SIGNAL_DEBOUNCE_S  = 30.0
_SIGNAL_DELTA_PREM  = 25_000.0
_SIGNAL_DELTA_PCT   = 0.20
_SIGNAL_EMIT_TTL_S  = 7_200.0

# PBE-2: TTL for _lookback_result_cache entries. Matches _SIGNAL_EMIT_TTL_S
# so both caches age out on the same 2-hour cycle. Self-contained — eviction
# has no dependency on _signal_last_emit.
_LBC_TTL_S = 7_200.0

# ---------------------------------------------------------------------------
# Global stats
# ---------------------------------------------------------------------------
_stream_start_at: float = _time.time()

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
}

_non_timesale_etypes_seen: set = set()
_order_side_startup_logged: bool = False

# PREMERGE-3 (ING-006): min_premium= constructor kwarg removed in ING-006.
accumulator = RepetitionAccumulator(
    window_minutes=30,
    min_trades=1,
    dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
)

_sweep_upgrade_dispatched: dict[str, float] = {}
_signal_last_emit: dict[str, dict] = {}

# ING-007 / PBE-2: in-process cache of last-known lookback result per emit_key.
# dict[emit_key, tuple[is_multi_day_repeat: bool, stamped_at: float]]
# Eviction is self-contained via _evict_lookback_result_cache() using _LBC_TTL_S.
_lookback_result_cache: dict[str, tuple[bool, float]] = {}


def get_stats() -> dict:
    stats = dict(_stats)
    stats["uptime_seconds"] = round(_time.time() - _stream_start_at, 1)
    stats.update(get_parser_stats())
    stats.update(flow_dedup.dedup_stats())
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
    """
    PBE-2 fix: self-contained TTL eviction keyed on stamped_at.

    Previous implementation evicted entries where emit_key was absent from
    _signal_last_emit — piggybacking on the wrong namespace. Contracts that
    never crossed the signal gate (and therefore never wrote to
    _signal_last_emit) would accumulate entries indefinitely.

    Now: _lookback_result_cache values are (bool, float) tuples where
    float is time.time() at write time. Evict entries older than _LBC_TTL_S.
    Fully independent of _signal_last_emit.
    """
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

    C008 fix (2026-05-05): decouple persist gate from signal gate.
      persist_ep = await accumulator.ingest_tick(ev)   # persist gate, no cooldown
      sig_ep     = await accumulator.get_signal(ev)    # bus gate, cooldown-aware

    persist_flow_event fires whenever ingest_tick returns non-None (persist_ep).
    bus.publish_all fires only when get_signal returns non-None (sig_ep).
    This satisfies C008-1 (persist fires, bus silent during cooldown) and
    C008-2 (both fire after cooldown passes).

    PBE-BLOCKING-1 fix (2026-05-06): persist_flow_episode is fire-and-forget.
      Episode write does not block the signal emit path. The queue worker
      (_update_episode_multiday) enriches the DB row asynchronously.
      A missed episode row is an acceptable enrichment loss under Supabase
      pressure; a 3s stall on the hot path is not.
    """
    _stats["ticks"] += 1
    tick_n = _stats["ticks"]

    if tick_n <= _FIRST_TICK_LOG_COUNT:
        log.info(
            "[stream] FIRST-TICK #%d raw=%r",
            tick_n, {k: v for k, v in raw.items() if k != "data"},
        )

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

    # C008 fix: ingest_tick() is the persist gate (no cooldown).
    # get_signal() is the bus gate (cooldown-aware).
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

    # ING-007: order_side is UNKNOWN by platform design.
    _order_side = getattr(ev, "order_side", None) or "UNKNOWN"

    # ING-007: enqueue ContractKey for async lookback enrichment (non-blocking).
    _contract_key = _ContractKey(ev.ticker, ev.contract_type, ev.strike, ev.expiry)
    enqueue_lookback(_contract_key)

    # ING-007 / PBE-1 fix: resolve is_multi_day_repeat synchronously from the
    # in-process cache using accumulator._multi_day_min_days as the canonical
    # threshold.
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

    # PBE-2 fix: stamp wall-clock time so _evict_lookback_result_cache() can
    # age entries out independently of _signal_last_emit.
    _now = _time.time()
    if _is_repeat_now:
        _lookback_result_cache[emit_key] = (True, _now)
    elif emit_key not in _lookback_result_cache:
        _lookback_result_cache[emit_key] = (False, _now)
    _is_multi_day_repeat: bool = _lookback_result_cache[emit_key][0]

    # ING-007: strong_sentiment computed from is_directionally_aggressive().
    _strong_sentiment = is_directionally_aggressive(
        getattr(ev, "bid_ask_class", ""), ev.contract_type
    )

    # ING-007: execution_mechanic AMBIGUOUS_LONG is the correct cold default.
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

    # C008 fix: bus gate is sig_ep (from get_signal), not persist_ep.
    if not sig_ep:
        return

    # SIGNAL-GATE
    if sig_ep.trade_count < _SIGNAL_MIN_TRADES or sig_ep.total_premium <= _SIGNAL_MIN_PREMIUM:
        log.debug(
            "[signal-gate] suppressed %s %s — trades=%d (min=%d) prem=$%.0f (min=$%.0f)",
            sig_ep.ticker, sig_ep.contract_type,
            sig_ep.trade_count, _SIGNAL_MIN_TRADES,
            sig_ep.total_premium, _SIGNAL_MIN_PREMIUM,
        )
        return

    alert_level = accumulator.get_alert_level(sig_ep.total_premium)
    direction   = sig_ep.dominant_direction

    ep_summary = (
        f"{sig_ep.ticker} {sig_ep.contract_type} ${sig_ep.strike:.0f} {sig_ep.expiry} "
        f"trades={sig_ep.trade_count} prem=${sig_ep.total_premium:,.0f}"
    )

    # EPISODE-FIX: persist before debounce gate.
    # PBE-BLOCKING-1 fix: fire-and-forget via create_task — does NOT block the
    # hot path. Episode enrichment is asynchronous (queue worker patches DB row).
    # A missed episode row under Supabase pressure is an acceptable enrichment
    # loss. Timeout errors on the hot path are not.
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

    # SIG-DEBOUNCE
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
            # QA-3: is_multi_day_repeat is present in the bus payload so downstream
            # consumers (e.g. frontend WebSocket handlers) receive it. It is NOT
            # forwarded to signal_history by signal_store._build_row() because the
            # column does not exist in signal_history yet. _build_row() reads only
            # explicit keys via .get() — unknown keys are silently ignored and the
            # Supabase REST insert succeeds cleanly without the field.
            # The signal_history column migration is gated on ING-009 merge.
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
