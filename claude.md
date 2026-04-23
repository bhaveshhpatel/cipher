# Cipher — Claude Context File

## Project Overview

**Cipher** is an institutional options flow intelligence platform with the tagline *"Decode the Market."* It detects real-time whale/institutional options flow, scores signals using a composite engine, and runs multi-agent AI swarm simulations to generate BUY/SELL/HOLD verdicts.

---

## Repository

- **GitHub**: `https://github.com/bhaveshhpatel/cipher`
- **Owner**: Dhruv Patel

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Backend | FastAPI (Python 3.11 pinned), async WebSockets |
| Auth | JWT (`python-jose` + `passlib` bcrypt) |
| Streaming | Tradier WebSocket → async in-process event bus |
| AI Engine | OpenAI GPT-4o-mini (multi-agent swarm, 6 roles) |
| Database | Supabase (PostgreSQL) |
| Deploy (BE) | Railway |
| Deploy (FE) | Vercel |
| CI/CD | GitHub Actions (CI only for backend; deploy via Railway native GitHub integration) |

---

## Universe Pipeline (Steps 1–5)

```
Step 1: CBOE CSV → ~5,500 raw symbols
        ↓  _fetch_cboe_symbols()  in services/symbols_loader.py
Step 2: Tradier /expirations validation → ~5,500 confirmed optionable symbols
        ↓  _validate_symbols()  in services/symbols_loader.py
Step 3: Tradier batch quotes → /v1/markets/quotes
        - Batch into groups of 200 (~28 parallel requests)
        - Fetch: last_price, volume per symbol
        - Compute stream_eligible flag:
            last_price >= UNIVERSE_MIN_PRICE (default 1.0)
            AND volume >= UNIVERSE_MIN_VOLUME (default 100,000)
        - Priority symbols (UNIVERSE_PRIORITY_SYMBOLS) always forced eligible
        - Upsert all symbols into options_universe_symbols table
        ↓  _fetch_batch_quotes()  in services/symbols_loader.py
           upsert_symbol_quotes() in services/universe_store.py
Step 4: Extract stream_eligible=true symbols → StreamPoolManager
        (~1,000–2,000 after price/volume filter)
        ↓  main.py startup reads eligible_set from load_universe() return value
Step 5: Save snapshot to options_universe_snapshots
        ↓  save_snapshot()  in services/universe_store.py
```

### DB Columns Added (migration 002)
`options_universe_symbols` now has:
- `stream_eligible BOOLEAN NOT NULL DEFAULT false`
- `last_price NUMERIC(12,4)`
- `volume BIGINT`
- Index: `idx_universe_symbols_eligible` on `(snapshot_id, stream_eligible) WHERE stream_eligible = true`

### Config Knobs (Railway env vars)

| Var | Default | Purpose |
|---|---|---|
| `UNIVERSE_MIN_PRICE` | `1.0` | Min last_price to be stream-eligible |
| `UNIVERSE_MIN_VOLUME` | `100000` | Min daily volume to be stream-eligible |
| `UNIVERSE_QUOTES_BATCH_SIZE` | `200` | Symbols per /quotes request |
| `UNIVERSE_QUOTES_CONCURRENCY` | `28` | Parallel batch requests |
| `UNIVERSE_PRIORITY_SYMBOLS` | `SPY,QQQ,AAPL,TSLA,NVDA,MSFT,AMZN,META,GOOGL,AMD` | Always stream-eligible regardless of price/volume |

---

## Repository Structure

```
cipher/
├── .github/
│   └── workflows/
│       ├── backend.yml        # CI only — syntax check; NO deploy steps
│       └── frontend.yml       # Vercel deploy via CLI
├── backend/
│   ├── main.py                # FastAPI app — startup loads universe from DB first
│   ├── config.py              # pydantic-settings v2 — priority_symbols property added
│   ├── requirements.txt       # pydantic[email] ensures email-validator is installed
│   ├── requirements-dev.txt
│   ├── nixpacks.toml
│   ├── runtime.txt            # python-3.11.9
│   ├── .python-version        # 3.11.9
│   ├── migrations/
│   │   ├── 001_options_universe.sql          # base tables
│   │   └── 002_universe_symbols_quotes.sql   # stream_eligible, last_price, volume columns
│   ├── core/
│   │   ├── auth.py
│   │   └── async_bus.py
│   ├── parsers/
│   │   ├── options_flow_parser.py
│   │   └── bid_ask_classifier.py
│   ├── services/
│   │   ├── flow_store.py          # DB writer: flow_events + flow_episodes — uses SERVICE ROLE KEY only
│   │   ├── symbols_loader.py      # Steps 1–3: CBOE fetch, validation, batch quotes
│   │   ├── universe_store.py      # Steps 4–5: DB read/write + upsert_symbol_quotes
│   │   ├── universe_screener.py   # DEPRECATED — OI-based screener, no longer called
│   │   └── tradier_stream.py      # Resilient WebSocket stream processor
│   ├── signals/
│   │   └── repetition_accumulator.py
│   └── tests/
│       └── test_symbols_loader.py # Steps 1–3 full coverage incl. Step 3 batch quotes
├── frontend/
│   └── (Next.js 14 app)
├── docs/
│   ├── ARCHITECTURE.md        # System data flow and DB schema
│   ├── BACKLOG.md
│   ├── FIXES.md               # Chronological log of all bug fixes applied
│   ├── features.md
│   ├── regression-test-plan.md
│   └── specs.md
└── claude.md                  # This file — Claude context for code changes
```

---

## Known Fixes Applied

| ID | Description |
|---|---|
| C-005 | supabase-py v2 does not expose `.select()` after `.insert()` — generate `snapshot_id` via `uuid4()` in Python before insert |
| C-006 | `options_universe_snapshots.provider` is `NOT NULL` with no default — always pass `"tradier"` explicitly |
| C-007 | `config.py` missing `priority_symbols` property — added `@property` that parses `UNIVERSE_PRIORITY_SYMBOLS` string into `list[str]` |
| C-008 | `stream_eligible` column missing from DB migration — added in `002_universe_symbols_quotes.sql` along with `last_price` and `volume` |
| C-009 | `universe_screener.py` OI-based per-symbol screening replaced by `_fetch_batch_quotes()` batch quotes (Step 3) — screener marked deprecated |
| C-010 | `flow_store.py` was falling back to `SUPABASE_KEY` (anon key) when `SUPABASE_SERVICE_ROLE_KEY` was missing — anon key respects RLS and caused every `flow_episodes` insert to fail with 401/42501. Fixed: removed fallback, `SUPABASE_SERVICE_ROLE_KEY` is now required exclusively. See `docs/FIXES.md` for full details. |

---

## Critical Rules — Supabase Key Usage

> **NEVER use the anon key (`SUPABASE_KEY`) for any server-side DB write.**
>
> `flow_store.py` uses **only** `SUPABASE_SERVICE_ROLE_KEY`. This key bypasses Row Level Security (RLS).
> The anon key respects RLS policies and will cause **every insert** to fail with `42501` (policy violation).
> There is NO fallback to the anon key — if `SUPABASE_SERVICE_ROLE_KEY` is missing, `flow_store.py` logs
> a warning and exits cleanly rather than silently using the wrong key.

### Supabase Key Reference

| Key | Env var | Used by | Bypasses RLS? |
|-----|---------|---------|---------------|
| Anon / Public | `SUPABASE_KEY` | `universe_store.py` (reads only) | ❌ No |
| Service Role | `SUPABASE_SERVICE_ROLE_KEY` | `flow_store.py` (writes) | ✅ Yes |

---

## Important Implementation Notes

### Step 3 — Tradier Single-Symbol Dict Edge Case
When only 1 symbol is in a `/v1/markets/quotes` batch, Tradier returns a **dict** instead of a **list** for `quotes.quote`. The loader handles this:
```python
if isinstance(quotes_raw, dict):
    quotes_raw = [quotes_raw]
```

### Step 3 — Price Field Fallback Order
Tradier quote responses use inconsistent field names. The loader tries in order:
`last` → `last_price` → `close` → `prevclose`

### upsert_symbol_quotes() Timing
`upsert_symbol_quotes()` is called from `load_universe()` BEFORE `save_snapshot()`.
If no active snapshot exists yet (first ever startup), it logs a warning and is a no-op.
The `stream_eligible` flag written by `save_snapshot()` is authoritative — it uses
the `eligible_set` returned by `_fetch_batch_quotes()` directly.

### universe_screener.py
Kept in the repo for reference and backward test compatibility.
`screen_universe()` emits a deprecation warning log if called.
Do NOT re-add a call to it from `load_universe()`.

### flow_store.py — Key Selection
`flow_store.py` is the **only** module that writes options flow data to the DB.
It **must** use `SUPABASE_SERVICE_ROLE_KEY`. Never introduce a fallback to `SUPABASE_KEY` here.
See the Critical Rules section above.

---

## Environment Variables (Full List)

```
# Auth
SECRET_KEY=
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# Supabase
SUPABASE_URL=
SUPABASE_KEY=                      # anon key — used by universe_store.py (reads)
SUPABASE_SERVICE_ROLE_KEY=          # service role key — REQUIRED by flow_store.py (writes)

# Tradier
TRADIER_API_KEY=
TRADIER_ACCOUNT_ID=
TRADIER_BASE_URL=https://api.tradier.com
TRADIER_STREAM_URL=https://stream.tradier.com

# AI
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GROQ_API_KEY=

# Misc
REDIS_URL=redis://localhost:6379
ALLOWED_ORIGINS=http://localhost:3000

# Universe pipeline
UNIVERSE_PRIORITY_SYMBOLS=SPY,QQQ,AAPL,TSLA,NVDA,MSFT,AMZN,META,GOOGL,AMD
UNIVERSE_BATCH_DELAY_MS=0
UNIVERSE_STREAM_ELIGIBLE_DEFAULT=true
UNIVERSE_MIN_PRICE=1.0
UNIVERSE_MIN_VOLUME=100000
UNIVERSE_QUOTES_BATCH_SIZE=200
UNIVERSE_QUOTES_CONCURRENCY=28
```
