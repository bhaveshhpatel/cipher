# Cipher — Architecture & Data Flow

> Last updated: 2026-04-26 (registry prewarm loop, CORS allow_origin_regex, lifespan task inventory, dedup C-019 TTL corrections, health/stats aliases, OI stamp pipeline)

---

## Overview

Cipher is an institutional options flow intelligence platform. It monitors live Tradier WebSocket streams across a tier-filtered OCC symbol universe, classifies each trade tick through a 6-layer pipeline, detects repetition patterns, runs a multi-agent AI swarm, and surfaces high-conviction signals to the frontend via WebSocket — persisting all signals to Supabase for historical querying.

At runtime, the active stream worker count is derived from the registry size and Tradier's ~500-symbol-per-session cap. In practice this is typically 20–40 workers, with cold-start session establishment deliberately spread out to avoid Tradier session-endpoint rate bursts.

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
│  Pre-warm loop (_registry_prewarm_loop in main.py):              │
│  Fires every weekday at 9:15 AM ET (15 min before market open).  │
│  Rebuilds the full OCC contract set so workers connect instantly  │
│  at 9:30 AM with no cold-start contract-load delay.              │
│  Skipped on weekends. Non-fatal on error.                        │
│                                                                  │
│  Per-tier contract filtering:                                    │
│  Contract universe is shaped by the symbol's tier at build time. │
│  Tier params loaded from tier_thresholds DB row (cached 300s).   │
│    Tier 1 (liquid): ATM ±20%  max DTE 90  (e.g. AAPL, TSLA)      │
│    Tier 2 (mid-cap): ATM ±15%  max DTE 60                        │
│    Tier 3 (default): ATM ±10%  max DTE 30                        │
│  Unknown-tier symbols fall back to T3 params.                    │
│  ContractMeta gains a .tier field — carried through pipeline     │
│  into backtest_score (historical win-rate by ticker/type/DTE/    │
│  tier). Tier map seeded from universe_store.load_tier_map() on   │
│  warm start; updated via registry.set_tier_map() on refresh.     │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 2 — Stream Manager  (services/stream_manager.py)          │
│                                                                  │
│  Streams the tier-filtered OCC symbol set produced by Layer 1.   │
│  Symbol count is dynamic (tier ATM/DTE params drive registry     │
│  size). Tradier caps each connection at ~500 symbols; workers    │
│  are spawned as ceil(registry.size() / 500) — typically 20–40.  │
│                                                                  │
│  Startup protections (2026-04-25):                               │
│  - B-021 — Staggered cold start: worker i sleeps i×200ms before  │
│    its first session-token request (startup_delay_s, default     │
│    0.2s). Worker 0 starts immediately; worker 31 starts ~6.2s   │
│    later. Reconnects do NOT re-apply the stagger.                │
│  - B-022 — Global token semaphore: session token fetches are     │
│    guarded by a process-wide Semaphore(3), so at most 3 workers  │
│    call /markets/events/session concurrently.                    │
│  - B-023 — Explicit 429 handling: if Tradier returns HTTP 429,   │
│    the client reads Retry-After (default 10s if absent), sleeps  │
│    that duration, and retries instead of crashing.               │
│                                                                  │
│  Effective cold-start profile: ~32 workers launch over ~6.2s,   │
│  and with only 3 token fetches in flight at a time, startup      │
│  resolves in ~11 batches instead of a 32-request burst.          │
│                                                                  │
│  Each worker has its own session token (stream_worker.py).       │
│  Auto-reconnects on drop. On refresh: diffs old vs new symbol   │
│  set, restarts only affected workers — not all of them.          │
│                                                                  │
│  FIX (2026-04-24): registry refresh loop now calls               │
│  manager.refresh() after every rebuild. Previously the refresh   │
│  loop ran but never notified the manager — workers streamed      │
│  stale OCC symbols indefinitely after a 30-min rebuild.          │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 3 — Parser  (parsers/options_flow_parser.py)              │
│                                                                  │
│  CRITICAL FIX (C-015): stream sends "last" as fill price,        │
│  not "price".                                                    │
│  fill_price = float(tick["last"] or tick.get("price") or mid)    │
│  Also: size==0 guard, OCC regex expanded to {1,10} chars,        │
│  synthetic bid/ask spread when bid=ask=0, registry enrichment    │
│  overrides OCC-parsed fields with pre-validated chain metadata.  │
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
│  3. Eliminated int(ts//2) bucket boundary bug: CBOE at t=1.99s  │
│     and MIAX at t=2.01s landed in different buckets, both passed │
│     as canonical. Pure first-seen TTL comparison replaces this.  │
│  4. Fill key: 2dp → 1dp — absorbs ±$0.01 feed rounding across   │
│     exchanges without conflating genuinely different fills.      │
│  5. flow_dedup was instantiated but NEVER imported or called     │
│     in _process_trade() — Layer 4 was completely inert in        │
│     production. Fixed + exchange field now correctly passed      │
│     via "exch"/"exchange" fallback so sweep detection fires.     │
│                                                                  │
│  Key: (occ_symbol, size, round(fill, 1))  — no time bucket       │
│  Sweep: 3+ unique exchanges within 8s → trade_type = SWEEP       │
│  Module-level singleton: flow_dedup (TTL=5s, sweep_win=8s)       │
│  Observability: dedup_stats() exposed via /health endpoint       │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 5 — Batched DB Writes  (services/flow_store.py)           │
│                                                                  │
│  Never write one row at a time. Buffer events and flush to       │
│  Supabase every 500ms OR 100 rows, whichever comes first.        │
│  Estimated: ~62K filtered rows/day → ~744 batched flushes.       │
│  Uses SUPABASE_SERVICE_KEY (bypasses RLS).                       │
│                                                                  │
│  FIX (2026-04-24): _FLUSH_INTERVAL was set to 5s instead of      │
│  500ms. At 62K rows/day that's ~430 rows buffered per flush.     │
│  Fixed: _FLUSH_INTERVAL=0.5s + _FLUSH_MAX_ROWS=100 early-flush   │
│  in persist_flow_event() so 100-row batches fire immediately.    │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 6 — Supabase Realtime + TierEngine          Feature 4A    │
│                                                                  │
│  Realtime: Zero extra work. Supabase auto-broadcasts every       │
│  INSERT to subscribed frontend clients. Frontend subscribes to   │
│  flow_episodes and signal_history channels.                      │
│                                                                  │
│  TierEngine (services/tier_engine.py):                           │
│    assign_tiers(symbols) → returns tier_map dict[str,int]        │
│    and upserts tier + open_interest + average_volume onto        │
│    options_universe_symbols.                                     │
│    Thresholds loaded from tier_thresholds (is_active=true row)   │
│    and cached for TIER_THRESHOLD_CACHE_TTL_S (default 300s).     │
│    Admin whitelist (TIER_ADMIN_WHITELIST env) forces symbols to  │
│    Tier 1 regardless of metrics.                                 │
│    Called by main.py lifespan after OI stamp and on each         │
│    background universe refresh.                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## Layer 2 Startup Math

| Item | Value | Notes |
|------|-------|-------|
| Worker count example | 32 | `ceil(registry.size()/500)` |
| Per-worker cold-start delay | 200ms × worker index | B-021 |
| Last worker initial delay | 6.2s | worker 31 |
| Max concurrent token fetches | 3 | B-022 semaphore |
| 32 workers / 3 slots | 11 batches | 10 full + 1 partial |
| 429 backoff | `Retry-After` header or 10s default | B-023 |

Cold start is intentionally slower but materially safer. A fast burst that trips 429s or invalidates sessions is worse than an 8–11 second controlled bring-up.

---

## Lifespan Task Inventory (`main.py`)

All tasks are created inside the `lifespan` async context manager and cancelled on shutdown.

| Task variable | Coroutine | Purpose |
|---------------|-----------|---------|
| `registry_refresh_task` | `registry.refresh_loop()` | Rebuilds OCC registry every `REGISTRY_REFRESH_MINS` (default 30 min), notifies `StreamManager` to diff/restart affected workers |
| `prewarm_task` | `_registry_prewarm_loop()` | Rebuilds OCC registry at 9:15 AM ET every weekday — workers are warm before 9:30 market open |
| `stream_task` | `stream_options_flow(stream_symbols)` | Main Tradier WebSocket pipeline (all 6 layers) |
| `db_write_task` | `start_flow_writer()` | Batched Supabase writes — flush every 500ms or 100 rows |
| `signal_write_task` | `start_signal_writer()` | Persists `CompositeSignal` rows to `signal_history` |
| `refresh_task` | `_universe_refresh_loop()` | Full universe rebuild every 24 h — reloads symbols, re-runs OI stamp + tier assignment, updates DB snapshot |

### Startup Sequence (blocking)

```
1. _resolve_startup_universe()      — fresh DB snapshot (max_age 24h) or full CBOE+Tradier load
2. init_registry(watchlist, tier_map) — Layer 1 init
3. registry.build()                 — first OCC contract build (blocks lifespan until complete)
4. _stamp_oi(quotes, oi_map)        — stamps open_interest on quote objects from registry
5. assign_tiers(quotes)             — OI-informed tier assignment (T1/T2/T3)
6. registry.set_tier_map(tier_map)  — final tier map wired into registry
7. universe_store.upsert_symbol_quotes() — open_interest + tier written to DB
```
After the blocking sequence, all 6 background tasks are spawned.

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

The pattern is logged at startup:
```
CORS allow_origin_regex: <pattern>
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
│    ├── _resolve_startup_universe()   fresh snapshot or full load │
│    ├── init_registry()              Layer 1 init                │
│    ├── registry.build()             first OCC contract build    │
│    ├── _stamp_oi(quotes, oi_map)    open_interest → quotes      │
│    ├── assign_tiers(quotes)         OI-informed T1/T2/T3        │
│    ├── registry.set_tier_map()      wire final tier map         │
│    └── universe_store.upsert_symbol_quotes()  OI+tier → DB      │
│                                                                 │
│    BACKGROUND TASKS (asyncio)                                   │
│    ├── registry.refresh_loop()      rebuild OCC every 30 min   │
│    ├── _registry_prewarm_loop()     rebuild at 9:15 AM ET daily │
│    ├── stream_options_flow()        Tradier WS pipeline         │
│    ├── start_flow_writer()          batched DB writes (L5)      │
│    ├── start_signal_writer()        signal_history writes       │
│    └── _universe_refresh_loop()     full universe refresh 24h   │
│                                                                 │
│  Stream Pipeline (per tick)                                     │
│    ├── SymbolRegistry (Layer 1)  services/symbol_registry.py    │
│    │     ├── O(1) OCC lookup                                    │
│    │     ├── tier_map from tier_thresholds DB (cached 300s)     │
│    │     └── ATM/DTE params per tier                            │
│    ├── StreamManager (Layer 2)   services/stream_manager.py     │
│    │     └── ceil(registry.size()/500) workers                  │
│    │           ├── cold-start stagger: i×200ms (B-021)          │
│    │           ├── get_session_token() semaphore=3 (B-022)      │
│    │           ├── 429 Retry-After sleep/retry (B-023)          │
│    │           ├── parse_tradier_trade()  Layer 3               │
│    │           │     ├── fill_price: tick["last"] (not "price") │
│    │           │     ├── size==0 guard → skip                   │
│    │           │     └── OCC regex {1,10} + synthetic spread    │
│    │           ├── DedupCache.is_duplicate()  Layer 4  C-019    │
│    │           │     ├── TTL=5s key=(occ_symbol, size, fill_1dp)│
│    │           │     ├── "exch"/"exchange" fallback             │
│    │           │     ├── is_sweep() → 3+ exchanges within 8s    │
│    │           │     └── → trade_type=SWEEP + exchange_count    │
│    │           ├── RepetitionAccumulator                         │
│    │           │     └── episode when ≥3 trades / ≥$50K prem   │
│    │           ├── build_composite()                             │
│    │           │     └── flow×0.55 + backtest×0.35 + vol×0.10   │
│    │           ├── SwarmEngine  (12 Groq agents)  Phase 5A      │
│    │           └── bus.publish_all()  core/async_bus.py         │
│    │                      │                                      │
│    │              AsyncEventBus (in-memory fan-out)             │
│    │                ├── "signals"       → ws.py → WS clients    │
│    │                ├── "db_writer"     → flow_store.py (L5)    │
│    │                └── "signal_writer" → signal_store.py       │
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
│   tier_thresholds  ← Feature 4A                                 │
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

## Backend Signal Pipeline — Phase 5A

```text
Tradier stream worker cold start (Layer 2 — StreamManager)
  → worker_index-based startup sleep                 B-021
       ├── worker 0: 0.0s
       ├── worker 1: 0.2s
       └── ...
  → get_session_token()                             B-022 / B-023
       ├── acquire global Semaphore(3)
       ├── POST /markets/events/session
       ├── HTTP 429 → read Retry-After (default 10s) → sleep → retry
       └── success → release semaphore
  → connect streaming session
  → parse_tradier_trade()                           Layer 3
       ├── fill_price = tick["last"] or tick.get("price") or mid
       ├── size==0 guard → return None (skip)
       ├── OCC regex {1,10} — ticker/strike/expiry/type
       ├── synthetic spread when bid=ask=0  (is_synthetic_quote=True)
       └── registry enrichment → override with chain metadata
  → DedupCache.is_duplicate()                       Layer 4  C-019
       ├── key: (occ_symbol, size, round(fill, 1))
       ├── TTL: 5s — covers PHLX/MIAX worst-case lag
       ├── NO time-bucket (int(ts//2) bug eliminated)
       ├── exchange: trade_payload["exch"] or ["exchange"]
       ├── duplicate (same trade, slower exchange) → DROP
       ├── canonical → check is_sweep()
       └── 3+ unique exchanges within 8s → trade_type = SWEEP
  → RepetitionAccumulator.ingest()
       └── RepetitionEpisode when trades ≥ 3 AND premium ≥ $50K
  → build_composite(ep, accumulator)
       ├── compute_flow_score()              × 0.55
       │     premium (capped $10M) + acceleration + trade count
       ├── get_backtest_score()              × 0.35
       │     historical win-rate by ticker/type/DTE/tier
       └── volume_weighted_premium_factor()  × 0.10
             total_premium / (OI × 100), capped 0–1, 0.5 if OI absent
  → SwarmEngine.run()                                  Phase 5A
       └── 3/6/9/12 Groq agents → majority vote → EnsembleResult
  → CompositeSignal { recommendation, composite_score,
                      swarm_direction, swarm_confidence,
                      swarm_agents JSONB, bull/bear/hold votes }
  → bus.publish_all()
       ├── "signals"       → WebSocket clients (ws.py)
       ├── "db_writer"     → flow_store.py → flow_episodes + flow_events  (Layer 5)
       └── "signal_writer" → signal_store.py → signal_history
```

### Composite Score Weights (Phase 3+)

| Component | Weight | Source |
|-----------|--------|--------|
| `flow_score` | 0.55 | Premium size, acceleration, trade count |
| `backtest_score` | 0.35 | Historical win-rate (ticker/type/DTE/tier) |
| `volume_premium_factor` | 0.10 | Premium relative to open interest |

**Recommendation threshold:** composite ≥ 0.65 → BUY (bullish) or SELL (bearish)

---

## TierEngine — Feature 4A

### Tier Definitions

| Tier | Label | Min Avg Volume | ATM Strike Range | Max DTE |
|------|-------|---------------|-----------------|---------|
| 1 | Liquid large-cap | ≥ 20M | ±20% | 90 |
| 2 | Mid-cap | ≥ 2M | ±15% | 60 |
| 3 | Standard (default) | ≥ 500K | ±10% | 30 |

Thresholds are stored in `tier_thresholds` (the `is_active = true` row) and cached for 300 seconds. Admins can update them live via `PATCH /admin/tier-thresholds` without redeployment.

### Admin Whitelist

Symbols in `TIER_ADMIN_WHITELIST` env var (default: SPY, QQQ, AAPL, TSLA, NVDA, MSFT, AMZN, META, GOOGL, AMD, PLTR, COIN) are always assigned Tier 1 regardless of volume thresholds.

### `options_universe_symbols` — Feature 4A columns

| Column | Type | Default | Notes |
|--------|------|---------|-------|
| `tier` | `SMALLINT` | `3` | 1 = liquid, 2 = mid-cap, 3 = standard |
| `open_interest` | `INT` | `NULL` | Populated by TierEngine from Tradier quotes + OI stamp |
| `average_volume` | `INT` | `NULL` | Populated by TierEngine from Tradier quotes |

### Admin endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/admin/tier-thresholds` | `GET` | Admin JWT | Read active threshold row |
| `/admin/tier-thresholds` | `PATCH` | Admin JWT | Update thresholds live (no redeploy) |
| `/admin/tier-distribution` | `GET` | Admin JWT | Count of symbols per tier |

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

## 6-Layer Gap Fixes (2026-04-24 / 2026-04-25 Audit)

| Layer | File | What Was Wrong | Fix |
|-------|------|----------------|-----|
| **L2 (2026-04-24)** | `tradier_stream.py` | `registry.refresh_loop()` rebuilt the registry every 30 min but never called `manager.refresh()`. Workers kept streaming stale OCC symbols. | Replaced with `_registry_refresh_with_manager_notify()` which calls `await manager.refresh()` after every rebuild. |
| **L2 (B-021)** | `services/stream_worker.py` | Cold boot started all workers immediately, producing a synchronized burst of session-token fetches. | Added per-worker stagger: worker `i` waits `i × startup_delay_s` before first token fetch; default `startup_delay_s = 0.2s`. |
| **L2 (B-022)** | `services/tradier_client.py` | Session token acquisition was unconstrained — many workers could hit `/markets/events/session` simultaneously. | Added a process-wide `asyncio.Semaphore(3)` around session-token fetches; max 3 concurrent requests. |
| **L2 (B-023)** | `services/tradier_client.py` | HTTP 429 from Tradier was not handled — retries ignored provider backoff. | Added explicit 429 branch: read `Retry-After` (default 10s), sleep, then retry within same semaphore hold. |
| **L4 (orig)** | `tradier_stream.py` | `DedupCache` fully built and tested but **never imported or called** in `_process_trade()`. Every exchange copy wrote a DB row. | Added `from utils.dedup import flow_dedup` + `flow_dedup.is_duplicate()` gate before every `persist_flow_event()` call. |
| **L4 (C-019)** | `utils/dedup.py` + `tradier_stream.py` | TTL=2s too tight for PHLX/MIAX lag. `int(ts//2)` bucket boundary bug. Fill key 2dp conflated ±$0.01 rounding. `exchange` never passed to `is_duplicate()` so sweep never fired. | TTL→5s, sweep window→8s. Pure first-seen TTL (no buckets). Fill key 1dp. `"exch"/"exchange"` fallback. `get_exchange_count()` + `dedup_stats()` added. |
| **L5** | `flow_store.py` | `_FLUSH_INTERVAL = 5` (5 seconds). Spec says 500ms. ~430 rows buffered between flushes, risking data loss on crash. | `_FLUSH_INTERVAL = 0.5` (500ms) + `_FLUSH_MAX_ROWS = 100` early-flush triggered inside `persist_flow_event()`. |
| **L6 (4A)** | `services/tier_engine.py` | `options_universe_symbols` had no tier column — every symbol defaulted to Tier 3. `backtest_score` tier was always 3. | Added `tier_engine.py` with dynamic threshold-driven assignment. `tier_thresholds` admin table (migration 011). Lifespan calls OI stamp + `assign_tiers()` at startup. |
| **B-008** | `services/stream_worker.py` | `errors`, `reconnects`, `last_reconnect_at` in `_stats` were never updated — `/health/stream` always returned zeros. | Added `_inc_global_error()` / `_inc_global_reconnect()` helpers writing directly into `tradier_stream._stats` via lazy import. |

---

## Supabase Tables

| Table | Writer | Key Used | Notes |
|-------|--------|----------|-------|
| `flow_episodes` | `flow_store.py` | SERVICE_KEY | 82k+ rows, primary flow data |
| `flow_events` | `flow_store.py` | SERVICE_KEY | Batched writes (500ms/100 rows); `expiry` nullable |
| `signal_history` | `signal_store.py` | SERVICE_KEY | Composite signals + swarm fields (Phase 5A) |
| `options_universe_symbols` | `universe_store.py` + `tier_engine.py` | ANON_KEY / SERVICE_KEY | Symbol quotes, stream_eligible, tier/OI/avg_vol (4A) |
| `options_universe_snapshots` | `universe_store.py` | ANON_KEY | Universe snapshots |
| `tier_thresholds` | admin endpoint | SERVICE_KEY | Single active row; cached 300s by TierEngine |

### Supabase Critical Rules

1. **Always use `SUPABASE_SERVICE_KEY`** for writes to `flow_episodes`, `flow_events`, `signal_history`, `tier_thresholds` — anon key fails with `42501` (RLS)
2. **Never send `id` fields** — Postgres generates them server-side
3. **No `.select()` after `.insert()`** in supabase-py v2
4. **`flow_events` is empty** — live data is in `flow_episodes` (82k+ rows)
5. **Env var is `SUPABASE_SERVICE_KEY`** in `config.py` — confirm the Railway variable name maps to this

---

## Alert Level Logic

| Level | Criteria |
|-------|----------|
| `CONVICTION` | premium ≥ $5M, OR (accelerating AND premium ≥ $1M) |
| `STRONG_SIGNAL` | premium ≥ $1M (non-accelerating) |
| `ALERT` | premium ≥ $250K |
| `WATCH` | premium < $250K |
