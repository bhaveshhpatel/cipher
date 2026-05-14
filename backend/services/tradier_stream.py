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
  build_composite() can legitimatel