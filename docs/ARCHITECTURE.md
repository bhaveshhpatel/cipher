# Cipher — Architecture & Data Flow

> Last updated: 2026-04-28 (STREAM-1/2/3 parallel workers, shared session token, snapshot dedup
> idempotency, accumulator retrigger gate, dense telemetry, alert_level fix, DEDUP-KWARGS fix,
> H4 sweep-dispatch TTL eviction, options_universe_symbols unique constraint)

---

## Overview

Cipher is an institutional options flow intelligence platform. It monitors live Tradier WebSocket
streams across a tier-filtered OCC symbol universe, classifies each trade tick through a 6-layer
pipeline, detects repetition patterns, runs a composite signal engine, and surfaces high-conviction
signals to the frontend via WebSocket — persisting all events and signals to Supabase for historical
querying.

At runtime the active worker count is `ceil(registry.size() / 500)` — typically 60–70 workers for
a full universe of ~31,920 OCC symbols. All workers share a single Tradier session token and stream
in parallel, each covering ≤500 symbols simultaneously.

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
│                                                                  │
│  Build-complete flag:                                            │
│  registry._build_complete is set True only after registry.build()│
│  finishes. stream_options_flow() waits on this flag (M-1/M-2)    │
│  before handing symbols to StreamManager, ensuring no worker     │
│  connects before the OCC contract set is fully ready.            │
└───────────────────────────────┬──────────────────────────────────┘
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
│  - Spawns one asyncio task per batch (STREAM-1/2/3...).          │
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
└───────────────────────────────┬──────────────────────────────────┘
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
│  - Compute premium = fill_price × size × 100.                    │
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
└───────────────────────────────┬──────────────────────────────────┘
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
│                                                                  │
│  C-003 — Sweep Retroactive Upgrade:                              │
│  When the 3rd unique exchange for a (occ|size|fill) key arrives  │
│  as a duplicate tick — meaning the canonical row was already      │
│  written as trade_type='BTO' — _process_trade() dispatches a     │
│  background upgrade_to_sweep_in_db() call. Issues a targeted     │
│  PATCH to flow_events: sets trade_type='SWEEP' for rows          │
│  matching (occ_symbol, fill_price, size) within the last 30s.   │
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
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 5 — Repetition Accumulator + Flow Persistence             │
│    Accumulator: signals/repetition_accumulator.py                │
│    Persistence:  services/flow_store.py                          │
│                                                                  │
│  accumulator.ingest_tick(ev) → RepetitionEpisode | None          │
│                                                                  │
│  Episode key: (ticker, contract_type, strike, expiry)            │
│  Episode window: 30 minutes of inactivity before reset.          │
│  Acceleration flag: ≥2 ticks within the last 5 minutes.          │
│                                                                  │
│  Gate 1 — Persist threshold (OR logic):                          │
│    trade_count >= min_trades (default 3), OR                     │
│    total_premium >= min_premium (default $10,000)                │
│  Below both thresholds → ingest_tick() returns None → dropped.   │
│  accumulator_gated stat incremented and logged at INFO.          │
│                                                                  │
│  Gate 2 — Signal re-emission guard (2026-04-28):                 │
│  RepetitionEpisode.last_signaled_premium tracks the total_premium │
│  at the last emission. After Gate 1 is crossed, ingest_tick()    │
│  only returns the episode again when:                            │
│    total_premium - last_signaled_premium >= SIGNAL_RETRIGGER     │
│    (default $50,000)                                             │
│  Prevents QQQ/SPY episodes from emitting a new signal_history    │
│  row on every single tick once threshold is crossed.             │
│                                                                  │
│  Flow persistence (flow_store.py):                               │
│  - flow_events table:  one row per classified tick.              │
│    Written via batched buffer: 500ms timer OR 100-row early flush.│
│    3 retries with 1s delay on Supabase failure.                  │
│    SUPABASE_SERVICE_ROLE_KEY only (bypasses RLS).                │
│  - flow_episodes table: one row per qualifying episode.          │
│    Written immediately on composite_signal bus message.          │
│    Written by _bus_signal_listener on "db_writer" channel.       │
│                                                                  │
│  ALERT-LEVEL fix (2026-04-28):                                   │
│  _bus_signal_listener was reading sig.get("recommendation")      │
│  (BUY/SELL/HOLD) for alert_level — wrong field.                  │
│  Fix: reads sig.get("alert_level") which is populated from       │
│  accumulator.get_alert_level(sig_ep) in tradier_stream.py        │
│  before the composite_signal bus publish.                        │
│                                                                  │
│  alert_level values (from accumulator.get_alert_level()):        │
│    total_premium >= $1,000,000 → CONVICTION                      │
│    total_premium >= $500,000   → STRONG_SIGNAL                   │
│    total_premium >= $200,000   → ALERT                           │
│    total_premium <  $200,000   → WATCH                           │
│                                                                  │
│  flow_events columns written:                                    │
│    ticker, contract_type, strike, expiry, dte, fill_price,       │
│    bid, ask, size, premium, trade_type, bid_ask_class,           │
│    is_aggressive, is_golden_sweep, sentiment, influence_tier,    │
│    conviction_score, exchange_count, fill_count, open_interest,  │
│    iv, underlying_price, occ_symbol, is_synthetic_quote          │
│  (No id field — Postgres uuid default generates it.)             │
│                                                                  │
│  flow_episodes columns written:                                  │
│    ticker, direction, contract_type, strike, expiry,             │
│    total_premium, trade_count, alert_level, is_accelerating,     │
│    seed_episode, signal_ts                                       │
│  (No id field — bigserial generated.)                            │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 6 — Signal Engine + Delivery                              │
│    Composite: signals/composite_signal_engine.py                 │
│    Bus:       core/async_bus.py                                  │
│    Signal DB: services/signal_store.py                           │
│    WebSocket: routers/ws.py (or routers/stream.py)               │
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
│    score >= 0.65 AND PUT  → SELL                                  │
│    score <  0.65          → HOLD                                  │
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
│               volume_premium_factor, reasoning }                 │
│    episode: { contract_type, direction, influence_tier,          │
│               total_premium, trade_count, is_accelerating,       │
│               timestamp }                                        │
│                                                                  │
│  signal_store.py (signal_writer channel):                        │
│  - Subscribes to "signal_writer" bus channel.                    │
│  - Persists composite_signal to signal_history table.            │
│  - In-memory deque fallback when DB is unavailable.              │
│  - Exposed via /api/signals/history endpoint.                    │
│                                                                  │
│  SwarmEngine (services/swarm_engine.py):                         │
│  - Groq llama-3.3 AI swarm for narrative signal reasoning.       │
│  - NOT called automatically per tick.                            │
│  - Explicit invocation only (admin panel or direct API call).    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Key Runtime Parameters

| Parameter | Value | Source |
|---|---|---|
| Session token model | 1 shared token across all workers | StreamManager |
| Tradier batch limit | ≤500 symbols per POST | StreamWorker |
| Dedup key | `(occ_symbol, size, round(fill, 1))` | DedupCache |
| Dedup TTL | 5 seconds | DedupCache |
| Sweep window | 8 seconds | DedupCache |
| Sweep min exchanges | 3 | DedupCache |
| Sweep dispatch TTL | 1800 seconds (30 min) | _sweep_upgrade_dispatched |
| Persist gate (Gate 1) | trade_count ≥ 3 OR total_premium ≥ $10k | RepetitionAccumulator |
| Signal retrigger (Gate 2) | Δ total_premium ≥ $50k since last emit | RepetitionAccumulator |
| Episode window | 30 minutes inactivity | RepetitionAccumulator |
| Acceleration threshold | ≥2 ticks within 5 minutes | RepetitionEpisode |
| DB write pattern | 500ms flush OR 100-row early flush | flow_store._flush_flow_events |
| DB retry | 3 attempts, 1s delay | flow_store._insert_rows_with_retry |
| Persist timeout | 2 seconds per event | tradier_stream._process_trade |
| Idle watchdog | 30 seconds | StreamWorker |
| Backoff | 5s base, 60s cap, jitter | tradier_stream._backoff |
| Registry refresh | 30 min (15 min on expiry days) | SymbolRegistry |
| Pre-warm time | 9:15 AM ET weekdays | main._registry_prewarm_loop |
| Stats log interval | Every 100 ticks | tradier_stream._STATS_LOG_INTERVAL |
| First-tick log count | First 5 ticks individually | tradier_stream._FIRST_TICK_LOG_COUNT |
| Demo mode | Admin panel only (disabled as auto-fallback since 2026-04-25) | tradier_stream |

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

---

## Alert Levels

| Level | Premium Threshold | Meaning |
|---|---|---|
| `CONVICTION` | ≥ $1,000,000 | Institutional-grade repeated positioning |
| `STRONG_SIGNAL` | ≥ $500,000 | High-conviction multi-fill episode |
| `ALERT` | ≥ $200,000 | Notable accumulation above noise floor |
| `WATCH` | < $200,000 | Threshold-crossing but not yet significant |

Computed by `accumulator.get_alert_level(ep)` in `tradier_stream._process_trade()` and injected
into both the `signal` bus message and the `composite_signal` bus message **before** publish.
The `flow_store._bus_signal_listener` reads `sig.get("alert_level")` — **not**
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
Migration 013 added `UNIQUE(snapshot_id, symbol)` to prevent duplicate rows on restart.
`universe_store._sync_save_snapshot()` reuses an existing active `snapshot_id` if:
- Snapshot is < 20 hours old, AND
- Symbol count is within ±10% of the current count.

### chain_store (DB cache)
OCC contract metadata cached from Tradier chain fetches. Pre-seeds the registry on warm restart
via `registry.load_from_db(snapshot_id)` before the full `build()` completes.

---

## Startup Sequence (main.py lifespan)

```text
1. start_flow_writer()      — subscribe bus "db_writer", start flush loop
2. start_signal_writer()    — subscribe bus "signal_writer"
3. init_registry()          — create SymbolRegistry with watchlist
4. universe_store snapshot  — reuse or create (idempotency check)
5. registry.build()         — async (background task)
   └─ load_from_db()        — fast pre-seed from chain_store cache
   └─ fetch Tradier chains  — fill missing contracts
   └─ set _build_complete   — unblocks stream_options_flow()
6. stream_options_flow(registry=registry)
   └─ polls registry.is_ready() every 500ms (30-min timeout)
   └─ StreamManager.run()   — spawns STREAM-1/2/3... workers
7. registry.refresh_loop()  — background refresh task (lifespan-owned)
8. _registry_prewarm_loop() — 9:15 AM ET pre-warm task
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
    ├─ flow_dedup.is_duplicate()?               → deduped++
    │     └─ exchange_count == sweep_min?       → upgrade_to_sweep_in_db() [background task]
    │                                           → RETURN
    │
    ├─ flow_dedup.is_sweep()?                   → ev.trade_type = "SWEEP"
    │
    ├─ accumulator.ingest_tick()                → classified++
    │     ├─ Gate 1 not crossed?               → accumulator_gated++ / LOG INFO / RETURN
    │     └─ Gate 2 not crossed?               → (silent: no return value) → RETURN
    │
    ├─ persist_flow_event()                     → persisted++ / buffered → flow_events
    │
    ├─ build_composite()                        → composite score
    │
    ├─ bus.publish_all(signal)                  → signals++ / WebSocket delivery
    │
    └─ bus.publish_all(composite_signal)        → flow_episodes + signal_history
```

---

## Key Fixes in This Branch

| Fix ID | File | Description |
|---|---|---|
| DEDUP-KWARGS | tradier_stream.py | `is_duplicate()` first param is positional — keyword form raised `TypeError`. Fixed to pass `occ_symbol` positionally. |
| ALERT-LEVEL | flow_store.py | `_bus_signal_listener` was reading `recommendation` (BUY/SELL/HOLD) for `alert_level`. Fixed to read `sig.get("alert_level")`. |
| H4 | tradier_stream.py | `_sweep_upgrade_dispatched` Set grew forever. Changed to `dict[str, float]` with 30-min TTL eviction. |
| Gate 2 retrigger | repetition_accumulator.py | `last_signaled_premium` on `RepetitionEpisode`. Re-emit only when Δ ≥ $50k. Kills QQQ/SPY signal spam. |
| U-1 snapshot idempotency | universe_store.py | `_sync_save_snapshot()` reuses existing `snapshot_id` if <20h old and symbol count within ±10%. |
| Migration 013 | migrations/ | `UNIQUE(snapshot_id, symbol)` on `options_universe_symbols` + chain cache. |
| C-003 sweep upgrade | flow_store.py | Retroactive `PATCH` to set `trade_type='SWEEP'` on rows already written as BTO. |
| FLOW-DEBUG | tradier_stream.py | INFO-level gate logging at every drop point. `_stats` tracks `parsed_count`, `accumulator_gated`, `parse_failed`. |
| FIRST-TICK | tradier_stream.py | First 5 ticks logged individually at INFO. Non-timesale types logged at INFO for first 10 distinct types. |
| D-001 | tradier_stream.py | `stream_options_flow()` accepts `registry=` from lifespan, skipping duplicate `build()`. |
| D-002 | tradier_stream.py | `refresh_loop()` only spawned in standalone path; lifespan owns it in production. |
| STREAM-1/2/3 | stream_manager.py | Shared session token, no lock, full parallel workers. |
