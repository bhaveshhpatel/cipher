# Cipher — Architecture & Data Flow

> Last updated: 2026-04-28 (STREAM-1/2/3 parallel workers, shared session token, snapshot dedup idempotency, accumulator retrigger gate, dense telemetry, options_universe_symbols unique constraint)

---

## Overview

Cipher is an institutional options flow intelligence platform. It monitors live Tradier WebSocket streams across a tier-filtered OCC symbol universe, classifies each trade tick through a 6-layer pipeline, detects repetition patterns, runs a composite signal engine, and surfaces high-conviction signals to the frontend via WebSocket — persisting all events and signals to Supabase for historical querying.

At runtime the active worker count is `ceil(registry.size() / 500)` — typically 60–70 workers for a full universe of ~31,920 OCC symbols. All workers share a single Tradier session token and stream in parallel, each covering ≤500 symbols simultaneously.

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
│  - Instantiates StreamManager with the registry and hands off    │
│    _process_trade as the per-event callback.                     │
│  - Waits for registry._build_complete before calling manager.run │
│  - Spawns registry.refresh_loop() as a background task.          │
│  - If TRADIER_API_KEY is unset: enters idle mode. Auto demo      │
│    fallback is DISABLED as of 2026-04-25 — use admin panel.      │
│                                                                  │
│  StreamManager (services/stream_manager.py):                     │
│                                                                  │
│  STREAM-1 (2026-04-28):                                          │
│  _respawn_workers() fires every _worker_refresh_s (default 300s) │
│  to stay in sync with registry.refresh_loop() rebuilds. Also     │
│  called immediately when a worker signals token expiry.          │
│                                                                  │
│  STREAM-2 (2026-04-28):                                          │
│  Tradier rejects stream POSTs with >500 symbols. Fix:            │
│  _CHUNK_SIZE=500, ONE shared session token fetched once by the   │
│  manager, passed to all workers as shared_session_token.         │
│  Workers skip their own get_session_token() call entirely.       │
│                                                                  │
│  STREAM-3 (2026-04-28):                                          │
│  asyncio.Lock removed. All workers run fully in parallel.        │
│  Tradier's "1 concurrent session" rule = 1 sessionid active at   │
│  a time, NOT 1 open TCP connection. Workers sharing the same     │
│  sessionid each hold their own open POST stream concurrently —   │
│  all symbols covered simultaneously from T+0.                    │
│                                                                  │
│  Current architecture:                                           │
│  - 1 session token fetched at spawn time, shared to all workers  │
│  - ~64 workers × 500 symbols = ~31,920 OCC symbols in parallel   │
│  - 50ms staggered startup between worker spawns (thundering-herd │
│    protection on Tradier's connection endpoint)                  │
│  - asyncio.Queue(maxsize=50_000) buffers ticks from all workers  │
│  - Single _consume_queue() task drains the queue and calls       │
│    _process_trade() serially                                     │
│  - _health_loop() logs STREAM_HEALTH every 30s: total_ticks,     │
│    active_workers, stalled_workers, never_ticked, rate/s,        │
│    errors, reconnects, queue_depth, uptime                       │
│  - Token expiry: any worker sets _token_expired=True; manager    │
│    detects within 60s → full token refresh + full respawn        │
│  - On registry refresh: _respawn_workers() diffs old vs new      │
│    symbol set; always refreshes token on respawn                 │
│                                                                  │
│  Per-worker telemetry (stream_worker.py):                        │
│  - STREAM_STATS log every 30s per worker:                        │
│    symbols, ticks, ticks_30s, rate/s, errors, reconnects,        │
│    uptime, last_tick_ago                                         │
│  - FIRST_TICK log (full untruncated) per worker on every fresh   │
│    connect                                                       │
│  - Explicit log when Tradier returns "too many symbols" or other  │
│    API error                                                     │
│  - _BACKOFF_BASE=1.0s, _BACKOFF_CAP=10.0s, _IDLE_TIMEOUT=15.0s  │
│                                                                  │
│  Previous startup protections (superseded by STREAM-3):          │
│  B-021 / B-022 / B-023 — stagger, semaphore, 429 handling were   │
│  removed when the shared-token + parallel model was adopted.     │
│  The 50ms per-worker spawn stagger (thundering-herd guard) is    │
│  the only startup rate-limiting that remains.                    │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 3 — Parser  (parsers/options_flow_parser.py)              │
│                                                                  │
│  CRITICAL FIX (C-015): stream sends "last" as fill price,        │
│  not "price".                                                    │
│  fill_price = float(tick["last"] or tick.get("price") or mid)    │
│  Also: size==0 guard, OCC regex expanded to {1,10} chars,        │
│  synthetic bid/ask spread when bid=ask=0 (is_synthetic_quote     │
│  tagged True), registry enrichment overrides OCC-parsed fields   │
│  with pre-validated chain metadata.                              │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 4 — Deduplication  (utils/dedup.py)                 C-019 │
│                                                                  │
│  A single trade prints on CBOE, MIAX, PHLX, AMEX within a        │
│  reporting window. OPRA exchange lag reality (2026):             │
│    CBOE:  50-200ms  (fastest, canonical print)                   │
│    MIAX:  500ms-3s  (routinely late)                             │
│    PHLX:  2-5s      (worst-case lag on sweeps)                   │
│    BATO:  1-4s      (common on large prints)                     │
│                                                                  │
│  C-019 fix (2026-04-24) — 5 bugs fixed:                          │
│  1. TTL: 2s → 5s  — covers worst-case PHLX/MIAX lag             │
│  2. Sweep window: 5s → 8s  — matches extended TTL               │
│  3. Eliminated int(ts//2) bucket boundary bug                    │
│  4. Fill key: 2dp → 1dp — absorbs ±$0.01 feed rounding          │
│  5. flow_dedup was never called in _process_trade() — fixed      │
│                                                                  │
│  C-020 fix: arrival_ts = time.time() (wall-clock, not monotonic) │
│  so TTL comparison stays valid across both sides.                │
│                                                                  │
│  Key: (occ_symbol, size, round(fill, 1))  — no time bucket       │
│  Sweep: 3+ unique exchanges within 8s → trade_type = SWEEP       │
│  Module-level singleton: flow_dedup (TTL=5s, sweep_win=8s)       │
│  Observability: dedup_stats() exposed via /health endpoint       │
│                                                                  │
│  C-003 — Retroactive sweep upgrade:                              │
│  On the duplicate path, if exchange_count just reached           │
│  sweep_min_exchanges (3), asyncio.create_task() fires            │
│  upgrade_to_sweep_in_db() — a targeted PATCH to flow_events      │
│  that retroactively sets trade_type='SWEEP' on the canonical     │
│  row. _sweep_upgrade_dispatched set prevents double-dispatch.    │
│                                                                  │
│  occ_symbol is now passed positionally to is_duplicate() — the   │
│  first positional param is event_or_occ_symbol (2026-04-28 fix). │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 5 — Accumulator + Batched DB Writes                       │
│    Accumulator: signals/repetition_accumulator.py                │
│    DB writes:   services/flow_store.py                           │
│                                                                  │
│  RepetitionAccumulator — episode-level signal gating:            │
│                                                                  │
│  An episode is keyed on (ticker, contract_type, strike, expiry). │
│  ingest_tick() applies two gates:                                │
│                                                                  │
│  Gate 1 — Threshold (persist gate, no cooldown):                 │
│    threshold_crossed = (                                         │
│        ep.trade_count >= min_trades        # default 3           │
│        or ep.total_premium >= min_premium  # default $10,000     │
│    )                                                             │
│    OR logic: single large print ≥$10k fires on tick 1.           │
│    Repeated small prints need 3 ticks. Sub-$10k retail noise     │
│    with <3 trades is filtered.                                   │
│                                                                  │
│  Gate 2 — Retrigger (2026-04-28, signal re-emission guard):      │
│    After an episode first crosses the threshold, it only         │
│    re-emits when total_premium has grown by at least             │
│    SIGNAL_RETRIGGER_THRESHOLD ($50,000) since last_signaled_     │
│    premium. Prevents QQQ/SPY episodes from spamming a new        │
│    signal_history row on every tick once threshold is crossed.   │
│    First emission: last_signaled_premium == 0 → always passes.   │
│    Subsequent: delta = total_premium - last_signaled_premium     │
│               emit only when delta >= retrigger ($50k).          │
│                                                                  │
│  get_signal() is the signal gate (separate from ingest_tick):    │
│    Returns ep only when trade_count >= 1 AND premium >= min_     │
│    premium. Called independently — persist_flow_event() is       │
│    decoupled from signal emission (C-008 fix).                   │
│                                                                  │
│  Episode lifecycle:                                              │
│  - Window: 30 min inactivity → episode evicted on next           │
│    cleanup_expired() call                                        │
│  - is_accelerating: 2+ ticks within last 5 min                   │
│  - Per-episode fields: trade_count, total_premium,               │
│    is_accelerating, timestamps, events, last_signaled_premium    │
│                                                                  │
│  flow_store.py — batched DB writes:                              │
│  Never write one row at a time. Buffer events and flush to       │
│  Supabase every 500ms OR 100 rows, whichever comes first.        │
│  Estimated: ~62K filtered rows/day → ~744 batched flushes.       │
│  Uses SUPABASE_SERVICE_ROLE_KEY (bypasses RLS).                  │
│  3-attempt retry with 1s delay on any flush failure.             │
│                                                                  │
│  flow_events writes (direct buffer path):                        │
│  persist_flow_event() called from _process_trade() on every      │
│  qualifying tick (above Gate 1 threshold). Appends to            │
│  _flow_event_buffer; early-flushes when buffer hits 100 rows.    │
│  The background _flush_flow_events() loop drains every 500ms.    │
│                                                                  │
│  flow_episodes writes (bus path):                                │
│  _bus_signal_listener() subscribes to "db_writer" channel.       │
│  On composite_signal messages only — one row per qualifying       │
│  repetition episode that crossed the signal threshold.           │
│  Written immediately (no buffering) via _insert_rows().          │
│                                                                  │
│  FIX (2026-04-24): _FLUSH_INTERVAL was 5s. Fixed to 0.5s +      │
│  _FLUSH_MAX_ROWS=100 early-flush.                                │
│  FIX (C-002): persist_flow_event() moved after ingest_tick() so  │
│  only threshold-passing ticks write to flow_events.              │
│  FIX (C-008): ingest_tick() (persist gate) and get_signal()      │
│  (signal gate) are called independently — tick 4-N writes to     │
│  flow_events even when the 5-min signal cooldown is active.      │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 6 — Supabase Realtime + TierEngine          Feature 4A    │
│                                                                  │
│  Realtime: Zero extra work. Supabase auto-broadcasts every       │
│  INSERT to subscribed frontend clients. Frontend subscribes to   │
│  flow_episodes and signal_history channels.                      │
│                                                                  │
│  signal_history writes (signal_store.py):                        │
│  _bus_signal_listener() subscribes to the "signal_writer" bus    │
│  channel. On composite_signal messages, persist_composite_signal  │
│  builds the full signal_history row (including swarm fields) and │
│  inserts via REST with 3-attempt retry. In-memory fallback:      │
│  _signal_memory deque(maxlen=1000) stores signals when Supabase  │
│  is unreachable, preventing loss during transient outages.       │
│                                                                  │
│  TierEngine (services/tier_engine.py):                           │
│    assign_tiers(symbols) → returns tier_map dict[str,int]        │
│    and upserts tier + open_interest + average_volume onto        │
│    options_universe_symbols.                                     │
│    Thresholds loaded from tier_thresholds (is_active=true row)   │
│    and cached for CACHE_TTL (default 300s).                      │
│    Admin whitelist (TIER_ADMIN_WHITELIST env) forces symbols to  │
│    Tier 1 regardless of metrics.                                 │
│    Called by main.py lifespan after OI stamp and on each         │
│    background universe refresh.                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## Layer 2 — Stream Architecture (Current State)

| Item | Value | Notes |
|------|-------|-------|
| Session token model | 1 shared token | Fetched once by manager, passed to all workers |
| Worker count | ~64 | `ceil(registry.size() / 500)` |
| Symbols per worker | ≤500 | Tradier hard limit per POST |
| Total symbols covered | ~31,920 | All streaming in parallel from T+0 |
| Worker spawn stagger | 50ms × worker index | Thundering-herd guard only |
| Concurrency model | Fully parallel | Lock removed (STREAM-3) |
| Queue size | 50,000 | `asyncio.Queue` buffers all worker ticks |
| Token expiry recovery | Within 60s | Any worker sets `_token_expired`; manager detects + full respawn |
| Worker refresh period | 300s | `_worker_refresh_s` — respawns when registry changes |
| Backoff | 1.0s base, 10.0s cap | Per-worker reconnect exponential backoff |
| Idle timeout | 15.0s | Stale connection detection threshold |
| Health log interval | 30s | Manager-level STREAM_HEALTH aggregate log |

**Why parallel works:** Tradier's "1 concurrent session" constraint means only 1 `sessionid` can be active at a time — but multiple POST stream connections can share the same `sessionid` simultaneously. Each worker holds its own open POST connection with the shared token and ≤500 symbols. All workers stream concurrently.

---

## Lifespan Task Inventory (`main.py`)

All tasks are created inside the `lifespan` async context manager and cancelled on shutdown.

| Task variable | Coroutine | Purpose |
|---------------|-----------|---------|
| `registry_refresh_task` | `registry.refresh_loop()` | Rebuilds OCC registry every `REGISTRY_REFRESH_MINS` (default 30 min), notifies `StreamManager` to respawn workers on symbol-set change |
| `prewarm_task` | `_registry_prewarm_loop()` | Rebuilds OCC registry at 9:15 AM ET every weekday — workers are warm before 9:30 market open |
| `stream_task` | `stream_options_flow(stream_symbols)` | Main Tradier WebSocket pipeline (all 6 layers) |
| `db_write_task` | `start_flow_writer()` | Batched flow_events writes (500ms/100 rows) + flow_episodes via bus |
| `signal_write_task` | `start_signal_writer()` | Persists `CompositeSignal` rows to `signal_history` via bus |
| `refresh_task` | `_universe_refresh_loop()` | Full universe rebuild every 24 h — reloads symbols, re-runs OI stamp + tier assignment, updates DB snapshot |

### Startup Sequence (blocking)

`_resolve_startup_universe()` returns a **4-tuple**: `(stream_symbols, tier_map, quotes, snapshot_id)`.

**DB-hit path** (fresh snapshot ≤ 24h old):
- Returns `(symbols, tier_map, [], snapshot_id)` — `quotes` is empty, no OI stamp needed.

**Full-load path** (no fresh snapshot):
- Calls `load_universe()` (CBOE + Tradier validate + screen).
- Calls `_fetch_batch_quotes(symbols)` separately to obtain quotes for tier assignment.
- Returns `(stream_symbols, tier_map, quotes, snapshot_id)` — `quotes` is populated.

```
1. _resolve_startup_universe()      — fresh DB snapshot (max_age 24h) or full CBOE+Tradier load
2. init_registry(watchlist, tier_map) — Layer 1 init
3. registry.load_from_db(snapshot_id) — fast-seed OCC contracts from DB chain cache (DB-hit path only)
4. registry.build()                 — first full OCC contract build (blocks lifespan until complete)
                                      sets registry._build_complete = True
5. _stamp_oi(quotes, oi_map)        — stamps open_interest on quote objects from registry
                                      (full-load path only; skipped when quotes=[])
6. assign_tiers(quotes)             — OI-informed tier assignment (T1/T2/T3)
                                      (full-load path only; skipped when quotes=[])
7. registry.set_tier_map(tier_map)  — final tier map wired into registry
8. universe_store.upsert_symbol_quotes() — open_interest + tier written to DB
```
After the blocking sequence, all 6 background tasks are spawned. `stream_options_flow()` waits on `registry._build_complete` (M-1/M-2 gate) before passing symbols to `StreamManager.run()`.

---

## Universe Snapshot Idempotency (U-1 / 2026-04-28)

`universe_store._sync_save_snapshot()` now reuses the existing active `snapshot_id` instead of generating a new `uuid4()` on every deployment, making restarts fully idempotent.

**Root cause of the old bug:** The upsert key is `(snapshot_id, symbol)`. A new `uuid4()` on every restart means on_conflict never fires → every restart inserted ~1,400 fresh rows into `options_universe_symbols` and `options_chain_cache`.

**Fix logic:**
- If an active snapshot exists **and** was fetched within `_REUSE_SNAPSHOT_AGE_H` hours (default 20h) **and** the new symbol count is within 10% of the stored count → reuse that `snapshot_id`.
- A new `snapshot_id` is only generated when: no active snapshot exists, the snapshot is >20h old, or symbol count drifted >10% (genuine universe refresh).

**Migration 013** adds a `UNIQUE(snapshot_id, symbol)` constraint to `options_universe_symbols` and `UNIQUE(snapshot_id, underlying, expiration, strike, option_type)` to `options_chain_cache` so PostgREST upserts fire correctly even if an older un-constrained schema is in place.

---

## Ingestion Observability — Per-Tick Logging

`_process_trade()` in `tradier_stream.py` now logs at INFO level at every gate so Railway surfaces stream activity from tick 1:

| Log event | When |
|-----------|------|
| Raw tick type received | Every tick (first 5 per startup, then every 100) |
| `dedup_dropped` | Tick dropped by DedupCache (count shown) |
| `parse_returned_none` | `parse_tradier_trade()` returned None |
| `accumulator_gated` | `ingest_tick()` returned None (below threshold) |
| `persist_called` | Tick passed all gates; `persist_flow_event()` invoked |
| Periodic stats (every 100 ticks) | `ticks`, `classified`, `deduped`, `signals` summary |

---

## Backend Signal Pipeline — Full Per-Tick Flow

```text
Tradier stream worker startup (Layer 2 — StreamManager)
  → Manager fetches ONE shared session token
  → Spawns ~64 workers, 50ms stagger between each
  → All workers connect in parallel (no lock)
  → Each worker: POST /v1/markets/events with shared sessionid + ≤500 symbols
  → Worker enqueues raw ticks into shared asyncio.Queue(50_000)
  → _consume_queue() drains queue → _process_trade(raw)

_process_trade(raw) in tradier_stream.py
       ├── unwrap envelope: raw["timesale"] payload
  → parse_tradier_trade()                           Layer 3
       ├── fill_price = tick["last"] or tick.get("price") or mid
       ├── size==0 guard → return None (skip)
       ├── OCC regex {1,10} — ticker/strike/expiry/type
       ├── synthetic spread when bid=ask=0  (is_synthetic_quote=True)
       └── registry enrichment → override with chain metadata
  → DedupCache.is_duplicate(occ_symbol, ...)        Layer 4  C-019/C-020
       ├── key: (occ_symbol, size, round(fill, 1))
       ├── TTL: 5s — covers PHLX/MIAX worst-case lag
       ├── arrival_ts: time.time() (wall-clock)
       ├── exchange: trade_payload["exch"] or ["exchange"]
       ├── duplicate (same trade, slower exchange):
       │     _stats["deduped"] += 1
       │     C-003: if exch_count == sweep_min → create_task(upgrade_to_sweep_in_db())
       │     return (no persist, no accumulator)
       ├── canonical → check is_sweep()
       └── 3+ unique exchanges within 8s → trade_type = SWEEP + exchange_count
  → RepetitionAccumulator.ingest_tick(ev)           Layer 5 — Gate 1 + Gate 2
       ├── Gate 1 (threshold):
       │     trade_count >= 3 OR total_premium >= $10,000
       │     → None returned if not crossed
       ├── Gate 2 (retrigger, 2026-04-28):
       │     first emission: last_signaled_premium == 0 → pass
       │     subsequent: delta = total_premium - last_signaled_premium
       │                 emit only when delta >= $50,000
       │     → None returned if delta < $50k
       └── returns persist_ep (RepetitionEpisode) if both gates pass
  → persist_flow_event()                            Layer 5 (C-008)
       ├── called on persist_ep (every qualifying tick — Gate 1 only)
       ├── appends to _flow_event_buffer
       ├── early-flush if buffer ≥ 100 rows
       └── background _flush_flow_events() drains every 500ms (3-retry)
  → RepetitionAccumulator.get_signal(ts, persist_ep)
       ├── returns sig_ep if trade_count >= 1 AND premium >= min_premium
       └── None if persist_ep is None
  → build_composite(sig_ep, accumulator)            (called only when sig_ep is not None)
       ├── compute_flow_score()              × 0.55
       │     premium (capped $10M) + acceleration + trade count
       ├── get_backtest_score()              × 0.35
       │     historical win-rate by ticker/type/DTE/tier
       └── volume_weighted_premium_factor()  × 0.10
             total_premium / (OI × 100), capped 0–1, 0.5 if OI absent
  → bus.publish_all()  (only when sig_ep passes retrigger gate)
       ├── "signal" message      → "signals" channel → ws.py → WebSocket clients
       └── "composite_signal" message → fan-out:
             ├── "db_writer" channel → flow_store._bus_signal_listener()
             │     → persist_flow_episode() → flow_episodes row
             └── "signal_writer" channel → signal_store._bus_signal_listener()
                   → persist_composite_signal() → signal_history row

NOTE — SwarmEngine (services/swarm_engine.py):
  The Groq-backed AI swarm is NOT called automatically per tick.
  It is available as a standalone service callable from routers or
  simulation endpoints. signal_history rows have swarm_* fields
  (direction, confidence, votes, agents JSONB) which are populated
  only when persist_composite_signal() receives a signal dict that
  includes swarm results from an explicit swarm run.
```

### Composite Score Weights

| Component | Weight | Source |
|-----------|--------|--------|
| `flow_score` | 0.55 | Premium size, acceleration, trade count |
| `backtest_score` | 0.35 | Historical win-rate (ticker/type/DTE/tier) |
| `volume_premium_factor` | 0.10 | Premium relative to open interest |

**Recommendation threshold:** composite ≥ 0.65 → BUY (bullish) or SELL (bearish)

---

## CORS Configuration (`main.py`)

`allow_origins=["*"]` is **never used** — it breaks `allow_credentials=True`.
Instead, `allow_origin_regex` accepts a combined pattern:

```
https://[a-zA-Z0-9\-]+\.vercel\.app   ← all Vercel production + preview deploys
http://localhost:(3000|3001)           ← local dev
http://127\.0\.0\.1:3000              ← local dev alternative
<escaped explicit origins from CORS_ALLOWED_ORIGINS env var>
```

`CORSMiddleware` is configured with `allow_credentials=True`, `allow_methods=["*"]`, `allow_headers=["*"]`, `expose_headers=["*"]`.

---

## System Components

```text
┌─────────────────────────────────────────────────────────────────┐
│                        Railway (Backend)                        │
│                                                                 │
│  main.py (FastAPI lifespan)                                     │
│    │                                                            │
│    │  BLOCKING STARTUP SEQUENCE                                 │
│    ├── _resolve_startup_universe()  4-tuple: symbols/tier_map/quotes/snapshot_id │
│    ├── init_registry()              Layer 1 init                │
│    ├── registry.load_from_db()      DB chain fast-seed          │
│    ├── registry.build()             first OCC contract build    │
│    │     └── sets _build_complete = True                        │
│    ├── _stamp_oi(quotes, oi_map)    open_interest → quotes      │
│    ├── assign_tiers(quotes)         OI-informed T1/T2/T3        │
│    ├── registry.set_tier_map()      wire final tier map         │
│    └── universe_store.upsert_symbol_quotes()  OI+tier → DB      │
│                                                                 │
│    BACKGROUND TASKS (asyncio)                                   │
│    ├── registry.refresh_loop()      rebuild OCC every 30 min    │
│    ├── _registry_prewarm_loop()     rebuild at 9:15 AM ET daily │
│    ├── stream_options_flow()        Tradier WS pipeline          │
│    │     └── waits for _build_complete before manager.run()     │
│    ├── start_flow_writer()          batched DB writes (L5)       │
│    ├── start_signal_writer()        signal_history writes (L6)   │
│    └── _universe_refresh_loop()     full universe refresh 24h    │
│                                                                 │
│  Stream Pipeline (per tick — tradier_stream._process_trade)     │
│    ├── SymbolRegistry (Layer 1)  services/symbol_registry.py    │
│    │     ├── O(1) OCC lookup                                    │
│    │     ├── tier_map from tier_thresholds DB (cached 300s)     │
│    │     └── ATM/DTE params per tier                            │
│    ├── StreamManager (Layer 2)   services/stream_manager.py     │
│    │     ├── 1 shared session token (fetched by manager)        │
│    │     ├── ~64 workers × 500 symbols (parallel, no lock)      │
│    │     ├── 50ms spawn stagger per worker                      │
│    │     ├── asyncio.Queue(50_000) shared tick buffer           │
│    │     ├── _consume_queue() → _process_trade() serial          │
│    │     ├── STREAM_HEALTH log every 30s                         │
│    │     └── token expiry → refresh + full respawn within 60s   │
│    ├── parse_tradier_trade()  Layer 3  (parsers/options_flow_parser.py) │
│    │     ├── fill_price: tick["last"] (not "price")             │
│    │     ├── size==0 guard → skip                               │
│    │     ├── OCC regex {1,10} + synthetic spread                │
│    │     └── is_synthetic_quote tagged when bid=ask=0           │
│    ├── DedupCache.is_duplicate()  Layer 4  C-019/C-020          │
│    │     ├── occ_symbol passed positionally                     │
│    │     ├── TTL=5s key=(occ_symbol, size, fill_1dp)            │
│    │     ├── arrival_ts = time.time() (wall-clock)              │
│    │     ├── is_sweep() → 3+ exchanges within 8s               │
│    │     └── C-003: retroactive upgrade_to_sweep_in_db()        │
│    ├── RepetitionAccumulator  (signals/repetition_accumulator.py) │
│    │     ├── ingest_tick() Gate 1: min_trades=3 OR min_premium=$10k │
│    │     ├── ingest_tick() Gate 2: retrigger=$50k new premium   │
│    │     │     → suppress re-emission until +$50k delta         │
│    │     └── get_signal() → sig_ep (trade_count>=1 AND premium>=min) │
│    ├── persist_flow_event()  Layer 5  (services/flow_store.py)  │
│    │     └── buffered write to flow_events (Gate 1 only)        │
│    └── build_composite()  (signals/composite_signal_engine.py)  │
│          ├── flow×0.55 + backtest×0.35 + vol×0.10               │
│          └── bus.publish_all() on sig_ep only                   │
│                                                                 │
│                AsyncEventBus (in-memory fan-out)                │
│                  core/async_bus.py                              │
│                  ├── "signals"       → ws.py → WS clients       │
│                  ├── "db_writer"     → flow_store._bus_signal_listener() │
│                  │     └── persist_flow_episode() on composite_signal │
│                  └── "signal_writer" → signal_store._bus_signal_listener() │
│                        └── persist_composite_signal() on composite_signal │
│                                                                 │
│  FastAPI Routers                                                │
│    ├── /api/auth                  auth.py                       │
│    ├── /api/flow/scan             flow.py                       │
│    ├── /api/simulate              simulation.py                 │
│    ├── /ws/signals                ws.py (ping/pong heartbeat)   │
│    ├── /api/signals/composite     smart_signals.py              │
│    ├── /api/signals/list          smart_signals.py              │
│    ├── /api/signals/history       history.py                    │
│    ├── /admin/tier-thresholds     admin.py  (PATCH/GET)         │
│    ├── /admin/tier-distribution   admin.py  (GET)               │
│    └── /health/stream             health.py  (B-008)            │
│                                                                 │
│  Health / Alias Endpoints (main.py — not routers)              │
│    ├── GET /stream/stats     → {status:"ok"}  Railway probe     │
│    ├── GET /api/stream/stats → stream_stats() (auth required)   │
│    ├── GET /api/health       → {status:"ok", service:"..."}     │
│    ├── GET /health           → {status:"ok", service:"..."}     │
│    └── GET /                 → {message:"Cipher API v1.0 ..."}  │
└─────────────────────────────────────────────────────────────────┘
                              │
                Supabase Realtime (Layer 6)
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Supabase (PostgreSQL)                        │
│   flow_episodes · flow_events · options_universe_snapshots      │
│   options_universe_symbols · signal_history · auth.users        │
│   tier_thresholds · options_chain_cache                         │
│                                                                 │
│   Key constraints (migration 013):                              │
│   UNIQUE(snapshot_id, symbol)  — options_universe_symbols       │
│   UNIQUE(snapshot_id, underlying, expiration, strike, opt_type) │
│                               — options_chain_cache             │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Vercel (Frontend)                            │
│   Next.js 14, TypeScript, Tailwind CSS                          │
│   Supabase Realtime subscription — zero-latency INSERT push     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Demo Mode — DISABLED as Automatic Fallback

As of **2026-04-25**, `_demo_mode()` is no longer invoked automatically when the Tradier API key is missing or the live stream fails. The backend enters **idle mode** and logs a warning. To run synthetic demo signals, use the **admin panel** at `/admin`.

The `_demo_mode_once()` and `_demo_mode()` functions are preserved in `tradier_stream.py` for future use and can be re-enabled by uncommenting the call sites in `stream_options_flow()`.

---

## TierEngine — Feature 4A

### Tier Definitions

| Tier | Label | Min Avg Volume | Min Last Price | Min OI | ATM Strike Range | Max DTE |
|------|-------|---------------|---------------|--------|-----------------|----|
| 1 | Liquid large-cap | ≥ 20M | ≥ $10.00 | ≥ 1,000 | ±20% | 90 |
| 2 | Mid-cap | ≥ 2M | ≥ $10.00 | ≥ 500 | ±15% | 60 |
| 3 | Standard (default) | ≥ 500K | ≥ $1.00 | ≥ 100 | ±10% | 30 |

Thresholds are stored in `tier_thresholds` (the `is_active = true` row) and cached for 300 seconds. Admins can update them live via `PATCH /admin/tier-thresholds` without redeployment.

### Admin Whitelist

Symbols in `TIER_ADMIN_WHITELIST` env var (default: SPY, QQQ, AAPL, TSLA, NVDA, MSFT, AMZN, META, GOOGL, AMD, PLTR, COIN) are always assigned Tier 1 regardless of volume thresholds.

### Admin endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/admin/tier-thresholds` | `GET` | Admin JWT | Read active threshold row |
| `/admin/tier-thresholds` | `PATCH` | Admin JWT | Update thresholds live (no redeploy) |
| `/admin/tier-distribution` | `GET` | Admin JWT | Count of symbols per tier |

---

## Stream Health Observability — `_stats` dict (`tradier_stream.py`)

| Key | Type | Notes |
|-----|------|-------|
| `active_symbols` | int | OCC contract count (registry.size() after build) |
| `ticks` | int | Total raw events received |
| `classified` | int | Events that passed parse + dedup |
| `deduped` | int | Events dropped by DedupCache |
| `accumulator_gated` | int | Events dropped by RepetitionAccumulator (below Gate 1 or Gate 2) |
| `signals` | int | Episodes that passed both accumulator gates |
| `errors` | int | DB timeout / persist failures |
| `composite_errors` | int | `build_composite()` failures |
| `reconnects` | int | Stream reconnect attempts |
| `mode` | str | `"starting"` / `"live"` / `"idle"` / `"demo"` |
| `last_tick_at` | float | Wall-clock epoch of most recent classified tick |
| `last_reconnect_at` | float | Wall-clock epoch of most recent reconnect |
| `uptime_seconds` | float | Computed in `get_stats()` from `_stream_start_at` |
| `dedup_cache_size` | int | From `flow_dedup.dedup_stats()` |
| `sweep_candidates` | int | From `flow_dedup.dedup_stats()` |

`get_stats()` merges `_stats` with `flow_dedup.dedup_stats()` and exposes at `GET /health/stream`.

---

## WebSocket Heartbeat

Railway terminates idle TCP connections. The WS router runs a full ping/pong loop:

| Event | Details |
|-------|---------|
| Server → client ping | `{"type":"ping"}` every **25 seconds** |
| Client → server pong | `{"type":"pong"}` expected within **10 seconds** |
| Pong timeout | Server closes with code `1001` |
| Invalid JWT | Server closes with code `4001` |

---

## AI Swarm — Phase 5A

The swarm engine is available but is **not called automatically** on every signal tick.

| Setting | Value |
|---------|-------|
| Provider | Groq `llama-3.3-70b-versatile` |
| Agent counts | 3, 6, 9, 12 — set via `SWARM_N_AGENTS` env var |
| Tier 1 agents (1–6) | Momentum, Contrarian, Fundamental, Technical, Macro, Risk |
| Tier 2 agents (7–9) | Options Flow Specialist, Quant/Stat Arb, Sentiment |
| Tier 3 agents (10–12) | Sector Rotation, Volatility Trader, Dark Pool/Tape Reader |
| Ensemble | Majority vote → `bull_votes`, `bear_votes`, `hold_votes`, `confidence` |
| Fallback | All agents return HOLD when `GROQ_API_KEY` not set |

---

## Signal History — Phase 4

### `signal_history` Table (with Phase 5A swarm fields)

```sql
CREATE TABLE signal_history (
  id                    BIGSERIAL PRIMARY KEY,
  ticker                TEXT NOT NULL,
  recommendation        TEXT NOT NULL,
  composite_score       NUMERIC NOT NULL,
  flow_score            NUMERIC NOT NULL,
  backtest_score        NUMERIC NOT NULL,
  volume_premium_factor NUMERIC,
  reasoning             TEXT,
  contract_type         TEXT,
  alert_level           TEXT NOT NULL,
  sentiment             TEXT NOT NULL,
  direction             TEXT NOT NULL,
  influence_tier        TEXT NOT NULL,
  premium               NUMERIC NOT NULL,
  trade_type            TEXT NOT NULL,
  is_golden_sweep       BOOLEAN NOT NULL DEFAULT false,
  total_premium         NUMERIC,
  trade_count           INT,
  swarm_direction       TEXT,
  swarm_confidence      NUMERIC,
  swarm_agents          JSONB,
  swarm_bull_votes      INT,
  swarm_bear_votes      INT,
  swarm_hold_votes      INT,
  signal_ts             TIMESTAMPTZ DEFAULT now(),
  created_at            TIMESTAMPTZ DEFAULT now()
);
```

`swarm_*` fields are nullable — populated only when a swarm run result is present in the `composite_signal` message.

---

## Smart Signals Endpoints

### `GET /api/signals/history`

| Param | Type | Default | Constraints |
|-------|------|---------|-------------|
| `ticker` | string | — | 1–10 chars |
| `direction` | string | — | `bullish` / `bearish` / `neutral` |
| `tier` | string | — | `whale` / `institutional` / `large` / `retail` |
| `min_conviction` | float | 0.0 | 0.0–1.0 |
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | ≥0 |

### `GET /api/signals/list`

| Param | Type | Default | Constraints |
|-------|------|---------|-------------|
| `page` | int | 1 | ≥1 |
| `page_size` | int | 20 | 1–100 |
| `direction` | string | — | `bullish` / `bearish` / `neutral` |
| `tier` | string | — | `whale` / `institutional` / `large` / `retail` |
| `min_conviction` | float | 0.0 | 0.0–1.0 |

### `GET /api/signals/composite/{ticker}`

Single-ticker composite. Returns `volume_premium_factor`, `swarm_direction`, `swarm_confidence`.

---

## 6-Layer Gap Fixes (Audit Log)

| Layer | Fix ID | File | What Was Wrong | Fix |
|-------|--------|------|----------------|-----|
| **L1** | M-1/M-2 | `main.py` + `symbol_registry.py` | `stream_options_flow()` could hand symbols to `StreamManager` before `registry.build()` completed, causing workers to connect with an empty or partial OCC set. | Added `registry._build_complete` flag. `stream_options_flow()` awaits it before calling `manager.run()`. |
| **L2** | STREAM-1 | `stream_manager.py` | `_respawn_workers()` was never called on a schedule — registry rebuilds updated the OCC set but workers kept streaming the old symbol list. | Added `_worker_refresh_s` (default 300s) loop in `run()`; `_respawn_workers()` fires on timer and on registry change. |
| **L2** | STREAM-2 | `stream_manager.py` + `stream_worker.py` | `_CHUNK_SIZE` was temporarily set to 50,000 (single-worker mode), sending all 31,920 symbols in one POST. Tradier rejects >500 symbols per POST with "too many symbols". | Reset `_CHUNK_SIZE=500`. Manager fetches ONE shared session token and passes it to every worker via `shared_session_token`. Workers skip their own token fetch. |
| **L2** | STREAM-3 | `stream_manager.py` + `stream_worker.py` | A global `asyncio.Lock` was added to enforce "1 connection at a time" — misunderstanding Tradier's constraint. Only 1 worker could stream at a time; the others queued, producing near-zero throughput. | Lock removed. All workers run fully in parallel. Tradier allows multiple POST streams sharing the same `sessionid` simultaneously. |
| **L2** | 2026-04-24 | `tradier_stream.py` | `registry.refresh_loop()` rebuilt the registry every 30 min but never called `manager.refresh()`. Workers kept streaming stale OCC symbols. | Replaced with `_registry_refresh_with_manager_notify()` → `await manager.refresh()` after every rebuild. |
| **L2** | B-021 | `stream_worker.py` | (Superseded by STREAM-3) Cold boot started all workers immediately. | Now handled by 50ms per-worker spawn stagger in manager. |
| **L2** | B-022/B-023 | `stream_worker.py` | (Superseded by STREAM-2/3) Per-worker session token fetches + 429 handling. | Shared token model eliminates per-worker token fetches entirely. |
| **L2** | TRADIER_STREAM_URL | `config.py` | `settings.TRADIER_STREAM_URL` was missing — workers were POSTing to `api.tradier.com` instead of `stream.tradier.com`. | Added `TRADIER_STREAM_URL` to `Settings` with default `https://stream.tradier.com`. |
| **L3** | C-015 | `options_flow_parser.py` | Stream sends `"last"` as fill price, not `"price"`. | `fill_price = tick["last"] or tick.get("price") or mid`. OCC regex broadened. Synthetic bid/ask when bid=ask=0. |
| **L4** | orig | `tradier_stream.py` | `DedupCache` built and tested but never imported or called in `_process_trade()`. | Added `from utils.dedup import flow_dedup` + gate before every `persist_flow_event()` call. |
| **L4** | C-019 | `utils/dedup.py` | TTL=2s, `int(ts//2)` bucket boundary bug, fill key 2dp, exchange never passed. | TTL→5s, sweep_win→8s, pure first-seen TTL, fill key 1dp, `"exch"/"exchange"` fallback. |
| **L4** | C-020 | `tradier_stream.py` | `arrival_ts` used `time.monotonic()` — TTL comparison mixed monotonic and wall-clock, entries never expired. | `arrival_ts = time.time()` (wall-clock). |
| **L4** | C-003 | `tradier_stream.py` + `flow_store.py` | Canonical row written as `'BTO'`; sweep confirmation via duplicate exchange ticks never upgraded the DB row. | On duplicate path: if `exch_count == sweep_min`, fire `create_task(upgrade_to_sweep_in_db())`. |
| **L4** | occ_positional | `tradier_stream.py` | `flow_dedup.is_duplicate()` was called with `occ_symbol` as a keyword arg; the parameter is positional `event_or_occ_symbol`. | Changed to positional call. |
| **L5** | C-002 | `tradier_stream.py` | `persist_flow_event()` was called before `accumulator.ingest()`, writing every dedup-passing tick regardless of threshold. | `persist_flow_event()` moved after `ingest_tick()` — only Gate 1-passing ticks write to `flow_events`. |
| **L5** | C-008 | `tradier_stream.py` | `ingest()` returned None during cooldown — `persist_flow_event()` was also suppressed. Ticks 4-N never wrote to `flow_events`. | Decoupled: `ingest_tick()` (persist gate) vs `get_signal()` (signal gate) called independently. |
| **L5** | retrigger | `repetition_accumulator.py` | Active episodes re-emitted a signal row on every single tick once threshold was crossed (QQQ/SPY spamming hundreds of `signal_history` rows per session). | Added Gate 2: `last_signaled_premium` field on `RepetitionEpisode`; re-emit only when `total_premium` grows by ≥$50k since last emission. |
| **L5** | flush | `flow_store.py` | `_FLUSH_INTERVAL = 5` (5 seconds). ~430 rows buffered per flush. | `_FLUSH_INTERVAL = 0.5` + `_FLUSH_MAX_ROWS = 100` early-flush. |
| **L6** | 4A | `tier_engine.py` | `options_universe_symbols` had no tier column — every symbol defaulted to Tier 3. | Added `tier_engine.py` with dynamic threshold-driven assignment. `tier_thresholds` admin table. |
| **L6** | B-008 | `stream_worker.py` | `errors`, `reconnects`, `last_reconnect_at` never updated — `/health/stream` always returned zeros. | Added `_inc_global_error()` / `_inc_global_reconnect()` helpers. |
| **DB** | U-1 | `universe_store.py` | New `uuid4()` on every restart — upsert on_conflict never fired → exponential row growth in `options_universe_symbols`. | `_sync_save_snapshot()` reuses existing active `snapshot_id` if <20h old and symbol count within 10%. |
| **DB** | mig-013 | `migrations/` | No `UNIQUE` constraint on `options_universe_symbols(snapshot_id, symbol)` — PostgREST upserts fell back to plain INSERT. | Migration 013 adds unique constraints on both `options_universe_symbols` and `options_chain_cache`. |
| **startup** | 4-tuple | `main.py` | `_resolve_startup_universe()` returned 3-tuple. `snapshot_id` not available for DB chain fast-seed. | Changed to 4-tuple `(stream_symbols, tier_map, quotes, snapshot_id)`. |

---

## Supabase Tables

| Table | Writer | Key Used | Notes |
|-------|--------|----------|-------|
| `flow_episodes` | `flow_store._bus_signal_listener()` | SERVICE_KEY | One row per qualifying repetition episode; written on `composite_signal` bus event |
| `flow_events` | `flow_store.persist_flow_event()` | SERVICE_KEY | Batched writes (500ms/100 rows, 3-retry); one row per Gate-1-qualifying tick |
| `signal_history` | `signal_store.persist_composite_signal()` | SERVICE_KEY | Composite signals + swarm fields; in-memory deque fallback when DB unreachable |
| `options_universe_symbols` | `universe_store.py` + `tier_engine.py` | ANON_KEY / SERVICE_KEY | Symbol quotes, stream_eligible, tier/OI/avg_vol; UNIQUE(snapshot_id, symbol) |
| `options_universe_snapshots` | `universe_store.py` | ANON_KEY | Universe snapshots; idempotent on restart (U-1 fix) |
| `options_chain_cache` | `chain_store.py` | SERVICE_KEY | OCC contract cache; UNIQUE(snapshot_id, underlying, expiration, strike, opt_type) |
| `tier_thresholds` | admin endpoint | SERVICE_KEY | Single active row; cached 300s by TierEngine |

### Supabase Critical Rules

1. **Always use `SUPABASE_SERVICE_ROLE_KEY`** for writes to `flow_episodes`, `flow_events`, `signal_history`, `tier_thresholds` — anon key fails with `42501` (RLS)
2. **Never send `id` fields** — Postgres generates them server-side
3. **No `.select()` after `.insert()`** in supabase-py v2
4. **`flow_events` is the per-tick table** — `flow_episodes` has one row per episode
5. **`options_universe_symbols` requires the UNIQUE constraint** (migration 013) for upserts to fire correctly

---

## Alert Level Logic

| Level | Criteria |
|-------|----------|
| `CONVICTION` | premium ≥ $5M, OR (accelerating AND premium ≥ $1M) |
| `STRONG_SIGNAL` | premium ≥ $1M (non-accelerating) |
| `ALERT` | premium ≥ $250K |
| `WATCH` | premium < $250K |
