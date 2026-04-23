# Cipher — Bug Fix Log

Chronological record of all bugs found and fixed. Each entry includes root cause, symptom, and the exact change made.

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
