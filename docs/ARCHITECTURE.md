# Cipher — Architecture & Data Flow

> Last updated: 2026-04-30 (STREAM-ELIGIBLE fix — upsert_symbol_quotes no longer writes
> stream_eligible, preventing warm-restart wipeout of eligible symbols; DEDUP-2 —
> _SNAPSHOT_REUSE_DRIFT_PCT raised 10% → 30%, _KEEP_SNAPSHOTS reduced 7 → 3;
> startup universe data-fetch path documented — Tradier batch quotes called only on cold
> start / 24h refresh, never on warm restart; Signal gate _SIGNAL_MIN_TRADES/PREMIUM,
> Bug 1 alert_level in composite_msg, accumulator deadlock fix + per-key locks +
> is_accelerating ≥3/60s, alert_level thresholds updated 5M/1M-accel/1M/250k,
> flow_events created_at+influence_tier column fix, sweep upgrade PostgREST fix,
> /flow/events + /flow/episodes API endpoints, Flow Events/Episodes dashboard tabs,
> WS 403 auth-failure stop, pydantic-settings env var fix)

---

## Overview

Cipher is an institutional options flow intelligence platform. It monitors live Tradier WebSocket
streams across a tier-filtered OCC symbol universe, classifies each trade tick through a 6-layer
pipeline, detects repetition patterns, runs a composite signal engine, and surfaces high-conviction
signals to the frontend via WebSocket — persisting all events and signals to Supabase for historical
querying.

At runtime the active worker count is `ceil(registry.size() / 500)` — typically 60–70 workers for
a full universe of ~31,920 OCC symbols. All workers share a single Tradier session token and stream
in parallel, each covering ≤500 symbols simultaneously. Workers are staggered at 200ms intervals
on spawn to avoid thundering-herd against the Tradier session endpoint (B-021).

---

## Startup Universe Data-Fetch Strategy

This is the most critical architectural decision for understanding when Tradier is called.

### Warm Restart (Step 1 HIT — the common case)

`_resolve_startup_universe()` calls `universe_store.load_fresh_snapshot(max_age=24h)`.
If a DB snapshot younger than 24h exists:
- Symbols are loaded directly from `options_universe_symbols` (filtered `stream_eligible=True`).
- **Tradier `/v1/markets/quotes` is NOT called** for stock-level batch quotes.
- `_background_build_and_upsert` runs after server is live, calls `registry.build()` which
  fetches OCC chain data from Tradier (options chains only, not stock quotes).
- `_post_build_upsert` then runs, assembling `SymbolQuote` objects from `raw_quotes` already
  returned by `build()` — **no duplicate batch quote call** (H1 fix).
- `upsert_symbol_quotes()` is called to persist updated price/volume/OI/tier data.
- `stream_eligible` is **NOT written** by `upsert_symbol_quotes()` (STREAM-ELIGIBLE fix).

### Cold Start / 24h Refresh (Step 1 MISS)

When no fresh snapshot exists (first deploy, or snapshot is >24h old):
- Full pipeline runs: CBOE universe fetch → `load_universe()` → Tradier eligibility validation
  (`_fetch_batch_quotes()`) → `save_snapshot()` → `upsert_symbol_quotes()`.
- `stream_eligible=True` is written to DB by `_sync_save_snapshot()` **only on this path**.
- `_universe_refresh_loop` repeats this full cycle every 24h in the background.

### Why This Matters

`stream_eligible` is a **write-once-per-24h** flag set exclusively by `_sync_save_snapshot()`.
It must never be overwritten by the warm-restart upsert cycle, because `SymbolQuote` objects
assembled from OCC `raw_quotes` always default `stream_eligible=False` (STREAM-ELIGIBLE fix,
2026-04-30). Before this fix, every warm restart silently wiped `stream_eligible=True` → `False`
for all symbols, causing `_load_symbols()` (which filters `stream_eligible=True`) to return 0 or
very few symbols on the next restart.

---

## The 6-Layer Architecture

```text
┌──────────────────────────────────────────────────────────────────┐
│  Layer 1 — Symbol Registry  (services/symbol_registry.py)        │
│                                                                  │
│  Pre-loads OCC contract metadata at startup into a dict.         │
│  On each stream tick: O(1) lookup                                │
│    registry["TSLA260424C00375000"]                               │
│      → { ticker, strike, expiry, contract_type, DTE, tier }      │
│  No regex, no API call, no per-tick latency.                     │
│  Refreshes every REGISTRY_REFRESH_MINS (default 30).             │
│  On expiry days: refreshes every 15 min.                         │
│                                                                  │
│  Chain bulk fetch (H3 fix):                                      │
│  Uses get_option_chain_bulk() (was get_option_chain).            │
│  Module-level import ensures patch targets work in tests.        │
│                                                                  │
│  build() return type (H1 fix):                                   │
│  registry.build() returns tuple[int, dict[str, dict]] —          │
│  (count, raw_quotes). raw_quotes is the stock-level quote map    │
│  from _fetch_stock_prices() (Tradier /v1/markets/quotes).        │
│  Callers must unpack: count, raw_quotes = await registry.build() │
│  _background_build_and_upsert passes raw_quotes to               │
│  _post_build_upsert to avoid a duplicate batch quote call.       │
│                                                                  │
│  raw_quotes content:                                             │
│  dict[ticker → raw Tradier quote dict] from get_quotes_batch().  │
│  Contains: last, close, prevclose, volume, average_volume, etc.  │
│  This is stock-level daily data — NOT options chain data.        │
│  OI comes from chain fetches (_build_ticker), not raw_quotes.    │
│                                                                  │
│  DB chain fast-seed (lifespan, on DB-hit path):                  │
│  registry.load_from_db(snapshot_id) pre-loads OCC contracts      │
│  from the chain_store DB cache before the full build runs.       │
│  Allows lookup() to work immediately on restart without waiting  │
│  for a full Tradier chain fetch.                                 │
│                                                                  │
│  Pre-warm loop (_registry_prewarm_loop in main.py):              │
│  Fires every weekday at 9:15 AM ET (15 min before market open).  │
│  Rebuilds the full OCC contract set so workers connect instantly  │
│  at 9:30 AM with no cold-start contract-load delay.              │
│  Skipped on weekends. Non-fatal on error.                        │
│                                                                  │
│  Per-tier contract filtering:                                    │
│  Contract universe is shaped by the symbol's tier at build time. │
│  Tier params loaded from tier_thresholds DB row (cached 300s).   │
│    Tier 1 (liquid): ATM ±20%  max DTE 90  (e.g. AAPL, TSLA)     │
│    Tier 2 (mid-cap): ATM ±15%  max DTE 60                        │
│    Tier 3 (default): ATM ±10%  max DTE 30                        │
│  Unknown-tier symbols fall back to T3 params.                    │
│  ContractMeta gains a .tier field — carried through pipeline     │
│  into backtest_score (historical win-rate by ticker/type/DTE/    │
│  tier). Tier map seeded from universe_store.load_tier_map() on   │
│  warm start; updated via registry.set_tier_map() on refresh.     │
│  assign_tiers() must be called with require_oi=True to enforce   │
│  the OI gate (the default require_oi=False skips OI filtering).  │
│                                                                  │
│  Build-complete flag:                                            │
│  registry._build_complete is set True only after registry.build()│
│  finishes. stream_options_flow() waits on this flag (M-1/M-2)    │
│  before handing symbols to StreamManager, ensuring no worker     │
│  connects before the OCC contract set is fully ready.            │
└───────────────────────────────┴──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 2 — Stream Ingestion                                      │
│    Entry:   services/tradier_stream.py (stream_options_flow)     │
│    Manager: services/stream_manager.py                           │
│    Workers: services/stream_worker.py                            │
│                                                                  │
│  stream_options_flow() (tradier_stream.py):                      │
│  - Builds the OCC SymbolRegistry (Layer 1) for the watchlist.    │
│  - D-001: Accepts optional registry= from lifespan. When         │
│    provided, skips init_registry()/build() and polls             │
│    registry.is_ready() (500ms poll, 30-min timeout) instead.     │
│  - When registry=None (standalone/test), builds registry inline  │
│    and spawns refresh_loop() as a background task.               │
│  - In lifespan mode, lifespan owns refresh_loop — not spawned    │
│    here (D-002 fix).                                             │
│  - Instantiates StreamManager with the registry and hands off    │
│    _process_trade as the per-event callback.                     │
│                                                                  │
│  StreamManager (stream_manager.py):                              │
│  - Fetches ONE shared Tradier session token for all workers.     │
│  - Splits OCC symbols into ≤500-symbol batches.                  │
│  - Worker count: ceil(registry.size() / 500) (D-003).            │
│    ~31,920 symbols → 64 workers; ~15,000 symbols → 30 workers.  │
│  - Spawns one asyncio task per batch (STREAM-1/2/3...).          │
│  - Worker N is delayed N × 200ms before connecting (B-021).      │
│    Stagger prevents thundering-herd on the Tradier session host.  │
│    At 64 workers the last worker connects at T+12.8s.            │
│  - No lock between workers — all tasks run fully concurrently.   │
│  - Emits STREAM_STATS log every 30s per worker.                  │
│  - Emits STREAM_HEALTH manager-level log every 30s.              │
│  - On session token expiry: re-fetches token, restarts workers.  │
│                                                                  │
│  StreamWorker (stream_worker.py):                                │
│  - Each worker opens one httpx streaming POST to Tradier.        │
│  - Chunk size: _CHUNK_SIZE=500 (aligned to API batch limit).     │
│  - Calls process_fn(_process_trade) for every timesale event.    │
│  - 30s idle watchdog: reconnects if no keepalive received.       │
│  - Exponential backoff: 5s base, 60s cap, jitter on all errors.  │
│                                                                  │
│  Demo mode:                                                      │
│  - DISABLED as automatic fallback since 2026-04-25.              │
│  - Admin panel only — explicit invocation via /admin endpoint.   │
│  - Synthetic signal generator preserved as _demo_mode_once().    │
└───────────────────────────────┴──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 3 — Trade Parsing  (parsers/options_flow_parser.py)       │
│                                                                  │
│  parse_tradier_trade(raw_dict) → OptionsFlowEvent | None         │
│                                                                  │
│  Responsibilities:                                               │
│  - Extract fill_price (last > 0, else mid-of-bid-ask).           │
│  - OCC symbol regex: extract ticker, expiry, contract_type,      │
│    strike from standard 21-char OCC format.                      │
│    Fallback: occ_positional parse for non-standard symbols.      │
│  - Compute premium = fill_price × size × 100.                   │
│  - Classify bid_ask_class: BID / ASK / MID based on position.   │
│  - Classify is_aggressive: fill at or through ask.               │
│  - Classify trade_type: BTO / STO / SWEEP (post-dedup upgrade).  │
│  - Classify sentiment: BULLISH / BEARISH / NEUTRAL.              │
│  - Classify influence_tier: WHALE / INSTITUTIONAL / LARGE /      │
│    RETAIL based on premium thresholds.                           │
│  - Compute conviction_score (0.0–1.0) from size, premium,        │
│    aggression, and spread quality.                               │
│  - is_golden_sweep: SWEEP AND premium >= $500,000.               │
│  - is_synthetic_quote: bid=0 AND ask=0 (no live market data).    │
│  - DTE: calendar days from today to expiry.                      │
│  - Returns None if symbol is unparseable, size=0, or fill=0.    │
└───────────────────────────────┴──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 4 — Deduplication  (utils/dedup.py — DedupCache)          │
│                                                                  │
│  flow_dedup.is_duplicate(occ_symbol, size, fill, exchange, ts)   │
│    → bool                                                        │
│                                                                  │
│  NOTE (DEDUP-KWARGS fix 2026-04-28):                             │
│  First param is positional (event_or_occ_symbol). Callers must   │
│  pass occ_symbol as the first positional arg, not as a keyword.  │
│  Keyword form raises TypeError. Fixed in _process_trade().        │
│                                                                  │
│  Dedup key: (occ_symbol, size, round(fill_price, 1))             │
│  Per-key state: {exchanges: set, first_seen: float, ts: float}   │
│  Sweep detection: exchange_count >= _sweep_min (3 exchanges)     │
│                                                                  │
│  C-019 / C-020 fixes:                                            │
│  - TTL: 5 seconds (entries expire 5s after first seen).          │
│  - Sweep window: 8 seconds (wider than TTL for multi-leg fills). │
│  - Eviction sweep runs on every is_duplicate() call.             │
│  - Clock source: time.monotonic() — not wall clock (C-020).      │
│    Prevents TTL misfires on DST transitions or system clock skew.│
│                                                                  │
│  C-003 — Sweep Retroactive Upgrade:                              │
│  When the 3rd unique exchange for a (occ|size|fill) key arrives  │
│  as a duplicate tick — meaning the canonical row was already      │
│  written as trade_type='BTO' — _process_trade() dispatches a     │
│  background upgrade_to_sweep_in_db() call.                       │
│  Issues a targeted PATCH to flow_events: sets trade_type='SWEEP' │
│  for rows matching (occ_symbol, fill_price, size) within the     │
│  last 30s using a PostgREST-compatible SQL expression filter     │
│  (fixed 2026-04-29 — prior form was invalid PostgREST syntax).   │
│                                                                  │
│  H4 — Sweep dispatch TTL eviction:                               │
│  _sweep_upgrade_dispatched was a Set that grew forever. Changed  │
│  to dict[str, float] (key → wall-clock timestamp). Entries       │
│  older than _SWEEP_DISPATCH_TTL_S (1800s / 30 min) are evicted   │
│  before each membership check. Correctly re-dispatches upgrade   │
│  for the same contract reprinting after 30 min.                  │
│                                                                  │
│  Stats exposed on /health/stream:                                │
│    dedup_hits, dedup_total, sweep_upgrades, active_keys          │
└───────────────────────────────┴──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 5 — Repetition Accumulator + Flow Persistence             │
│    Accumulator: signals/repetition_accumulator.py                │
│    Persistence:  services/flow_store.py                          │
│                                                                  │
│  RepetitionEpisode (updated 2026-04-29):                         │
│  - trade_count: computed property (len of events list)           │
│  - total_premium: computed property (sum of event premiums)      │
│  - is_accelerating: property — True when ≥3 events within 60s   │
│    (was: ≥2 ticks within 5 minutes)                              │
│  - New fields: first_seen, last_signal_at, occ_symbol, direction │
│                                                                  │
│  RepetitionAccumulator (updated 2026-04-29):                     │
│  - Per-key asyncio.Lock via _get_lock(key) — eliminates the      │
│    global lock deadlock (STREAM-3 + accumulator deadlock fix).   │
│  - _key(ev) and _key_from_ep(ep) helpers for episode keying.     │
│  - signal_cooldown param: gates re-emission at accumulator level.│
│  - ingest() shim with cooldown gate (distinct from Gate 2).      │
│  - get_signal() respects signal_cooldown and last_signal_at.     │
│  - Window pruning uses event timestamps, not wall clock.         │
│  - ingest_tick / get_signal / ingest contracts are separated     │
│    (no nested lock acquisition — deadlock-safe).                 │
│                                                                  │
│  Episode key: (ticker, contract_type, strike, expiry)            │
│  Episode window: 30 minutes of inactivity before reset.          │
│                                                                  │
│  Gate 1 — Persist threshold (OR logic):                          │
│    trade_count >= min_trades (default 1), OR                     │
│    total_premium >= min_premium (default $10,000)                │
│  Below both thresholds → ingest_tick() returns None → dropped.   │
│  accumulator_gated stat incremented and logged at INFO.          │
│  NOTE: Gate 1 is intentionally kept at low thresholds so that    │
│  flow_events persist for ALL qualifying ticks. Signal bus        │
│  publish is separately gated (see Signal Gate in Layer 6).       │
│                                                                  │
│  Gate 2 — Signal re-emission guard:                              │
│  RepetitionEpisode.last_signaled_premium tracks the total_premium │
│  at the last emission. After Gate 1 is crossed, ingest_tick()    │
│  only returns the episode again when:                            │
│    total_premium - last_signaled_premium >= SIGNAL_RETRIGGER     │
│    (default $50,000)                                             │
│  Prevents QQQ/SPY episodes from emitting on every single tick.   │
│                                                                  │
│  Concurrency note:                                               │
│  64 workers call ingest_tick() concurrently.                     │
│  Per-episode asyncio locks isolate concurrent ticks on the same  │
│  (ticker, strike, expiry, type) key. Cross-episode calls are     │
│  fully parallel (B-029 open: prune race).                        │
│                                                                  │
│  Flow persistence (flow_store.py):                               │
│  - flow_events table: one row per classified tick.               │
│    Written via batched buffer: 500ms timer OR 100-row early flush.│
│    3 retries with 1s delay on Supabase failure.                  │
│    SUPABASE_SERVICE_ROLE_KEY only (bypasses RLS).                │
│    Column note: timestamp field in DB is created_at.             │
│    Pydantic model (FlowEventRaw) maps created_at → timestamp.    │
│    Tier field in DB is influence_tier (not tier).                │
│  - flow_episodes table: one row per qualifying episode.          │
│    Written immediately on composite_signal bus message.          │
│    Written by _bus_signal_listener on "db_writer" channel.       │
│                                                                  │
│  ALERT-LEVEL fix (2026-04-28/29):                                │
│  _bus_signal_listener was reading sig.get("recommendation")      │
│  (BUY/SELL/HOLD) for alert_level — wrong field.                  │
│  Fix: reads sig.get("alert_level") correctly.                    │
│  Confirmed populated: alert_level is injected into               │
│  composite_msg["data"]["signal"] in _process_trade() before      │
│  any bus publish (Bug 1 fix 2026-04-29 — was previously missing  │
│  from the composite_msg dict entirely).                          │
│                                                                  │
│  alert_level values (updated thresholds 2026-04-29):             │
│    total_premium >= $5,000,000                → CONVICTION       │
│    total_premium >= $1,000,000 AND accel.     → STRONG_SIGNAL    │
│    total_premium >= $1,000,000                → ALERT            │
│    total_premium >= $250,000                  → WATCH            │
│    (below $250k: gated, not emitted)                             │
│                                                                  │
│  flow_events columns written:                                    │
│    ticker, contract_type, strike, expiry, dte, fill_price,       │
│    bid, ask, size, premium, trade_type, bid_ask_class,           │
│    is_aggressive, is_golden_sweep, sentiment, influence_tier,    │
│    conviction_score, exchange_count, fill_count, open_interest,  │
│    iv, underlying_price, occ_symbol, is_synthetic_quote          │
│    created_at (default now())                                    │
│  (No id field — Postgres uuid default generates it.)             │
│                                                                  │
│  flow_episodes columns written:                                  │
│    ticker, direction, contract_type, strike, expiry,             │
│    total_premium, trade_count, alert_level, is_accelerating,     │
│    seed_episode, signal_ts                                       │
│  (No id field — bigserial generated.)                            │
└───────────────────────────────┴──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 6 — Signal Engine + Delivery                              │
│    Composite:  signals/composite_signal_engine.py                │
│    Bus:        core/async_bus.py                                 │
│    Signal DB:  services/signal_store.py                          │
│    WebSocket:  routers/ws.py (or routers/stream.py)              │
│    REST API:   routers/flow.py (flow events + episodes)          │
│    Frontend:   Next.js dashboard (frontend/src/)                 │
│                                                                  │
│  Signal Gate (_process_trade — 2026-04-29):                      │
│  After persist_flow_event() completes, an explicit gate checks:  │
│    sig_ep.trade_count >= _SIGNAL_MIN_TRADES (3) AND              │
│    sig_ep.total_premium >  _SIGNAL_MIN_PREMIUM (50,000)          │
│  If either condition fails → suppressed (debug log) → RETURN.    │
│  flow_events write is UNAFFECTED — only the signal bus           │
│  publish is gated. /health/stream signals counter will diverge   │
│  downward from persisted when this gate activates.               │
│                                                                  │
│  Composite score formula:                                        │
│    composite_score = flow_score    × 0.55                        │
│                    + backtest_score × 0.35                       │
│                    + vol_factor    × 0.10                        │
│                                                                  │
│  flow_score:     normalized from total_premium and trade_count   │
│  backtest_score: historical win-rate by ticker/type/DTE/tier     │
│  vol_factor:     volume_premium_factor from open interest ratio  │
│                                                                  │
│  recommendation from composite_score:                            │
│    score >= 0.65 AND CALL → BUY                                  │
│    score >= 0.65 AND PUT  → SELL                                 │
│    score <  0.65          → HOLD                                 │
│                                                                  │
│  Bus publish sequence (_process_trade):                          │
│  1. publish_all("signals")        → WebSocket clients (signal)   │
│  2. publish_all("db_writer")      → flow_store.py (flow_episode) │
│  3. publish_all("signal_writer")  → signal_store.py (signal_hist)│
│  All three channels receive the same composite_signal message.   │
│                                                                  │
│  signal published to "signals" channel (type="signal"):          │
│    ticker, direction, contract_type, strike, expiry,             │
│    total_premium, trade_count, alert_level, is_accelerating,     │
│    seed_episode, timestamp                                       │
│                                                                  │
│  composite_signal published to all channels:                     │
│    signal:  { ticker, recommendation, composite_score,           │
│               flow_score, backtest_score,                        │
│               volume_premium_factor, reasoning, alert_level }    │
│    episode: { contract_type, direction, influence_tier,          │
│               total_premium, trade_count, is_accelerating,       │
│               timestamp }                                        │
│  NOTE: alert_level is now explicitly included in                 │
│  composite_msg["data"]["signal"] (Bug 1 fix 2026-04-29).         │
│                                                                  │
│  signal_store.py (signal_writer channel):                        │
│  - Subscribes to "signal_writer" bus channel.                    │
│  - Persists composite_signal to signal_history table.            │
│  - In-memory deque fallback when DB is unavailable.              │
│  - Exposed via /api/signals/history endpoint.                    │
│                                                                  │
│  REST endpoints (added 2026-04-29):                              │
│  GET /api/flow/events                                            │
│    Queries flow_events directly. Filters: ticker, sentiment,     │
│    contract_type, influence_tier (not tier), aggressive,         │
│    golden_sweep, limit (default 50).                             │
│    Returns: FlowEventRaw[] — includes conviction_score, dte,     │
│    trade_type, iv, underlying_price, occ_symbol.                 │
│    created_at column mapped → timestamp in FlowEventRaw.         │
│                                                                  │
│  GET /api/flow/episodes                                          │
│    Returns flow_episodes with alert_level HOLD+ and all required │
│    fields. Filters: ticker, alert_level, direction, contract_type│
│    Returns: FlowEpisodeOut[] (episode-level aggregation).        │
│    last_signaled_premium delta field present in response.        │
│                                                                  │
│  WebSocket (useSignalStream hook — 2026-04-29):                  │
│  Detects WS close code 403 and stops the retry loop on auth      │
│  failure. Prevents infinite reconnect on expired/invalid token.  │
│                                                                  │
│  SwarmEngine (services/swarm_engine.py):                         │
│  - Groq llama-3.3 AI swarm for narrative signal reasoning.       │
│  - NOT called automatically per tick.                            │
│  - Explicit invocation only (admin panel or direct API call).    │
│                                                                  │
│  Frontend dashboard tabs (updated 2026-04-29):                   │
│  Tab order: Flow Events → Episodes → Composite → Signals → …     │
│  Flow Scanner tab removed.                                       │
│  FlowEventsTab: KPI bar + 5 filters (ticker, sentiment,          │
│    contract_type, influence_tier, aggressive). 10s auto-refresh. │
│  FlowEpisodesTab: alert badges + 4 filters (ticker, alert_level, │
│    direction, contract_type). 30s auto-refresh.                  │
│  Hooks: useFlowEvents(token, filters?), useFlowEpisodes(token,   │
│    filters?) — filters as second arg, owner-controlled state.    │
│                                                                  │
│  Open — B-026:                                                   │
│  WebSocket `send()` iterates subscriber list during broadcast.   │
│  A failed send to one client does not currently isolate other    │
│  subscribers. Under concurrent connects/disconnects, list        │
│  mutation mid-loop is possible. No regression test exists yet.   │
└──────────────────────────────────────────────────────────────────┘
```

---

## Key Runtime Parameters

| Parameter | Value | Source |
|---|---|---|
| Session token model | 1 shared token across all workers | StreamManager |
| Worker count | `ceil(registry.size() / 500)` — dynamic (D-003) | StreamManager |
| Worker stagger delay | N × 200ms (B-021); 64 workers → T+12.8s last connect | StreamManager |
| Tradier batch limit | ≤500 symbols per POST | StreamWorker |
| Dedup key | `(occ_symbol, size, round(fill, 1))` | DedupCache |
| Dedup TTL | 5 seconds (monotonic clock — C-020) | DedupCache |
| Sweep window | 8 seconds | DedupCache |
| Sweep min exchanges | 3 | DedupCache |
| Sweep dispatch TTL | 1800 seconds (30 min) | _sweep_upgrade_dispatched |
| Accumulator Gate 1 | trade_count ≥ 1 OR total_premium ≥ $10k (low — intentional) | RepetitionAccumulator |
| Signal Gate | trade_count ≥ 3 AND total_premium > $50k (bus publish only) | tradier_stream._process_trade |
| Signal retrigger (Gate 2) | Δ total_premium ≥ $50k since last emit | RepetitionAccumulator |
| Episode window | 30 minutes inactivity | RepetitionAccumulator |
| Acceleration threshold | ≥3 events within 60 seconds | RepetitionEpisode.is_accelerating |
| DB write pattern | 500ms flush OR 100-row early flush | flow_store._flush_flow_events |
| DB retry | 3 attempts, 1s delay | flow_store._insert_rows_with_retry |
| Persist timeout | 2 seconds per event | tradier_stream._process_trade |
| Idle watchdog | 30 seconds | StreamWorker |
| Backoff | 5s base, 60s cap, jitter | tradier_stream._backoff |
| Registry refresh | 30 min (15 min on expiry days) | SymbolRegistry |
| Pre-warm time | 9:15 AM ET weekdays | main._registry_prewarm_loop |
| Universe refresh | Every 24 hours | main._universe_refresh_loop |
| Snapshot reuse age | < 24 hours (DEDUP-2) | universe_store._SNAPSHOT_REUSE_MAX_AGE_H |
| Snapshot reuse drift | ≤ 30% symbol count change (DEDUP-2; was 10%) | universe_store._SNAPSHOT_REUSE_DRIFT_PCT |
| Snapshots retained | 3 (DEDUP-2; was 7) | universe_store._KEEP_SNAPSHOTS |
| Stats log interval | Every 100 ticks | tradier_stream._STATS_LOG_INTERVAL |
| First-tick log count | First 5 ticks individually | tradier_stream._FIRST_TICK_LOG_COUNT |
| Demo mode | Admin panel only (disabled as auto-fallback since 2026-04-25) | tradier_stream |
| Flow events API refresh | 10 seconds | useFlowEvents hook |
| Flow episodes API refresh | 30 seconds | useFlowEpisodes hook |

---

## Tier Filtering

| Tier | Symbols | ATM Strike Range | Max DTE |
|---|---|---|---|
| T1 (Liquid) | AAPL, TSLA, SPY, QQQ, etc. | ±20% | 90 days |
| T2 (Mid-cap) | Mid-liquidity names | ±15% | 60 days |
| T3 (Default) | All others / unknown | ±10% | 30 days |

Tier assignment is done in `services/tier_engine.py`. The tier map is seeded from
`universe_store.load_tier_map()` on warm start and updated on every registry refresh via
`registry.set_tier_map()`. All tiers fall back to T3 params if unknown.

`assign_tiers()` and `_classify()` enforce the OI gate only when called with `require_oi=True`.
The default `require_oi=False` skips the OI gate (used for preliminary pass). `build()` calls
`assign_tiers(require_oi=True)` for the final tier assignment.

---

## Alert Levels

| Level | Threshold | Notes |
|---|---|---|
| `CONVICTION` | total_premium ≥ $5,000,000 | Institutional-grade repeated positioning |
| `STRONG_SIGNAL` | total_premium ≥ $1,000,000 AND is_accelerating | High-conviction + accelerating episode |
| `ALERT` | total_premium ≥ $1,000,000 | High-conviction multi-fill episode |
| `WATCH` | total_premium ≥ $250,000 | Notable accumulation above noise floor |
| *(gated)* | total_premium < $250,000 | Not emitted by get_alert_level() |

Thresholds updated 2026-04-29 (was $1M/$500k/$200k). Computed by
`accumulator.get_alert_level(ep)` in `tradier_stream._process_trade()` and injected into both the
`signal` bus message and `composite_msg["data"]["signal"]` **before** any bus publish.
`flow_store._bus_signal_listener` reads `sig.get("alert_level")` — **not**
`sig.get("recommendation")` — to persist the correct value to `flow_episodes.alert_level`.

---

## Async Bus Channels

| Channel | Publisher | Subscribers | Payload |
|---|---|---|---|
| `"signals"` | tradier_stream | WebSocket clients (routers/ws.py) | `signal` + `composite_signal` |
| `"db_writer"` | tradier_stream | flow_store._bus_signal_listener | `composite_signal` only |
| `"signal_writer"` | tradier_stream | signal_store listener | `composite_signal` only |

The bus (`core/async_bus.py`) is a purely in-memory asyncio fan-out. No persistence, no retries at
the bus level — those are the responsibility of each subscriber.

---

## Database Tables

### flow_events
One row per classified options tick that passed Gate 1. Written in batched flushes.

```sql
ticker, contract_type, strike, expiry, dte, fill_price,
bid, ask, size, premium, trade_type, bid_ask_class,
is_aggressive, is_golden_sweep, sentiment, influence_tier,
conviction_score, exchange_count, fill_count, open_interest,
iv, underlying_price, occ_symbol, is_synthetic_quote,
created_at (default now())
-- id: uuid generated by Postgres
-- NOTE: DB column is created_at (not timestamp); influence_tier (not tier)
-- Pydantic FlowEventRaw maps created_at → timestamp for API consumers
```

### flow_episodes
One row per qualifying repetition episode (every Gate 2 emission = one row).

```sql
ticker, direction, contract_type, strike, expiry,
total_premium, trade_count, alert_level, is_accelerating,
seed_episode, signal_ts,
created_at (default now())
-- id: bigserial generated by Postgres
```

### signal_history
Composite signal history. Written by signal_store on "signal_writer" channel.

### options_universe_symbols
OCC symbol universe snapshot with tier assignments.

```sql
snapshot_id, symbol, stream_eligible, tier,
last_price, volume, average_volume, open_interest
-- UNIQUE(snapshot_id, symbol) — migration 013
```

**stream_eligible ownership:** This column is written exclusively by `_sync_save_snapshot()`
during the full pipeline (cold start or 24h `_universe_refresh_loop`). It is **never** written
by `_sync_upsert_symbol_quotes()` (STREAM-ELIGIBLE fix, 2026-04-30). Writing it from the
warm-restart upsert path would silently wipe `True → False` for all symbols every restart,
because `SymbolQuote` objects from `registry.build()` always default `stream_eligible=False`.

**Snapshot reuse logic (DEDUP-2):** `_sync_save_snapshot()` reuses the existing active
`snapshot_id` when:
- Snapshot is < 24 hours old, **AND**
- Symbol count drift is ≤ 30% of existing count (raised from 10% — absorbs natural CBOE
  universe variation of 10–15% per restart).
New snapshots are created only when none of the above hold. At most 3 snapshots are retained
before hard pruning (reduced from 7 — prune fires sooner as safety net).

### chain_store (DB cache)
OCC contract metadata cached from Tradier chain fetches. Pre-seeds the registry on warm restart
via `registry.load_from_db(snapshot_id)` before the full `build()` completes.

---

## Startup Sequence (main.py lifespan)

```text
1. validate_ingestion_config()     — RC-3: warn on missing DB ingestion config rows

2. _resolve_startup_universe()
   ├─ Step 1: load_fresh_snapshot(max_age=24h)
   │    HIT  → load symbols + tier_map from DB (NO Tradier call)
   │            → return (symbols, tier_map, [], snapshot_id)
   │    MISS → Step 2: load_any_snapshot() as stale safety net
   │         → Step 2b: load_universe() → CBOE + Tradier validation
   │               source == "tradier_validated":
   │                 → save_snapshot()         — persist eligible symbols to DB
   │                 → _fetch_batch_quotes()   — Tradier stock quotes (batch)
   │                 → assign_tiers()          — preliminary tier assignment
   │               → return (symbols, tier_map, quotes, snapshot_id)

3. init_registry(watchlist, tier_map)  — in-memory init (instant)

4. registry.load_from_db(snapshot_id)  — seed OCC chains from DB (P1 fallback)
   └─ does NOT set _build_complete — stream workers still blocked

5. SERVER IS LIVE — health probe passes (yield)

6. Parallel background tasks launched:
   a. _background_build_and_upsert   — calls registry.build() (incremental/full OCC)
      └─ build() returns (count, raw_quotes)
      └─ _post_build_upsert(registry, stream_symbols, raw_quotes=raw_quotes)
           Phase 1: assemble SymbolQuote from raw_quotes (no extra Tradier call — H1)
           Phase 2: assign_tiers(require_oi=True) → update registry tier_map
           Phase 3: upsert_symbol_quotes() → persist price/volume/OI/tier to DB
                    NOTE: stream_eligible NOT written here (STREAM-ELIGIBLE fix)
      └─ registry._build_complete = True → stream workers unblock

   b. registry.refresh_loop()         — scheduled 30-min rebuilds
   c. _registry_prewarm_loop()        — 9:15 AM ET daily pre-warm
   d. stream_options_flow()           — polls is_ready() then streams
   e. start_flow_writer()             — DB flush loop
   f. start_signal_writer()           — signal DB writer
   g. _universe_refresh_loop()        — 24h full universe refresh
```

---

## _process_trade Tick Funnel

```text
raw Tradier WebSocket event
    │
    ├─ event_type not in {"timesale"}?          → SKIP (logged at INFO for first 10 types)
    │
    ├─ parse_tradier_trade() → None?            → parse_failed++ / LOG INFO / RETURN
    │
    ├─ flow_dedup.is_duplicate(occ_symbol, ...)?→ deduped++   [positional, not keyword]
    │     └─ exchange_count == sweep_min?       → upgrade_to_sweep_in_db() [background task]
    │                                           → RETURN
    │
    ├─ flow_dedup.is_sweep()?                   → ev.trade_type = "SWEEP"
    │
    ├─ accumulator.ingest_tick()                → classified++
    │     ├─ Gate 1 not crossed?               → accumulator_gated++ / LOG INFO / RETURN
    │     └─ Gate 2 not crossed?               → (no return value) → RETURN
    │
    ├─ persist_flow_event()                     → persisted++ / buffered → flow_events
    │
    ├─ Signal Gate check:                       → suppressed if below threshold
    │     sig_ep.trade_count < _SIGNAL_MIN_TRADES (3)?
    │     OR sig_ep.total_premium ≤ _SIGNAL_MIN_PREMIUM (50,000)?
    │                                           → LOG DEBUG "signal-gate suppressed" / RETURN
    │                                             (flow_events already written above)
    │
    ├─ build_composite()                        → composite score
    │
    ├─ alert_level = accumulator.get_alert_level(sig_ep)
    │
    ├─ assemble composite_msg with alert_level injected into
    │   composite_msg["data"]["signal"]         → Bug 1 fix ensures no None fallback
    │
    ├─ bus.publish_all("signals")               → signals++ / WebSocket delivery
    │
    └─ bus.publish_all("composite_signal")      → flow_episodes + signal_history
```

---

## Key Fixes

| Fix ID | File | Description |
|---|---|---|
| STREAM-ELIGIBLE | universe_store.py | `_sync_upsert_symbol_quotes()` no longer writes `stream_eligible`. SymbolQuote objects from `registry.build()` always default `stream_eligible=False`, silently wiping all eligible symbols on every warm restart. `stream_eligible` is now owned exclusively by `_sync_save_snapshot()`. (2026-04-30) |
| DEDUP-2 | universe_store.py | `_SNAPSHOT_REUSE_DRIFT_PCT` raised 10% → 30% to absorb natural CBOE universe variation (10–15% per restart). `_KEEP_SNAPSHOTS` reduced 7 → 3 so hard pruning fires sooner. Prevents exponential row accumulation when every restart exceeded the 10% drift guard. (2026-04-29) |
| Bug 1 / alert_level-composite | tradier_stream.py | `composite_msg["data"]["signal"]` was missing `alert_level` key. `signal_store._build_row()` always received None, fell through to score-based fallback. Fixed: `alert_level` now injected before any bus publish. |
| Signal Gate | tradier_stream.py | Explicit gate after `persist_flow_event()`: only fires bus publish when `trade_count ≥ 3 AND total_premium > $50k`. Decouples flow_events volume from signal volume. |
| Accumulator deadlock | repetition_accumulator.py | Global lock replaced with per-key asyncio.Lock via `_get_lock()`. Separated `ingest_tick`/`get_signal`/`ingest` contracts — no nested lock acquisition. |
| is_accelerating threshold | repetition_accumulator.py | Changed from ≥2 ticks within 5 minutes to ≥3 events within 60 seconds. |
| Alert level thresholds | repetition_accumulator.py | Updated to $5M (CONVICTION), $1M+accel (STRONG_SIGNAL), $1M (ALERT), $250k (WATCH). Was $1M/$500k/$200k. |
| RepetitionEpisode fields | repetition_accumulator.py | Added `occ_symbol`, `direction`, `first_seen`, `last_signal_at`. `trade_count`/`total_premium` now computed properties. |
| flow_events created_at | flow_store.py / routers/flow.py | DB column is `created_at` not `timestamp`. Was silently returning empty rows (Supabase 400/42703). Pydantic FlowEventRaw maps `created_at → timestamp`. |
| influence_tier column | flow_store.py | DB column is `influence_tier` not `tier`. Fixed in query SELECT and filter params. |
| Sweep upgrade PostgREST | flow_store.py | `upgrade_to_sweep_in_db()` PostgREST SQL expression was invalid syntax. Fixed to use compatible filter form. |
| ALERT-LEVEL bus listener | flow_store.py | `_bus_signal_listener` was reading `recommendation` (BUY/SELL/HOLD) for `alert_level`. Fixed to read `sig.get("alert_level")`. |
| WS 403 auth stop | frontend/hooks/useSignalStream | Detects close code 403 and stops retry loop on auth failure. Prevents infinite reconnect on expired token. |
| pydantic-settings env | backend/config.py | Removed `os.environ.get()` wrappers. pydantic-settings reads env vars directly from environment. |
| DEDUP-KWARGS | tradier_stream.py | `is_duplicate()` first param is positional — keyword form raised `TypeError`. Fixed to pass `occ_symbol` positionally. |
| H4 sweep dispatch TTL | tradier_stream.py | `_sweep_upgrade_dispatched` Set grew forever. Changed to `dict[str, float]` with 30-min TTL eviction. |
| Gate 2 retrigger | repetition_accumulator.py | `last_signaled_premium` on `RepetitionEpisode`. Re-emit only when Δ ≥ $50k. Kills QQQ/SPY signal spam. |
| DEDUP (U-1) | universe_store.py | `_sync_save_snapshot()` reuses existing `snapshot_id` if <24h old and symbol count within ±30% (DEDUP-2). |
| Migration 013 | migrations/ | `UNIQUE(snapshot_id, symbol)` on `options_universe_symbols` + chain cache. |
| C-003 sweep upgrade | flow_store.py | Retroactive `PATCH` to set `trade_type='SWEEP'` on rows already written as BTO. |
| D-003 | stream_manager.py | Worker count changed from hard-coded 32 to `ceil(registry.size() / 500)`. |
| B-021 | stream_manager.py | Workers staggered at N × 200ms on spawn. |
| STREAM-1/2/3 | stream_manager.py | Shared session token; single StreamManager with dynamic batching; removed global session lock. |
| C-020 | utils/dedup.py | Dedup TTL clock changed from `time.time()` to `time.monotonic()`. |
| H1 | symbol_registry.py | `build()` returns `tuple[int, dict[str, dict]]` — (count, raw_quotes). Callers unpack; `_post_build_upsert` reuses raw_quotes, skipping duplicate Tradier call. |
| H3 | symbol_registry.py | Module-level imports + `get_option_chain_bulk` replaces `get_option_chain`. Incremental warm-restart build: only re-fetches tickers with expired contracts (min_dte == 0). |
| B-ZERO-PRICE | symbol_registry.py | If `_fetch_stock_prices()` returns 0 prices, `build()` activates `zero_price_fallback=True`: ATM filter bypassed so chain fetches still run. Was silently building 0 OCC contracts. |
| D-001 | tradier_stream.py | `stream_options_flow()` accepts `registry=` from lifespan, skipping duplicate `build()`. |
| D-002 | tradier_stream.py | `refresh_loop()` only spawned in standalone path; lifespan owns it in production. |
| M-1/M-2 | symbol_registry.py / main.py | `_build_complete` flag: `is_ready()` returns `_build_complete` not `len(registry) > 0`. Stream workers spawn only after `build()` fully completes with fresh Tradier data. |
| M-3 | main.py | `_post_build_upsert` split into three explicit phases. `assign_tiers()` failure is caught and re-raised; `upsert_symbol_quotes()` is skipped if tiers fail. |
| STREAM-5 | main.py | Graceful shutdown: `stream_task` cancelled and awaited FIRST so Tradier HTTP connections close cleanly before process exits, freeing session quota for next container start. |
| RC-3 | main.py | `validate_ingestion_config()` at startup warns on missing DB rows. |
| FLOW-DEBUG | tradier_stream.py | INFO-level gate logging at every drop point. `_stats` tracks `parsed_count`, `accumulator_gated`, `parse_failed`. |
| FIRST-TICK | tradier_stream.py | First 5 ticks logged individually at INFO. |

---

## Open Issues

| ID | Component | Description |
|----|-----------|-------------|
| B-026 | routers/ws.py | WebSocket broadcast iterates subscriber list without copy. Concurrent connect/disconnect can mutate the list mid-loop. A failed `send()` to one client may not isolate other subscribers. No regression test. |
| B-028 | stream_manager.py | `refresh()` called while workers are mid-stagger spawn can race with `_spawn_workers()`. Diff logic may produce duplicate worker tasks. |
| B-029 | repetition_accumulator.py | Rolling-window `prune()` can evict events concurrently with Gate 2 delta evaluation. Prune mid-evaluation may cause the retrigger to miscalculate Δ premium. |
| B-030 | universe_store.py | `save_snapshot()` partial write on DB connection drop is not rolled back. A partial snapshot passes the `load_fresh_snapshot` age check on next restart. |
