# Cipher — Bug Fix Log

Chronological record of all bugs found and fixed. Each entry includes root cause, symptom, and the exact change made.

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
**Severity:** Medium — 2× rows per episode in `flow_episodes` table
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

### Why It's Dangerous

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

A detailed module docstring was also added explaining the key selection contract.

### Action Required on Railway

Verify in Railway → **Settings → Variables** that `SUPABASE_SERVICE_ROLE_KEY` is set to the **service_role** secret key from:
> Supabase Dashboard → Project Settings → API → **service_role** (secret)

Do **not** use the `anon` key — it will always fail for server-side writes with RLS enabled.

### Supabase Key Reference

| Key | Env var | RLS | Use for |
|-----|---------|-----|--------|
| Anon / Public | `SUPABASE_KEY` | ✅ Enforced | Client-side / read-only queries |
| Service Role | `SUPABASE_SERVICE_ROLE_KEY` | ❌ Bypassed | All server-side DB writes |

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
