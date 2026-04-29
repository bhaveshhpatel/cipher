# Cipher Signal Engine — Reference Document

> **Source of truth is the code.** This document is updated to match the actual runtime behavior
> as of `stable/ingestion-flow-2026-04-28`.
>
> Last updated: 2026-04-28 (Gate 2 retrigger, ALERT-LEVEL fix, DEDUP-KWARGS fix, H4 sweep-dispatch
> TTL, STREAM-1/2/3 shared-session workers, snapshot idempotency U-1, migration 013, dense
> telemetry, FLOW-DEBUG, FIRST-TICK logging)

---

## Overview

The Cipher signal engine is a real-time options flow pipeline that:

1. **Resolves** a tier-filtered OCC symbol universe at startup via DB snapshot or Tradier chain fetch
2. **Pre-seeds** the registry from a DB chain cache to enable sub-second lookup on warm restart
3. **Ingests** raw trade ticks via STREAM-1/2/3 parallel workers — one shared session token, ~60–70 workers for a full universe
4. **Parses & classifies** each tick (OCC contract, fill price, premium, sentiment, influence tier, conviction)
5. **Deduplicates** cross-exchange OPRA prints before any downstream processing
6. **Accumulates** repeated flow on the same contract within a rolling 30-minute window
7. **Gates** episodes through Gate 1 (persist threshold) and Gate 2 (retrigger threshold)
8. **Scores** each qualifying episode using flow metrics + historical backtest win-rate + volume/OI ratio
9. **Emits** a composite signal with recommendation, scores, and alert level
10. **Persists** flow events and signal episodes to Supabase (service role key required)
11. **Broadcasts** signals in real-time to all connected WebSocket clients via the async event bus

> **SwarmEngine** (Groq llama-3.3-70b-versatile, 12 agents) is available but is **not called
> automatically per tick**. It requires explicit invocation via admin panel or direct API call.

---

## Module Map

```
backend/
├── main.py                          # FastAPI app + lifespan startup sequence, prewarm loop
├── config.py                        # pydantic-settings v2 — all env vars
├── core/
│   ├── async_bus.py                 # In-memory asyncio fan-out bus
│   └── auth.py                      # JWT middleware
├── parsers/
│   ├── options_flow_parser.py       # Layer 3 — raw tick → OptionsFlowEvent
│   ├── bid_ask_classifier.py        # ABOVE_ASK / AT_ASK / MID / AT_BID / BELOW_BID
│   └── trade_type_detector.py       # SWEEP / BLOCK / SPLIT / SINGLE
├── signals/
│   ├── repetition_accumulator.py    # Layer 5 — Gate 1 + Gate 2 + alert level
│   ├── composite_signal_engine.py   # Layer 6 — 3-component composite score
│   ├── backtest_validator.py        # Historical win-rate by ticker/type/DTE/tier
│   └── midcap_screener.py           # Mid-cap filter (not wired into live pipeline)
├── services/
│   ├── tradier_stream.py            # Pipeline orchestrator — _process_trade() tick funnel
│   ├── stream_manager.py            # STREAM-1/2/3 — shared session token, worker pool
│   ├── stream_worker.py             # Per-worker Tradier POST + telemetry
│   ├── symbol_registry.py           # Layer 1 — OCC contract map, tier map, refresh loop
│   ├── flow_store.py                # Layer 5/6 — flow_events + flow_episodes DB writer
│   ├── signal_store.py              # Layer 6 — signal_history DB writer
│   ├── universe_store.py            # Snapshot idempotency, tier map load
│   ├── chain_store.py               # OCC contract DB cache (fast pre-seed)
│   ├── tier_engine.py               # T1/T2/T3 assignment
│   └── swarm_engine.py              # Groq AI swarm — explicit invocation only
├── utils/
│   └── dedup.py                     # Layer 4 — DedupCache TTL=5s, sweep_win=8s
├── routers/
│   ├── ws.py                        # WebSocket — signals bus subscriber
│   ├── health.py                    # /health/stream — full funnel stats
│   ├── history.py                   # /api/signals/history
│   ├── admin.py                     # Admin panel — demo mode, registry control
│   ├── auth.py, flow.py, simulation.py, smart_signals.py
└── migrations/
    └── 001–013_*.sql                # 013 = UNIQUE(snapshot_id, symbol)
```

---

## Startup Sequence (main.py lifespan)

```
1. start_flow_writer()          — subscribe bus "db_writer", start 500ms flush loop
2. start_signal_writer()        — subscribe bus "signal_writer"
3. init_registry(watchlist)     — create SymbolRegistry with watchlist tickers
4. universe_store snapshot      — reuse existing snapshot_id if < 20h old AND
                                  symbol count within ±10% (U-1 idempotency)
                                  otherwise create new snapshot
5. registry.build()             — background task:
     └── load_from_db()         — fast pre-seed from chain_store DB cache
     └── fetch Tradier chains   — fill missing contracts
     └── set _build_complete    — unblocks stream_options_flow()
6. stream_options_flow(registry=registry)
     └── polls registry.is_ready() every 500ms (up to 30-min timeout)
     └── StreamManager.run()    — spawns STREAM-1/2/3... workers
7. registry.refresh_loop()      — background refresh (30 min default, 15 min on expiry days)
8. _registry_prewarm_loop()     — 9:15 AM ET weekdays — pre-builds OCC set before 9:30 AM open
```

---

## Layer 1 — Symbol Registry (`services/symbol_registry.py`)

- Pre-loads OCC contract metadata into an in-memory dict at startup and after every refresh
- Per-tick lookup is O(1): `registry["TSLA260424C00375000"]` → `{ticker, strike, expiry, type, DTE, tier}`
- No regex, no API call, no per-tick latency
- Refreshes every `REGISTRY_REFRESH_MINS` (default 30 min); on expiry days every 15 min
- **Pre-warm loop** (`_registry_prewarm_loop` in `main.py`): fires every weekday at 09:15 ET to
  rebuild the full OCC set 15 minutes before market open. Skips weekends. Non-fatal on error.
- **DB chain fast-seed**: `registry.load_from_db(snapshot_id)` pre-loads OCC contracts from
  `chain_store` DB cache before the full Tradier chain fetch completes. Enables lookup immediately
  on warm restart without waiting for the network round-trips.
- **Build-complete flag**: `registry._build_complete` is set `True` only after `build()` finishes.
  `stream_options_flow()` polls `registry.is_ready()` before spawning workers (D-001 fix).

**Per-tier contract filtering:**

| Tier | Label | ATM Strike Range | Max DTE |
|---|---|---|---|
| T1 (Liquid) | AAPL, TSLA, SPY, QQQ, etc. | ±20% | 90 |
| T2 (Mid-cap) | Mid-liquidity names | ±15% | 60 |
| T3 (Default) | All others / unknown | ±10% | 30 |

Tier params loaded from `tier_thresholds` DB row (cached 300s). Unknown-tier symbols fall back
to T3. `ContractMeta.tier` carries the tier through the pipeline into `backtest_score`.

---

## Layer 2 — Stream Ingestion

### StreamManager (`services/stream_manager.py`)

- Fetches **one shared Tradier session token** for all workers
- Splits OCC symbols into ≤500-symbol batches
- Spawns one asyncio task per batch (STREAM-1, STREAM-2, STREAM-3...)
- Active worker count: `ceil(registry.size() / 500)` — typically 60–70 for full universe
- No lock between workers — full parallel concurrency
- Emits `STREAM_STATS` log every 30s per worker; `STREAM_HEALTH` at manager level every 30s
- On session expiry: re-fetches token, restarts all workers

### StreamWorker (`services/stream_worker.py`)

- One httpx streaming POST to Tradier per worker
- Calls `_process_trade()` for every received timesale event
- 30s idle watchdog: reconnects if no keepalive received
- Exponential backoff: 5s base, 60s cap, jitter on all error paths

### Demo mode

- **Disabled as automatic fallback since 2026-04-25**
- Explicit invocation only via admin panel (`/admin` endpoint)
- `_demo_mode_once()` generates synthetic signals across a random ticker/premium set

---

## Layer 3 — Trade Parsing (`parsers/options_flow_parser.py`)

**`parse_tradier_trade(raw_dict)`** → `OptionsFlowEvent | None`

| Field | Source / Logic |
|---|---|
| `ticker` | OCC parse → registry enrichment override |
| `contract_type` | OCC regex: `C` → CALL, `P` → PUT |
| `strike` | OCC regex (÷1000) |
| `expiry` | OCC regex → `YYYY-MM-DD` |
| `dte` | Calendar days from today to expiry |
| `fill_price` | `tick["last"]` → `tick.get("price")` → bid/ask mid |
| `size` | Contract count — `size == 0` → return None |
| `premium` | `fill_price × size × 100` |
| `bid` / `ask` | From tick; synthetic ±0.5% spread when both are 0 |
| `is_synthetic_quote` | `True` when synthetic spread applied (C-018) |
| `bid_ask_class` | ABOVE_ASK / AT_ASK / MID / AT_BID / BELOW_BID |
| `is_aggressive` | `True` if fill ≥ ask |
| `trade_type` | SWEEP / BLOCK / SPLIT / SINGLE (upgraded retroactively for SWEEP via C-003) |
| `sentiment` | BULLISH (CALL) / BEARISH (PUT) / NEUTRAL |
| `influence_tier` | WHALE / INSTITUTIONAL / LARGE / RETAIL (premium thresholds) |
| `conviction_score` | 0.0–1.0 from size, premium, aggression, spread quality |
| `is_golden_sweep` | `True` if SWEEP AND premium ≥ $500,000 |
| `timestamp` | UTC datetime of tick |

Returns `None` if OCC symbol is unparseable, `size == 0`, or `fill_price == 0` after all fallbacks.

---

## Layer 4 — Deduplication (`utils/dedup.py`)

Module-level singleton: `flow_dedup = DedupCache(ttl=5.0, sweep_window=8.0)`

**Dedup key:** `(occ_symbol, size, round(fill_price, 1))`

**DEDUP-KWARGS fix (2026-04-28):** First param of `is_duplicate()` is `event_or_occ_symbol`
(positional). Callers must pass `occ_symbol` as the **first positional arg** — not as a keyword.
Keyword form raises `TypeError: got an unexpected keyword argument 'occ_symbol'`.

| Parameter | Value | Rationale |
|---|---|---|
| TTL | 5 seconds | Covers MIAX/PHLX lag (worst-case 2–5s) |
| Sweep window | 8 seconds | Wider than TTL to catch multi-leg fills |
| Fill precision | 1 decimal place | Absorbs ±$0.01 feed rounding across exchanges |
| Sweep min exchanges | 3 | Standard OPRA multi-exchange sweep threshold |

**Sweep detection:** `exchange_count >= 3` within `sweep_window` → `trade_type = SWEEP`

**C-003 — Retroactive sweep upgrade:** When the 3rd exchange arrives as a duplicate (after the
canonical row was already written as BTO), `_process_trade()` dispatches
`upgrade_to_sweep_in_db()` as a background task. Issues a targeted PATCH to `flow_events` for
rows matching `(occ_symbol, fill_price, size)` within the last 30 seconds.

**H4 — Sweep dispatch TTL eviction (2026-04-28):** `_sweep_upgrade_dispatched` was a `Set[str]`
that grew forever. Changed to `dict[str, float]` (key → wall-clock timestamp). Entries older than
1800s (30 min) are evicted before each membership check. Correctly re-dispatches upgrade for the
same contract reprinting after 30 min.

---

## Layer 5 — Repetition Accumulator (`signals/repetition_accumulator.py`)

Groups events by `(ticker, contract_type, strike, expiry)` in a rolling 30-minute window.

### Gate 1 — Persist threshold (OR logic)

```
trade_count  >= min_trades   (default: 3)
OR
total_premium >= min_premium  (default: $10,000)
```

Below both thresholds → `ingest_tick()` returns `None` → tick dropped.
`_stats["accumulator_gated"]` incremented and logged at INFO.

### Gate 2 — Signal retrigger guard (2026-04-28)

After an episode first crosses Gate 1, it only re-emits when:

```
total_premium - last_signaled_premium >= SIGNAL_RETRIGGER_THRESHOLD  (default: $50,000)
```

`RepetitionEpisode.last_signaled_premium` tracks the `total_premium` at the last signal emission.
This prevents QQQ/SPY episodes from emitting a new `signal_history` row on every tick once the
threshold is crossed.

### Alert Levels

Computed by `accumulator.get_alert_level(ep)` from `ep.total_premium`:

| Level | Condition | DB value |
|---|---|---|
| `CONVICTION` | `total_premium >= $1,000,000` | `"CONVICTION"` |
| `STRONG_SIGNAL` | `total_premium >= $500,000` | `"STRONG_SIGNAL"` |
| `ALERT` | `total_premium >= $200,000` | `"ALERT"` |
| `WATCH` | `total_premium < $200,000` | `"WATCH"` |

**ALERT-LEVEL fix (2026-04-28):** `flow_store._bus_signal_listener` was reading
`sig.get("recommendation")` (BUY/SELL/HOLD) for `alert_level`. Fixed to `sig.get("alert_level")`.
`alert_level` is populated by `accumulator.get_alert_level(sig_ep)` in `tradier_stream._process_trade()`
and injected into the signal dict **before** `bus.publish_all()`. The two fields are NOT interchangeable.

### Acceleration flag

`ep.is_accelerating = True` when ≥ 2 ticks within the last 5 minutes.

---

## Layer 6 — Composite Scoring (`signals/composite_signal_engine.py`)

### Score formula

```
flow_score = min(1.0,
    (total_premium / $10,000,000) × 0.65
  + 0.15  if is_accelerating
  + min(trade_count / 20, 0.20)
)

volume_premium_factor = min(1.0, total_premium / (open_interest × 100))
  → falls back to 0.5 (neutral) when OI is unavailable
  → do NOT treat 0.5 as a signal — it means OI data is missing

composite_score = (flow_score × 0.55) + (backtest_score × 0.35) + (volume_premium_factor × 0.10)

BUY  → composite_score >= 0.65  AND  contract_type == CALL
SELL → composite_score >= 0.65  AND  contract_type == PUT
HOLD → composite_score <  0.65
```

**Score weight history:**

| Phase | Formula |
|---|---|
| Phase 2 | `flow × 0.60 + backtest × 0.40` |
| Phase 3+ (current) | `flow × 0.55 + backtest × 0.35 + volume_premium × 0.10` |

---

## Backtest Validation (`signals/backtest_validator.py`)

Looks up historical win-rate for `(ticker, contract_type, DTE_bucket, tier)`.

**DTE Buckets:** `0–7`, `8–30`, `31–90`, `90+`

**Tier baseline win-rates (seeded — not live Supabase aggregation):**

| Tier | Base Win-Rate |
|---|---|
| WHALE | 72% |
| INSTITUTIONAL | 63% |
| LARGE | 55% |
| RETAIL | 44% |

> These are seeded pseudo-values. Replace with live Supabase 90-day aggregation before full
> production launch.

---

## _process_trade Tick Funnel

```
raw Tradier timesale event
    │
    ├─ event_type not "timesale"?              → skip (INFO log for first 10 distinct types)
    │
    ├─ parse_tradier_trade() → None?           → parse_failed++ / INFO log / RETURN
    │
    ├─ flow_dedup.is_duplicate(               ← occ_symbol passed POSITIONALLY (DEDUP-KWARGS fix)
    │     occ_symbol, size, fill, exch, ts)?
    │     ├─ True → deduped++
    │     │     └─ exchange_count == sweep_min?  → upgrade_to_sweep_in_db() [background]
    │     └─ RETURN
    │
    ├─ flow_dedup.is_sweep()?                  → ev.trade_type = "SWEEP"
    │
    ├─ classified++
    │
    ├─ accumulator.ingest_tick(ev)
    │     ├─ Gate 1 not crossed?              → accumulator_gated++ / INFO log / RETURN
    │     └─ Gate 2 not crossed?              → (no episode returned) / RETURN
    │
    ├─ persist_flow_event()                    → persisted++ / buffered → flow_events
    │
    ├─ alert_level = accumulator.get_alert_level(sig_ep)   ← ALERT-LEVEL fix source
    │
    ├─ build_composite()                       → composite score
    │
    ├─ bus.publish_all({type:"signal", ...})   → signals++ / WebSocket delivery
    │
    └─ bus.publish_all({type:"composite_signal", ...})
          ├─ "signals"       → WebSocket clients (ws.py)
          ├─ "db_writer"     → flow_store → flow_episodes (reads alert_level, NOT recommendation)
          └─ "signal_writer" → signal_store → signal_history
```

---

## Persistence

### flow_events

One row per classified tick that passed Gate 1. Written via batched buffer.

- Flush interval: 500ms timer OR 100-row early flush
- Retry: 3 attempts with 1s delay on Supabase failure
- Key: `SUPABASE_SERVICE_ROLE_KEY` only (bypasses RLS)

### flow_episodes

One row per Gate 2 emission. Written immediately by `_bus_signal_listener` on `db_writer` channel.
`alert_level` is read from `sig.get("alert_level")` — the CONVICTION/STRONG_SIGNAL/ALERT/WATCH
enum. NOT from `sig.get("recommendation")` (BUY/SELL/HOLD).

### signal_history

Written by `signal_store.py` on `signal_writer` channel for every `composite_signal` event.
In-memory deque fallback when DB unavailable. Exposed via `GET /api/signals/history`.

---

## Async Bus Channels

| Channel | Publisher | Subscriber | Purpose |
|---|---|---|---|
| `"signals"` | tradier_stream | routers/ws.py | Live WebSocket delivery |
| `"db_writer"` | tradier_stream | flow_store._bus_signal_listener | flow_episodes persistence |
| `"signal_writer"` | tradier_stream | signal_store listener | signal_history persistence |

All three channels receive the same `composite_signal` message payload.

---

## WebSocket Protocol

| Message | Direction | Detail |
|---|---|---|
| Signal JSON | Server → Client | Live signal payload |
| `{"type":"ping"}` | Server → Client | Every 25 seconds |
| `{"type":"pong"}` | Client → Server | Must respond within 10 seconds |
| Code `4001` | Server closes | Invalid / expired JWT on connect |
| Code `1001` | Server closes | Pong timeout |

---

## DB Schema Reference

### flow_events

```sql
ticker, contract_type, strike, expiry, dte, fill_price,
bid, ask, size, premium, trade_type, bid_ask_class,
is_aggressive, is_golden_sweep, sentiment, influence_tier,
conviction_score, exchange_count, fill_count, open_interest,
iv, underlying_price, occ_symbol, is_synthetic_quote,
created_at  -- id: uuid generated by Postgres
```

### flow_episodes

```sql
ticker, direction, contract_type, strike, expiry,
total_premium, trade_count, alert_level, is_accelerating,
seed_episode, signal_ts, created_at
-- id: bigserial generated by Postgres
```

### signal_history

```sql
id BIGSERIAL, ticker, recommendation, composite_score,
flow_score, backtest_score, volume_premium_factor, reasoning,
contract_type, alert_level, sentiment, direction,
influence_tier, premium, trade_type, is_golden_sweep,
total_premium, trade_count,
swarm_direction, swarm_confidence, swarm_agents JSONB,
swarm_bull_votes, swarm_bear_votes, swarm_hold_votes,
signal_ts, created_at
```

### options_universe_symbols

```sql
snapshot_id, symbol, ticker, strike, expiry, contract_type,
tier SMALLINT DEFAULT 3,
open_interest INT, average_volume INT,
UNIQUE(snapshot_id, symbol)  -- migration 013
```

### tier_thresholds

```sql
id, t1_min_volume, t1_max_atm_pct, t1_max_dte,
t2_min_volume, t2_max_atm_pct, t2_max_dte,
t3_min_volume, t3_max_atm_pct, t3_max_dte,
is_active BOOLEAN, created_at
```

---

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_URL` | Yes | Supabase project REST endpoint |
| `SUPABASE_SERVICE_ROLE_KEY` | **Yes** | Server-side DB writes — bypasses RLS. Never use the anon key. |
| `SUPABASE_KEY` | No | Anon key — read-only queries only |
| `TRADIER_API_KEY` | Yes | Tradier streaming + REST API access |
| `TRADIER_BASE_URL` | Yes | Tradier REST base URL |
| `JWT_SECRET` | Yes | JWT signing key |
| `GROQ_API_KEY` | No | Groq API — required for SwarmEngine only |
| `SWARM_N_AGENTS` | No | Swarm agent count (valid: 3/6/9/12, default: 6) |
| `REGISTRY_REFRESH_MINS` | No | OCC registry refresh interval (default: 30) |
| `TIER_ADMIN_WHITELIST` | No | Comma-separated tickers forced to Tier 1 |
| `TIER_THRESHOLD_CACHE_TTL_S` | No | Tier threshold cache TTL (default: 300) |
| `CORS_ALLOWED_ORIGINS` | No | Extra origins for CORS regex |

> ⚠️ `SUPABASE_SERVICE_ROLE_KEY` is the service_role secret from
> Supabase Dashboard → Project Settings → API → service_role.
> The anon key will fail every server-side insert with `42501 RLS policy violation`.

---

## Known Issues / Backlog

- **Backtest validator** — uses seeded pseudo-random win-rates. Needs live Supabase 90-day aggregation before full production launch.
- **Mid-cap screener** — `midcap_screener.py` exists but not confirmed wired into the live pipeline accumulator filter.
- **Trade executor** — `execution/trade_executor.py` is built and tested but not connected to the signal output path.
- **`flow_event_buffer` loss on crash** — events buffered in memory are lost on pod restart.
- **Frontend WS pong** — frontend must implement `{"type":"pong"}` response within 10s.

---

## Changelog

| Date | Change |
|---|---|
| 2026-04-28 | Gate 2 retrigger (`last_signaled_premium`, $50k delta). ALERT-LEVEL fix in `flow_store._bus_signal_listener`. DEDUP-KWARGS positional fix. H4 sweep-dispatch dict + 30-min TTL eviction. STREAM-1/2/3 shared-session parallel workers. U-1 snapshot idempotency. Migration 013 UNIQUE constraint. FLOW-DEBUG gate logging. FIRST-TICK INFO logging. |
| 2026-04-26 | Layer 1 registry, Layer 2 StreamManager/Worker, C-019 dedup overhaul, Phase 5A swarm, Feature 4A tiers, prewarm loop, B-008/021/022/023 stream hardening. |
| 2026-04-24 | C-019 dedup TTL + sweep fix. C-018 synthetic quote flag. C-017 duplicate flow_episodes fix. C-016 UnboundLocalError. C-015 timesale filter. |
| 2026-04-23 | Phase 4: signal_history + signal_store + WS ping/pong. Phase 3: volume_premium_factor. RLS 42501 fix. Initial signal engine doc. |
