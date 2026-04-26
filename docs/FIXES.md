# Cipher — Bug Fix Log

Chronological record of all bugs found and fixed. Each entry includes root cause, symptom, and the exact change made.

---

## B-023 — Explicit 429 Handling in `get_session_token()`

**Date:** 2026-04-25  
**Type:** Reliability fix  
**Files:** `backend/utils/tradier_client.py`

### Root Cause

`get_session_token()` called `resp.raise_for_status()` without first checking for HTTP 429. When Tradier rate-limited a session token request, `raise_for_status()` converted the 429 into an unhandled `httpx.HTTPStatusError`. The worker caught this as a generic exception, returned `None`, immediately hit its backoff sleep, then retried — re-requesting a token without waiting for the rate-limit window to reset. This burned more API budget and deepened the 429 spiral.

### Symptom

Workers entering a crash-loop under load: `None` token → backoff sleep → retry → 429 again → repeat. Logs showed repeated `"No session token — backing off"` at startup with 32 workers attempting simultaneous connections.

### Fix Applied

**`backend/utils/tradier_client.py`**
- Added explicit `if resp.status_code == 429:` check **before** `raise_for_status()`
- Reads `Retry-After` header (defaults to `_DEFAULT_RETRY_AFTER_S = 10.0` if absent)
- Sleeps `retry_after` seconds, then `continue`s to retry within the same semaphore hold
- `_DEFAULT_RETRY_AFTER_S = 10.0` module constant — easily tunable

### Expected Impact

| Scenario | Before B-023 | After B-023 |
|----------|-------------|-------------|
| Tradier returns 429 | Unhandled exception → crash loop | Sleep Retry-After, then retry |
| API budget under load | Burned faster (crash-loop re-requests) | Controlled — respects rate-limit window |
| Worker restart time after 429 | Unpredictable (backoff jitter) | Deterministic (Retry-After header) |

---

## B-022 — Global Session Token Semaphore (`max 3 concurrent`)

**Date:** 2026-04-25  
**Type:** Reliability fix  
**Files:** `backend/utils/tradier_client.py`

### Root Cause

With 32 `StreamWorker` instances all calling `get_session_token()` at startup (and again during the 30-min registry refresh restart), Tradier received ~32 simultaneous POST requests to `/v1/markets/events/session`. Tradier's rate limiter silently returned 429s for the overflow requests. Because the 429 was not handled explicitly (B-023), workers entered a reconnect death spiral: failed token → backoff → retry → another 429 → repeat. The net result was that many workers never established a stream connection at all, leaving large gaps in OCC symbol coverage.

### Symptom

At startup with >8 workers: intermittent `"No session token — backing off"` log spam across many workers, with only a subset (typically the first 3–6) successfully connecting. Stream coverage dropped silently — no error surfaced at the `StreamManager` level.

### Fix Applied

**`backend/utils/tradier_client.py`**
- Added `_SESSION_SEM = asyncio.Semaphore(3)` module-level constant
- Wrapped entire `get_session_token()` body in `async with _SESSION_SEM:`
- With Semaphore(3) and ~400ms per token round-trip: 32 workers acquire tokens in ⌈32/3⌉ = 11 batches × ~400ms = **~4.4s total** (one-time startup cost)
- Semaphore also controls token pressure during the 30-min registry refresh restart

### Startup Timing (combined B-021 + B-022)

| Step | Time |
|------|------|
| B-021 worker stagger (32 workers × 200ms) | 6.4s |
| B-022 token batching (⌈32/3⌉ batches × 400ms avg) | ~4.4s |
| **Total one-time startup window** | **~10–11s** |
| Ongoing per-signal latency impact | **Zero** |

### Expected Impact

| Metric | Before B-022 | After B-022 |
|--------|-------------|-------------|
| Concurrent token requests at startup | 32 (burst) | Max 3 |
| Silent 429s at startup | Common (>8 workers) | Eliminated |
| Workers failing to connect | Up to ~28/32 | Zero (all connect, serially batched) |
| 30-min refresh token pressure | Same burst problem | Controlled |

---

## B-021 — Staggered Worker Startup (200ms between workers)

**Date:** 2026-04-25  
**Type:** Reliability fix  
**Files:** `backend/services/stream_manager.py`, `backend/services/stream_worker.py`, `backend/tests/test_stream_manager.py`

### Root Cause

Before B-021, `StreamManager._spawn_workers()` created all 32 `StreamWorker` tasks in a tight loop with no delay between them. Each worker immediately called `get_session_token()` on startup, producing a ~32-simultaneous-request burst to Tradier’s session endpoint. Even with the B-022 semaphore in place, the stagger further smooths connection load by spreading *when* workers attempt to acquire the semaphore rather than all contending for it instantly.

### Fix Applied

**`backend/services/stream_manager.py`**
- Added `_WORKER_STARTUP_STAGGER_MS = 200` and `_WORKER_STARTUP_STAGGER_S = 0.200` constants
- Each worker receives `startup_delay_s = idx * 0.200` — worker-0 gets 0s, worker-31 gets 6.2s
- `_spawn_workers()` passes `startup_delay_s` to `StreamWorker.__init__()` as a constructor arg

**`backend/services/stream_worker.py`**
- `__init__` accepts `startup_delay_s: float = 0.0`
- `run()` calls `await asyncio.sleep(startup_delay_s)` **once** at the top before any connection attempt
- Reconnects do **not** re-apply the startup delay — they use the standard `_backoff()` function
- `startup_delay_s` exposed in `worker.stats` for `/health` observability

**`backend/tests/test_stream_manager.py`**
- Added `TestB021StaggeredStartup` class — 7 tests (tests 21–27)
- Covers: constants are 200ms/0.200s, worker-0 delay=0, worker-1 delay=0.2, worker-N delay=N×0.2, stat exposure, one-time fire (reconnects don’t re-apply)

### Expected Impact

| Metric | Before B-021 | After B-021 |
|--------|-------------|-------------|
| Token request burst at startup | 32 simultaneous | Spread over 6.4s |
| Per-signal latency (ongoing) | — | Zero impact |
| Worker startup observability | None | `startup_delay_s` in `/health` |

---

## T-001 — Unit Test Suite: OCC Parser, Bid/Ask Classifier, Repetition Engine

**Date:** 2026-04-25
**Type:** Test coverage
**Files added:**
- `backend/tests/test_occ_parser.py` (40 tests)
- `backend/tests/test_classifier.py` (24 tests)
- `backend/tests/test_repetition_engine.py` (22 tests)

### Coverage Added

**`test_occ_parser.py`** — `parsers/options_flow_parser.py`
- `_parse_occ_symbol`: CALL/PUT/SPXW parse, whitespace padding, invalid symbol, invalid date (month 13), empty string, strike÷1000 precision
- `_calc_dte`: future date, empty string→0, past date clamped to 0, unparseable string→0
- `_parse_timestamp`: epoch ms, ISO string, None fallback, garbage string fallback
- `parse_tradier_trade` (full path): `last` field primary / `price` fallback / bid-ask mid fallback (C-015); OCC-derived ticker when `underlying` absent (C-010); OCC-derived strike/expiry/ctype when stream fields missing (C-011); DTE auto-calc (C-011); `is_synthetic_quote=True` when bid=ask=0 (C-018); `is_synthetic_quote=False` with real NBBO; premium formula; `size=0`→None; malformed payload→None (no exception); all 4 influence tiers (WHALE/INSTITUTIONAL/LARGE/RETAIL); conviction score in [0,1]; golden sweep flag; registry enrichment override; registry failure non-fatal

**`test_classifier.py`** — `parsers/bid_ask_classifier.py` + `parsers/trade_type_detector.py`
- `classify_bid_ask`: ABOVE_ASK, AT_ASK, AT_BID, BELOW_BID, MID, crossed market→MID, all zeros→MID, exact midpoint
- `is_aggressive`: ABOVE_ASK/AT_ASK→True; MID/AT_BID/BELOW_BID/unknown→False
- `detect_trade_type`: SWEEP (exchange_count≥3), SPLIT (fill_count≥3), BLOCK (size≥50 + premium≥500k), SINGLE (fallback); SWEEP beats BLOCK; SPLIT≠SWEEP
- `is_golden_sweep`: True (SWEEP + ≥1M + aggressive); False for low premium, wrong type, not aggressive

**`test_repetition_engine.py`** — `signals/repetition_accumulator.py`
- `RepetitionEpisode`: trade_count, total_premium, is_accelerating True/False/too few events, summary_str fields
- `RepetitionAccumulator.ingest`: below min_trades→None, below min_premium→None, both thresholds met→episode, rolling window prune, cross-contract isolation, accumulation across calls, episode returned on every qualifying call
- `RepetitionAccumulator.get_alert_level`: CONVICTION (≥5M), CONVICTION (accelerating + ≥1M), STRONG_SIGNAL (≥1M non-accelerating), ALERT (≥250k), WATCH (<250k)
- Init: default window=30min, min_trades=3, min_premium=50k; custom params respected

### Running Total

| File | Tests |
|------|-------|
| `test_occ_parser.py` | 40 |
| `test_classifier.py` | 24 |
| `test_repetition_engine.py` | 22 |
| **T-001 subtotal** | **86** |
| Prior tests (auth, stream, universe, flow store, signal) | ~164 |
| **Grand total** | **~250** |

---

## C-020 — Feature 4A: Tier Engine + Universe Tier Assignment

**Date:** 2026-04-25
**Severity:** Enhancement — universe symbols had no tier classification; `backtest_score` OI factor defaulted to 0.5 for all symbols
**Files:** `backend/services/tier_engine.py` *(new)*, `backend/services/universe_store.py`, `backend/main.py`
**Migrations:** `010_add_tier_and_oi_to_universe.sql`, `011_add_tier_thresholds.sql`

### Root Cause

All `options_universe_symbols` rows carried no tier metadata. The `volume_premium_factor` in the composite score used `open_interest` when available and fell back to 0.5 — but OI was never being written to the universe table. The `backtest_score` tier dimension was always `None`. `signal_history.influence_tier` was hard-coded downstream from a simple premium threshold rather than a real tier classification.

### Fix Applied

**New: `backend/services/tier_engine.py`**
- `_TierParams` dataclass — holds `min_volume`, `max_atm_pct`, `max_dte` per tier row
- `TierEngine` class:
  - `load_thresholds()` — reads active row from `tier_thresholds` table; caches in `_params`
  - `assign_tiers(symbols)` — classifies each symbol dict into tier 1/2/3 using `average_volume` and a 90-day ATM heuristic
  - `upsert_tiers(symbols)` — batch-upserts `tier` + `open_interest` + `average_volume` back to `options_universe_symbols`
  - Admin whitelist: symbols in `TIER_1_ADMIN_WHITELIST` (SPY, QQQ, AAPL, TSLA, NVDA, MSFT, AMZN, META, GOOGL, AMD, PLTR, COIN) are always Tier 1 regardless of thresholds
- `set_tier_map(symbols)` — module-level helper called by `main.py` after universe load; builds an in-memory `{ticker: tier}` lookup for the signal pipeline
- `get_tier(ticker)` — O(1) lookup; returns 3 if ticker not in map

**`backend/services/universe_store.py`**
- `load_tier_map()` — new function; reads `(ticker, tier)` from latest active snapshot’s symbols; returns `dict[str, int]`; returns `{}` on DB error or missing column

**`backend/main.py`**
- After universe load at startup, calls `tier_engine.load_thresholds()` then `set_tier_map(symbols)` so all downstream signal scoring has real tier data from first tick

### DB Migrations (applied 2026-04-25 to cipher-database)

**`010_add_tier_and_oi_to_universe.sql`**
```sql
ALTER TABLE options_universe_symbols
  ADD COLUMN IF NOT EXISTS tier         SMALLINT NOT NULL DEFAULT 3,
  ADD COLUMN IF NOT EXISTS open_interest INT,
  ADD COLUMN IF NOT EXISTS average_volume INT;
CREATE INDEX IF NOT EXISTS idx_universe_symbols_tier ON options_universe_symbols (tier);
```

**`011_add_tier_thresholds.sql`**
```sql
CREATE TABLE IF NOT EXISTS tier_thresholds (
  id               BIGSERIAL PRIMARY KEY,
  t1_min_volume    BIGINT  NOT NULL DEFAULT 20000000,
  t1_max_atm_pct   NUMERIC NOT NULL DEFAULT 20.0,
  t1_max_dte       INT     NOT NULL DEFAULT 90,
  t2_min_volume    BIGINT  NOT NULL DEFAULT 2000000,
  t2_max_atm_pct   NUMERIC NOT NULL DEFAULT 15.0,
  t2_max_dte       INT     NOT NULL DEFAULT 60,
  t3_min_volume    BIGINT  NOT NULL DEFAULT 500000,
  t3_max_atm_pct   NUMERIC NOT NULL DEFAULT 10.0,
  t3_max_dte       INT     NOT NULL DEFAULT 30,
  is_active        BOOLEAN NOT NULL DEFAULT false,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO tier_thresholds (t1_min_volume, t2_min_volume, t3_min_volume, is_active)
VALUES (20000000, 2000000, 500000, true)
ON CONFLICT DO NOTHING;
```

### Tier Classification Rules

| Tier | Label | Min Avg Volume | Admin Whitelist |
|------|-------|---------------|-----------------|
| 1 | Liquid large-cap | ≥ 20M | SPY, QQQ, AAPL, TSLA, NVDA, MSFT, AMZN, META, GOOGL, AMD, PLTR, COIN |
| 2 | Mid-cap | ≥ 2M | — |
| 3 | Standard (default) | ≥ 500K | — |

### Expected Impact

| Metric | Before C-020 | After C-020 |
|--------|-------------|-------------|
| `tier` on universe symbols | Always `NULL` / 3 | Real 1/2/3 per thresholds |
| `open_interest` written | Never | Populated on each universe refresh |
| `volume_premium_factor` OI fallback | Always 0.5 | Real OI used when available |
| `influence_tier` in signal_history | Premium threshold only | Derived from real tier classification |
| Admin tier override | None | `TIER_1_ADMIN_WHITELIST` always forces Tier 1 |

---

## C-019 — Layer 4 dedup TTL too tight / sweep detection inert

**Date:** 2026-04-24
**Severity:** High — multi-exchange duplicate trades inflating premium tallies in RepetitionAccumulator; sweep detection never firing
**Files:** `backend/utils/dedup.py`, `backend/services/tradier_stream.py`, `backend/services/demo_engine.py`

### Root Causes (5 separate bugs)

**Bug 1 — TTL too tight (2s) for real OPRA reporting lag**
MIAX routinely reports 500ms–3s after CBOE on the same trade. PHLX can lag 2–5s on high-volume sweeps. The 2s TTL meant duplicates from MIAX/PHLX slipped through as canonical prints, creating 2–4x row count and proportionally inflated premium totals in the `RepetitionAccumulator`.

**Bug 2 — Time-bucket boundary gap**
The dedup key used `int(ts // 2)` to bucket time into 2s slots. A CBOE print at `t=1.99s` and its MIAX duplicate at `t=2.01s` landed in *different buckets* and both passed as canonical. This was a systematic gap — any trade where the canonical and duplicate straddled a 2s boundary was double-written.

**Bug 3 — Fill price precision (2dp) too tight**
Different exchange feeds round fill prices differently. A $3.45 CBOE print and $3.46 MIAX print of the same trade were treated as different trades because `fill:.2f` produced different keys. Changed to `fill:.1f` — absorbs ±$0.01 rounding without conflating genuinely different strikes.

**Bug 4 — Dedup completely inert in production**
`flow_dedup` was instantiated as a module-level singleton in `utils/dedup.py` but was **never imported into `tradier_stream.py`**. `_process_trade()` never called `is_duplicate()`. Every exchange copy of every trade was written to the DB. Layer 4 has been a no-op in production since initial implementation.

**Bug 5 — Sweep detection never fired**
`is_duplicate()` accepted an `exchange` parameter, but `_process_trade()` never passed it — `exchange` defaulted to empty string `""`. `_exchange_hits` accumulated `["", "", ""]`. `set(["", "", ""])` has length 1 — the `>= 3` threshold was never reached. Zero sweeps were ever detected.

### Fix Applied

**`utils/dedup.py`**
- `ttl_seconds`: `2.0 → 5.0`
- `sweep_window`: `5.0 → 8.0`
- Eliminated `int(ts // 2)` bucket — replaced with pure `first_seen_ts` + TTL comparison
- Fill key: `fill:.2f → fill:.1f`
- `dedup_stats()` method added — exposes `dedup_seen`, `dedup_duplicates`, `dedup_sweeps`, `dedup_cache_size`
- `get_exchange_count()` method added — returns actual unique exchange count for sweep upgrade in `_process_trade()`

**`tradier_stream.py`**
- Added `from utils.dedup import flow_dedup`
- `exchange = trade_payload.get("exch") or trade_payload.get("exchange", "")` — handles both real Tradier feed (`"exch"`) and demo engine (`"exchange"`)
- `flow_dedup.is_duplicate()` called before `persist_flow_event()` — duplicates dropped, `_stats["deduped"]` incremented
- Sweep upgrade: `is_sweep()` → `ev.trade_type = "SWEEP"`, `ev.exchange_count = real_exch_count`
- `_stats` now includes `"deduped": 0` counter
- `get_stats()` merges `flow_dedup.dedup_stats()` for full `/health` observability

**`demo_engine.py`**
- `_build_timesale_envelope()` now sets `"exch"` as primary exchange key (matching real Tradier field); `"exchange"` kept as alias for backward compat
- Inter-exchange delay widened: `20–80ms → 50–300ms` to simulate real MIAX/PHLX lag and exercise the 5s TTL window properly
- `get_stats()` merges `flow_dedup.dedup_stats()` for live dedup observability from admin panel

### Expected Impact

| Metric | Before C-019 | After C-019 |
|--------|-------------|-------------|
| DB rows per trade | 2–4x (one per exchange) | 1x (canonical only) |
| Premium in accumulator | 2–4x inflated | Accurate |
| Sweep detection | Never fired | Fires on 3+ exchanges within 8s |
| MIAX/PHLX dedup | Missed if >2s late | Caught up to 5s lag |
| `/health` dedup visibility | None | `dedup_duplicates`, `dedup_sweeps`, `dedup_cache_size` |

---

## C-018 — Synthetic quotes polluting bid_ask_class / is_aggressive metrics

**Date:** 2026-04-24
**Severity:** Medium — data quality; backtesting and aggression metrics skewed by synthesized NBBO
**Files:** `backend/parsers/options_flow_parser.py`, `backend/services/tradier_stream.py`, `backend/services/flow_store.py`
**Migration:** `backend/migrations/009_flow_events_synthetic_quote.sql`

### Root Cause

When Tradier omits `bid` and `ask` from a timesale event (both arrive as `0`), the parser synthesised a ±0.5% spread from the fill price to avoid a 0/0 divide in `classify_bid_ask()`. This is correct behaviour — but the resulting `bid_ask_class` and `is_aggressive` values are **not real market data**. They were computed from the fill itself, so `is_aggressive` was always `False` (fill == synthetic mid) for these rows. No flag existed to identify these rows, making it impossible to exclude them from backtesting aggression metrics or net-premium calculations.

### Symptom

Backtest queries grouping by `is_aggressive = true` under-count aggressive flow because ~X% of rows have `is_aggressive = false` due to synthetic NBBO, not actual at-ask prints.

### Fix Applied

**`options_flow_parser.py`** — Added `is_synthetic_quote: bool = False` field to `OptionsFlowEvent` dataclass. Set to `True` in the synthetic spread block:
```python
is_synthetic_quote = False
if effective_bid == 0 and effective_ask == 0 and fill > 0:
    effective_bid = round(fill * 0.995, 4)
    effective_ask = round(fill * 1.005, 4)
    is_synthetic_quote = True
```

**`tradier_stream.py`** — Forward `is_synthetic_quote` in the `persist_flow_event()` dict inside `_process_trade()`. Also added to the debug log line.

**`flow_store.py`** — Added `"is_synthetic_quote": ev_dict.get("is_synthetic_quote", False)` to the row dict in `persist_flow_event()`.

### DB Migration

Run `backend/migrations/009_flow_events_synthetic_quote.sql` in Supabase SQL editor **before** deploying:
```sql
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS is_synthetic_quote boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_flow_events_synthetic_quote
  ON flow_events (is_synthetic_quote)
  WHERE is_synthetic_quote = true;
```
The `DEFAULT false` safely backfills all historical rows as clean.

### Backtest Query Pattern

Always filter synthetic quotes out of aggression/net-premium calculations:
```sql
SELECT * FROM flow_events
WHERE is_synthetic_quote = false
  AND is_aggressive = true;
```

---

## C-017 — Duplicate `flow_episodes` rows per signal episode

**Date:** 2026-04-24
**Severity:** Medium — 2x rows per episode in `flow_episodes` table
**Fix:** `_bus_signal_listener` in `flow_store.py` now writes `flow_episodes` ONLY on `composite_signal` events. Raw `signal` events are WebSocket-only and no longer trigger a DB write.

---

## C-016 — `UnboundLocalError` in `persist_flow_event()`

**Date:** 2026-04-24
**Severity:** High — every flow event write crashed after buffer hit 100 rows
**Fix:** Added `global _flow_event_buffer` declaration inside `persist_flow_event()`. Without it, Python treats local reassignment as a local variable binding for the entire function scope.

---

## C-015 — Stream filter `trade` delivering equity events instead of option events

**Date:** 2026-04-23
**Severity:** Critical — all strike/expiry/bid/ask values were 0 in DB
**Fix:** Switched Tradier stream `filter` from `trade` to `timesale`. `filter=trade` delivers equity ticks (stock price, stock bid/ask). `filter=timesale` delivers option contract ticks with the full OCC symbol and real option NBBO.

---

## C-014 — Regressions in parser from over-aggressive null guards

**Date:** 2026-04-23
**Severity:** Medium — trades with fill=0 or unknown contract type were silently dropped
**Fix:** Removed `if fill == 0: return None` and the hard `return None` for unknown `ctype`. Fill=0 derives from mid price; unknown ctype defaults to PUT.

---

## C-013 — Tradier stream envelope not unwrapped

**Date:** 2026-04-23
**Severity:** High — `parse_tradier_trade()` received the outer envelope dict, not the inner trade payload
**Fix:** `_process_trade()` now unwraps the `raw[event_type]` inner dict before passing to the parser. Logs first raw line on each new connection for diagnostics.

---

## C-012 — Tailwind + Auth + SmartSignals (Phase 2)

**Date:** 2026-04-23
**Files:** `tailwind.config.ts`, `login/page.tsx`, `register/page.tsx`, `SmartSignals.tsx`
**Changes:** Tailwind wired to CSS vars. Login/register pages matching dark design. SmartSignals leaderboard tab added.

---

## C-011 — Design System Overhaul (Phase 1)

**Date:** 2026-04-23
**Files:** `frontend/src/app/globals.css`, `layout.tsx`, `page.tsx`, all dashboard components
**Changes:** Full dark terminal design system. CSS variables for color, surface, typography. Redesigned FlowTable, SignalFeed, SimulationPanel, CompositeCard, StreamStatsBar with skeleton loaders, empty states, conviction bars, confidence rings, vote bars, agent grids. Sidebar navigation.

---

## C-010 — `flow_episodes` inserts failing with 401 / RLS policy violation

**Date:** 2026-04-23
**Severity:** Critical — no flow episodes were ever persisted to DB
**Symptom in logs:**
```
[flow_store] insert into flow_episodes failed: 401 — {"code":"42501","details":null,"hint":null,"message":"new row violates row-level security policy for table \"flow_episodes\""}
```

### Root Cause

`flow_store.py` had a silent fallback on line 36:
```python
# BEFORE — dangerous fallback
_SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
```
When `SUPABASE_SERVICE_ROLE_KEY` was not set in Railway environment variables, the expression evaluated to `SUPABASE_KEY` — the **anon/public key**. The anon key is subject to Supabase Row Level Security (RLS). Since `flow_episodes` has RLS enabled with no policy permitting anonymous inserts, every write was rejected with HTTP 401 / Postgres error `42501`.

### Why It’s Dangerous

The fallback was silent — no error was thrown at startup. The service appeared to run normally (flow ticks were logged, signals were detected) but **nothing was ever written to the database**. The only indication was the `401` error in Railway logs.

### Fix Applied

**File:** `backend/services/flow_store.py`

```python
# AFTER — service role key only, no fallback
_SUPABASE_KEY: Optional[str] = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
```

Also updated the startup warning to be explicit:
```python
# AFTER
log.warning(
    "[flow_store] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — "
    "flow_events and flow_episodes will NOT be persisted to DB. "
    "Ensure SUPABASE_SERVICE_ROLE_KEY (not the anon key) is set in Railway env vars."
)
```

### Action Required on Railway

Verify in Railway → **Settings → Variables** that `SUPABASE_SERVICE_ROLE_KEY` is set to the **service_role** secret key from:
> Supabase Dashboard → Project Settings → API → **service_role** (secret)

Do **not** use the `anon` key — it will always fail for server-side writes with RLS enabled.

### Supabase Key Reference

| Key | Env var | RLS | Use for |
|-----|---------|-----|--------|
| Anon / Public | `SUPABASE_KEY` | Enforced | Client-side / read-only queries |
| Service Role | `SUPABASE_SERVICE_ROLE_KEY` | Bypassed | All server-side DB writes |

---

## C-009 — `universe_screener.py` OI screener replaced by batch quotes

**Date:** 2026-04-20
**Severity:** Medium — screener was slow and unreliable for large universes
**Fix:** Replaced `screen_universe()` with `_fetch_batch_quotes()` in Step 3 of the universe pipeline. `universe_screener.py` kept for reference, marked deprecated — do not call from `load_universe()`.

---

## C-008 — `stream_eligible` column missing from DB migration

**Date:** 2026-04-20
**Severity:** High — `options_universe_symbols` upsert failing on missing column
**Fix:** Added `stream_eligible`, `last_price`, `volume` columns + index in `002_universe_symbols_quotes.sql`.

---

## C-007 — `config.py` missing `priority_symbols` property

**Date:** 2026-04-18
**Severity:** Medium — `UNIVERSE_PRIORITY_SYMBOLS` env var was read but never parsed into a list
**Fix:** Added `@property priority_symbols` in `config.py` that splits the comma-separated string into `list[str]`.

---

## C-006 — `options_universe_snapshots.provider` NOT NULL with no default

**Date:** 2026-04-18
**Severity:** High — every snapshot insert failed
**Fix:** Always pass `"tradier"` explicitly when inserting into `options_universe_snapshots`.

---

## C-005 — supabase-py v2 `.select()` not available after `.insert()`

**Date:** 2026-04-17
**Severity:** High — snapshot ID could not be read back after insert
**Fix:** Generate `snapshot_id` via `uuid4()` in Python before the insert, use it directly rather than reading back from the DB response.
