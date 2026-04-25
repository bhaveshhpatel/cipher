# Cipher — Architecture & Data Flow

> Last updated: 2026-04-25 (Feature 4A — TierEngine: dynamic tier assignment, admin thresholds, OI/vol enrichment)

---

## Overview

Cipher is an institutional options flow intelligence platform. It monitors live Tradier WebSocket streams across ~16,000 OCC symbols (split across 32 parallel connections), classifies each trade tick through a 6-layer pipeline, detects repetition patterns, runs a multi-agent AI swarm, and surfaces high-conviction signals to the frontend via WebSocket — persisting all signals to Supabase for historical querying.

---

## The 6-Layer Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 1 — Symbol Registry  (services/symbol_registry.py)        │
│                                                                   │
│  Pre-loads OCC contract metadata at startup into a dict.         │
│  On each stream tick: O(1) lookup                                │
│    registry["TSLA260424C00375000"]                               │
│      → { ticker, strike, expiry, contract_type, DTE, tier }      │
│  No regex, no API call, no per-tick latency.                     │
│  Refreshes every REGISTRY_REFRESH_MINS (default 30).            │
│  On expiry days: refreshes every 15 min.                         │
│                                                                   │
│  Feature 4A — Per-tier contract filtering:                       │
│  Contract universe is shaped by the symbol's tier at build time. │
│  Tier params loaded from tier_thresholds DB row (cached 300s).   │
│    Tier 1 (liquid): ATM ±20%  max DTE 90  (e.g. AAPL, TSLA)    │
│    Tier 2 (mid-cap): ATM ±15%  max DTE 60                       │
│    Tier 3 (default): ATM ±10%  max DTE 30                       │
│  Unknown-tier symbols fall back to T3 params.                    │
│  ContractMeta gains a .tier field — carried through pipeline     │
│  into backtest_score (historical win-rate by ticker/type/DTE/    │
│  tier). Tier map seeded from universe_store.load_tier_map() on   │
│  warm start; updated via registry.set_tier_map() on refresh.    │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 2 — Stream Manager  (services/stream_manager.py)          │
│                                                                   │
│  ~16,000 OCC symbols — Tradier caps each connection at ~500.     │
│  StreamManager splits into 32 parallel connections, each with    │
│  its own session token (stream_worker.py per connection).        │
│  Auto-reconnects on drop. When symbol list refreshes, only       │
│  affected workers restart — not all 32.                          │
│                                                                   │
│  FIX (2026-04-24): registry refresh loop now calls               │
│  manager.refresh() after every rebuild. Previously the refresh   │
│  loop ran but never notified the manager — workers streamed       │
│  stale OCC symbols indefinitely after a 30-min rebuild.          │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 3 — Parser  (parsers/options_flow_parser.py)              │
│                                                                   │
│  CRITICAL FIX (C-015): stream sends "last" as fill price,        │
│  not "price".                                                    │
│  fill_price = float(tick["last"] or tick.get("price") or mid)   │
│  Also: size==0 guard, OCC regex expanded to {1,10} chars,        │
│  synthetic bid/ask spread when bid=ask=0, registry enrichment    │
│  overrides OCC-parsed fields with pre-validated chain metadata.  │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 4 — Deduplication  (utils/dedup.py)                 C-019 │
│                                                                   │
│  A single trade prints on CBOE, MIAX, PHLX, AMEX within a       │
│  reporting window. OPRA exchange lag reality (2026):             │
│    CBOE:  50-200ms  (fastest, canonical print)                   │
│    MIAX:  500ms-3s  (routinely late)                             │
│    PHLX:  2-5s      (worst-case lag on sweeps)                   │
│    BATO:  1-4s      (common on large prints)                     │
│                                                                   │
│  C-019 fix (2026-04-24) — 5 bugs fixed:                         │
│  1. TTL: 2s → 5s  — covers worst-case PHLX/MIAX lag             │
│  2. Sweep window: 5s → 8s  — matches extended TTL               │
│  3. Eliminated int(ts//2) bucket boundary bug: CBOE at t=1.99s  │
│     and MIAX at t=2.01s landed in different buckets, both passed │
│     as canonical. Pure first-seen TTL comparison replaces this.  │
│  4. Fill key: 2dp → 1dp — absorbs ±$0.01 feed rounding across   │
│     exchanges without conflating genuinely different fills.       │
│  5. flow_dedup was instantiated but NEVER imported or called     │
│     in _process_trade() — Layer 4 was completely inert in        │
│     production. Fixed + exchange field now correctly passed      │
│     via "exch"/"exchange" fallback so sweep detection fires.     │
│                                                                   │
│  Key: (occ_symbol, size, round(fill, 1))  — no time bucket      │
│  Sweep: 3+ unique exchanges within 8s → trade_type = SWEEP      │
│  Module-level singleton: flow_dedup (TTL=5s, sweep_win=8s)      │
│  Observability: dedup_stats() exposed via /health endpoint       │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 5 — Batched DB Writes  (services/flow_store.py)           │
│                                                                   │
│  Never write one row at a time. Buffer events and flush to       │
│  Supabase every 500ms OR 100 rows, whichever comes first.        │
│  Estimated: ~62K filtered rows/day → ~744 batched flushes.       │
│  Uses SUPABASE_SERVICE_ROLE_KEY (bypasses RLS).                  │
│                                                                   │
│  FIX (2026-04-24): _FLUSH_INTERVAL was set to 5s instead of     │
│  500ms. At 62K rows/day that's ~430 rows buffered per flush.     │
│  Fixed: _FLUSH_INTERVAL=0.5s + _FLUSH_MAX_ROWS=100 early-flush  │
│  in persist_flow_event() so 100-row batches fire immediately.    │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Layer 6 — Supabase Realtime + TierEngine          Feature 4A    │
│                                                                   │
│  Realtime: Zero extra work. Supabase auto-broadcasts every       │
│  INSERT to subscribed frontend clients. Frontend subscribes to   │
│  flow_episodes and signal_history channels.                      │
│                                                                   │
│  TierEngine (services/tier_engine.py):                           │
│    assign_tiers(symbols) → upserts tier (1/2/3) + open_interest │
│    + average_volume onto options_universe_symbols.               │
│    Thresholds loaded from tier_thresholds (is_active=true row)  │
│    and cached for TIER_THRESHOLD_CACHE_TTL_S (default 300s).    │
│    Admin whitelist (TIER_ADMIN_WHITELIST env) forces symbols to  │
│    Tier 1 regardless of metrics.                                 │
│    Called by universe_store.upsert() after every symbol refresh. │
└──────────────────────────────────────────────────────────────────┘
```

---

## 6-Layer Gap Fixes (2026-04-24 Audit)

> Three gaps were discovered during a post-implementation audit. All fixed in commit `309192f`.
> C-019 adds five additional Layer 4 fixes applied after extended OPRA lag analysis.

| Layer | File | What Was Wrong | Fix |
|-------|------|----------------|-----|
| **L2** | `tradier_stream.py` | `registry.refresh_loop()` rebuilt the registry every 30 min but never called `manager.refresh()`. Workers kept streaming stale OCC symbols. | Replaced with `_registry_refresh_with_manager_notify()` which calls `await manager.refresh()` after every rebuild. |
| **L4 (orig)** | `tradier_stream.py` | `DedupCache` (`utils/dedup.py`) was fully built and unit-tested, but **never imported or called** in `_process_trade()`. Every exchange copy of a trade wrote a DB row. 4 exchanges → 4× row count. | Added `from utils.dedup import flow_dedup` + `flow_dedup.is_duplicate()` gate before every `persist_flow_event()` call. Also wired `is_sweep()` upgrade. |
| **L4 (C-019)** | `utils/dedup.py` + `tradier_stream.py` | TTL=2s too tight for PHLX/MIAX lag (2–5s). `int(ts//2)` bucket boundary let MIAX duplicate at t=2.01s slip past CBOE canonical at t=1.99s. Fill key at 2dp conflated ±$0.01 feed rounding. `exchange` field never passed to `is_duplicate()` so sweep detection always saw one exchange and never fired. | TTL→5s, sweep window→8s. Pure first-seen TTL (no buckets). Fill key 1dp. `"exch"/"exchange"` fallback in `_process_trade()`. `get_exchange_count()` + `dedup_stats()` added. |
| **L5** | `flow_store.py` | `_FLUSH_INTERVAL = 5` (5 seconds). Spec says 500ms. At 62K rows/day: ~430 rows buffered between flushes, risking data loss on crash. | `_FLUSH_INTERVAL = 0.5` (500ms) + `_FLUSH_MAX_ROWS = 100` early-flush triggered inside `persist_flow_event()` itself. |
| **L6 (4A)** | `services/tier_engine.py` | `options_universe_symbols` had no tier column — every symbol defaulted to Tier 3 regardless of liquidity. Tier used in `backtest_score` was always 3, degrading signal quality for large-cap / liquid symbols. | Added `tier_engine.py` with dynamic threshold-driven assignment. `tier_thresholds` admin table (migration 011). `universe_store.upsert()` now calls `assign_tiers()` post-refresh. |

---

## System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        Railway (Backend)                        │
│                                                                 │
│  main.py (FastAPI lifespan)                                     │
│    ├── SymbolRegistry (Layer 1)  services/symbol_registry.py   │
│    │     └── pre-loads ~16,000 OCC contracts at startup        │
│    ├── StreamManager (Layer 2)   services/stream_manager.py    │
│    │     └── 32 parallel Tradier connections via stream_worker  │
│    │           ├── parse_tradier_trade()  Layer 3               │
│    │           │     ├── fill_price: tick["last"] (not "price") │
│    │           │     ├── size==0 guard → skip                  │
│    │           │     └── OCC regex {1,10} + synthetic spread   │
│    │           ├── DedupCache.is_duplicate()  Layer 4  C-019   │
│    │           │     ├── 5s TTL (occ_symbol, size, fill_1dp)   │
│    │           │     ├── "exch"/"exchange" fallback for exch   │
│    │           │     ├── is_sweep() → 3+ exchanges within 8s   │
│    │           │     └── → trade_type=SWEEP + exchange_count   │
│    │           ├── RepetitionAccumulator                        │
│    │           │     └── episode when ≥3 trades / ≥$50K prem  │
│    │           ├── build_composite()                            │
│    │           │     └── flow×0.55 + backtest×0.35 + vol×0.10  │
│    │           ├── SwarmEngine  (12 Groq agents)  Phase 5A     │
│    │           └── bus.publish_all()  core/async_bus.py        │
│    │                      │                                    │
│    │              AsyncEventBus (in-memory fan-out)            │
│    │                ├── "signals"       → ws.py → WS clients  │
│    │                ├── "db_writer"     → flow_store.py (L5)  │
│    │                └── "signal_writer" → signal_store.py     │
│    │                                                           │
│    ├── _registry_refresh_with_manager_notify()  ← FIXED (L2)  │
│    │     └── registry.build() → manager.refresh() every 30min │
│    │                                                           │
│    ├── start_flow_writer()    services/flow_store.py  (L5)    │
│    │     ├── flush every 500ms OR 100 rows  ← FIXED (L5)      │
│    │     ├── persist_flow_episode() → flow_episodes            │
│    │     └── _flush_flow_events()   → flow_events              │
│    │                                                           │
│    ├── start_signal_writer()  services/signal_store.py        │
│    │     ├── persists CompositeSignal + swarm fields           │
│    │     └── → signal_history (Supabase Realtime L6)          │
│    │                                                           │
│    └── TierEngine  services/tier_engine.py  Feature 4A        │
│          ├── assign_tiers(symbols) — called by universe_store  │
│          │     upsert() after every symbol refresh             │
│          ├── _load_thresholds() — queries tier_thresholds      │
│          │     WHERE is_active=true; cached 300s               │
│          ├── Admin whitelist (TIER_ADMIN_WHITELIST) → Tier 1   │
│          └── upserts tier + open_interest + average_volume     │
│                onto options_universe_symbols                    │
│                                                                 │
│  FastAPI Routers                                                │
│    ├── /api/auth                  auth.py                      │
│    ├── /api/flow/scan             flow.py                      │
│    ├── /api/simulate              simulation.py                │
│    ├── /ws/signals                ws.py (ping/pong heartbeat)  │
│    ├── /api/signals/composite     smart_signals.py             │
│    ├── /api/signals/list          smart_signals.py             │
│    ├── /api/signals/history       history.py                   │
│    ├── /admin/tier-thresholds     admin.py  (PATCH/GET)        │
│    └── /admin/tier-distribution   admin.py  (GET)              │
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

```
Tradier SSE tick (Layer 2 — StreamManager)
  → parse_tradier_trade()                              Layer 3
       ├── fill_price = tick["last"] or tick.get("price") or mid
       ├── size==0 guard → return None (skip)
       ├── OCC regex {1,10} — ticker/strike/expiry/type
       ├── synthetic spread when bid=ask=0
       └── registry enrichment → override with chain metadata
  → DedupCache.is_duplicate()                          Layer 4  C-019
       ├── key: (occ_symbol, size, round(fill, 1))
       ├── TTL: 5s — covers PHLX/MIAX worst-case lag
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

### `options_universe_symbols` — Feature 4A columns

| Column | Type | Default | Notes |
|--------|------|---------|-------|
| `tier` | `SMALLINT` | `3` | 1 = liquid, 2 = mid-cap, 3 = standard |
| `open_interest` | `INT` | `NULL` | Populated by TierEngine from Tradier quotes |
| `average_volume` | `INT` | `NULL` | Populated by TierEngine from Tradier quotes |

### Admin endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/admin/tier-thresholds` | `GET` | Admin JWT | Read active threshold row |
| `/admin/tier-thresholds` | `PATCH` | Admin JWT | Update thresholds live (no redeploy) |
| `/admin/tier-distribution` | `GET` | Admin JWT | Count of symbols per tier |

### `tier_thresholds` Table

```sql
CREATE TABLE tier_thresholds (
  id                    BIGSERIAL PRIMARY KEY,
  t1_min_avg_volume     INT NOT NULL DEFAULT 20000000,
  t1_atm_range_pct      NUMERIC NOT NULL DEFAULT 20.0,
  t1_max_dte            INT NOT NULL DEFAULT 90,
  t2_min_avg_volume     INT NOT NULL DEFAULT 2000000,
  t2_atm_range_pct      NUMERIC NOT NULL DEFAULT 15.0,
  t2_max_dte            INT NOT NULL DEFAULT 60,
  t3_min_avg_volume     INT NOT NULL DEFAULT 500000,
  t3_atm_range_pct      NUMERIC NOT NULL DEFAULT 10.0,
  t3_max_dte            INT NOT NULL DEFAULT 30,
  is_active             BOOLEAN NOT NULL DEFAULT false,
  created_at            TIMESTAMPTZ DEFAULT now()
);
```

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

## Supabase Tables

| Table | Writer | Key Used | Notes |
|-------|--------|----------|-------|
| `flow_episodes` | `flow_store.py` | SERVICE_ROLE_KEY | 82k+ rows, primary flow data |
| `flow_events` | `flow_store.py` | SERVICE_ROLE_KEY | Batched writes (500ms/100 rows); `expiry` nullable |
| `signal_history` | `signal_store.py` | SERVICE_ROLE_KEY | Composite signals + swarm fields (Phase 5A) |
| `options_universe_symbols` | `universe_store.py` + `tier_engine.py` | ANON_KEY / SERVICE_ROLE_KEY | Symbol quotes, stream_eligible, **tier/OI/avg_vol (4A)** |
| `options_universe_snapshots` | `universe_store.py` | ANON_KEY | Universe snapshots |
| `tier_thresholds` | admin endpoint | SERVICE_ROLE_KEY | Single active row; cached 300s by TierEngine |

### Supabase Critical Rules

1. **Always use `SUPABASE_SERVICE_ROLE_KEY`** for writes to `flow_episodes`, `flow_events`, `signal_history`, `tier_thresholds` — anon key fails with `42501` (RLS)
2. **Never send `id` fields** — Postgres generates them server-side
3. **No `.select()` after `.insert()`** in supabase-py v2
4. **`flow_events` is empty** — live data is in `flow_episodes` (82k+ rows)
5. **Env var is `SUPABASE_SERVICE_ROLE_KEY`** (Railway config var name)

---

## Alert Level Logic

| Level | Criteria |
|-------|----------|
| `CONVICTION` | premium ≥ $5M, OR (accelerating AND premium ≥ $1M) |
| `STRONG_SIGNAL` | premium ≥ $1M |
| `ALERT` | premium ≥ $250K |
| `WATCH` | premium ≥ $50K (minimum threshold) |

---

## Universe Pipeline (Startup + 24h Refresh)

| Step | Action | Table |
|------|--------|-------|
| 1 | CBOE CSV → ~5,500 raw symbols | — |
| 2 | Tradier `/expirations` validation → ~5,500 confirmed optionable | — |
| 3 | Tradier batch quotes (200/batch, 28 parallel) → `stream_eligible` flag | `options_universe_symbols` |
| 4 | **TierEngine.assign_tiers()** → upserts `tier`, `open_interest`, `average_volume` | `options_universe_symbols` |
| 5 | Extract `stream_eligible=true` → StreamManager (~1,000–2,000 symbols) | — |
| 6 | Save snapshot | `options_universe_snapshots` |

**Startup priority:** fresh DB snapshot (< 24h) → Tradier fetch → stale snapshot → `SEED_SYMBOLS`.

---

## ID Generation Contract

> **Rule:** `flow_events`, `flow_episodes`, and `signal_history` rows are **never sent with an `id` field**. Postgres generates IDs server-side. Sending a client-generated `id` causes a 400 / schema mismatch error.

---

## Environment Variables

| Variable | Used by | Required |
|----------|---------|----------|
| `TRADIER_API_KEY` | tradier_stream.py | Yes (live mode) |
| `TRADIER_BASE_URL` | tradier_stream.py | Yes |
| `TRADIER_STREAM_URL` | tradier_stream.py | Yes |
| `TRADIER_ACCOUNT_ID` | trade_executor.py | Yes (paper/live trading) |
| `SUPABASE_URL` | flow_store, signal_store, universe_store | Yes |
| `SUPABASE_SERVICE_ROLE_KEY` | flow_store, signal_store, tier_engine | **Yes — service role key** |
| `SUPABASE_KEY` | universe_store, smart_signals (reads) | Yes (anon key) |
| `SECRET_KEY` | auth.py | Yes |
| `ALGORITHM` | auth.py | Yes (default: HS256) |
| `GROQ_API_KEY` | swarm_engine.py | Yes (swarm; HOLD fallback if absent) |
| `SWARM_N_AGENTS` | swarm_engine.py | No (default: 6) |
| `REGISTRY_MAX_DTE` | symbol_registry.py | No (default: 90) |
| `REGISTRY_REFRESH_MINS` | symbol_registry.py | No (default: 30) |
| `TIER_ADMIN_WHITELIST` | tier_engine.py | No — comma-separated tickers forced to Tier 1 |
| `TIER_THRESHOLD_CACHE_TTL_S` | tier_engine.py | No (default: 300) |

---

## Where to Look for Signals

| What you want | Where to look |
|--------------|---------------|
| Raw flow ticks (live) | Railway logs → filter `[flow]` |
| Signal episodes (live) | Railway logs → filter `[signal]` |
| Persisted flow episodes | Supabase `flow_episodes` (82k+ rows) |
| Signal history (paginated) | `GET /api/signals/history?limit=50&min_conviction=0.65` |
| Paginated signals list | `GET /api/signals/list?page=1&min_conviction=0.65` |
| WebSocket delivery | Browser devtools → WS frames on `/ws/signals` |
| Swarm agent reasoning | `signal_history.swarm_agents` JSONB column |
| Dedup stats (live) | `GET /health` → `dedup_duplicates`, `dedup_sweeps`, `dedup_cache_size` |
| Tier distribution | `GET /admin/tier-distribution` |
| Active tier thresholds | `GET /admin/tier-thresholds` |

---

## Known Issues / Phase 6 TODO

- ✅ **Feature 4A complete** — TierEngine, tier_thresholds table, migrations 010+011, admin endpoints, 35 tests
- `signals/midcap_screener.py` — confirm integrated into signal pipeline
- Wire `TradeExecutor` into simulation router for live paper trade execution
- Load test `/api/signals/list` and `/api/signals/history` with 50 concurrent authenticated users
- WebSocket fan-out benchmark with 50+ subscribers
- Redis integration (`REDIS_URL` in config but unused — candidate for WS pub/sub at scale)
- Wire `/api/signals/list` tier filter to live `tier` column on `options_universe_symbols`
