# SPRINT: WSJ Ingestion Alignment — P0
**Priority:** HIGHEST — blocks all downstream signal quality
**Sprint Goal:** Align the ingestion pipeline (Gates 1–11) with the WallStreetJesus repeat-flow methodology before any signal layer work begins.
**Review Requirement:** ⚠️ EVERY story in this sprint requires a 3-way deliberation session before implementation begins:
- **Senior Architect (SA)** — architectural impact, data flow, registry coupling
- **Principal Backend Engineer (PBE)** — implementation correctness, hot-path safety, regression risk
- **Lead QA (QA)** — test coverage, observable stat counters, regression test additions

No story moves to `In Progress` without sign-off from all three roles.

---

## Sprint Order (Strict — Dependencies Enforced)

| Order | Story ID | Title | Depends On | Can Ship? |
|-------|----------|-------|------------|-----------|
| 1 | **ING-001** | Verify Tradier timesale `order_side` field | — | After API check |
| 2 | **ING-002** | Hard per-event $10k premium floor at parser | — | ✅ NOW |
| 3 | **ING-003** | Wire `_DEFAULT_DTE_PREMIUM_TIERS` at accumulator init | — | ✅ NOW |
| 4 | **ING-004** | Fallback `underlying_price` from registry | — | ✅ NOW |
| 5 | **ING-005** | Align OTM band thresholds registry ↔ accumulator | ING-004 | After ING-004 |
| 6 | **ING-006** | Directional aggression weighting on premium floor | ING-001 | After ING-001 confirmed |
| 7 | **ING-007** | Multi-day repeat window lookback (DB + cache) | ING-002, ING-003 | After infra prereqs |
| 8 | **ING-008** | Volume vs. OI gate via registry injection | ING-004, ING-005 | After ING-004+005 |

---

## Story Detail

---

### ING-001 — Verify Tradier Timesale `order_side` Field
**Type:** Research / Spike
**Priority:** P0 — BLOCKER for ING-006
**Estimated Effort:** 0.5 day

#### Context
The entire directional aggression classification system (`order_side_to_direction()` in `parsers/order_side_classifier.py`, `dominant_direction` on episodes, and the proposed C1 directional aggression weighting in ING-006) assumes `order_side` is available per tick. Currently `order_side` is **never extracted** from the raw Tradier timesale payload in `parse_tradier_trade()`. It defaults to `"UNKNOWN"` on every single event, meaning `dominant_direction` on all episodes is determined solely by contract type — `order_side_to_direction()` is functionally dead code today.

#### 🔴 Tradier API Verification Required
**Action:** Dump a raw timesale tick from Railway production logs and check whether any of the following fields appear in the JSON payload:
- `order_side`
- `side`
- `aggressor_side`
- `trade_condition`
- `condition`

**How to verify:**
1. SSH into Railway or use Railway log tail
2. Temporarily add `log.debug("[raw_tick] %s", json.dumps(raw))` to `_process_trade()` before the Gate 1 event_type check
3. Capture 5–10 ticks during market hours
4. Check raw JSON for any side/aggression field

**Expected Tradier timesale fields (known):** `type`, `symbol`, `price`, `bid`, `ask`, `size`, `date`, `exchange`, `last`, `open`, `high`, `low`, `close`, `prevclose`

**If `order_side` or equivalent IS present:**
- Proceed to ING-006 with the confirmed field name
- Add field extraction to `parse_tradier_trade()`
- Add `order_side: str = "UNKNOWN"` to `OptionsFlowEvent` dataclass

**If `order_side` IS NOT present in Tradier stream:**
- ING-006 partial path (BUY CALL/PUT at-ask) still ships — only SELL PUT/CALL at-bid aggression detection is blocked
- Document limitation in `docs/ARCHITECTURE.md`
- Evaluate OCC Options Data API as alternative source (see below)

#### 🔴 OCC API Verification (Fallback)
If Tradier does not provide `order_side`, check whether the **OCC (Options Clearing Corporation) public data feed** or a supplemental data provider (e.g., OPRA, CBOE LiveVol, or Polygon.io options trades endpoint) exposes aggressor side per print.

**Polygon.io options trades endpoint** (`/v3/trades/{optionsTicker}`) reportedly includes a `conditions` array — verify if condition codes map to buy/sell aggressor.

**Deliberation Questions for SA + PBE + QA:**
1. If Tradier never sends `order_side`, is a heuristic inference acceptable? (e.g., fill consistently ≥ ask → infer BUY, fill ≤ bid → infer SELL)
2. Should we add a secondary data source purely for `order_side` enrichment, or accept the limitation?
3. What stat counter do we add to `_stats{}` to make UNKNOWN order_side visible in the health endpoint?

#### Acceptance Criteria
- [ ] Raw Tradier tick logged and inspected — field presence documented
- [ ] Decision recorded in `docs/FIXES.md` under ING-001
- [ ] If field found: field name confirmed and extraction path scoped for ING-006
- [ ] If field not found: fallback strategy agreed by SA + PBE + QA before ING-006 scoping

---

### ING-002 — Hard Per-Event $10k Premium Floor at Parser
**Type:** Feature / Gate Addition
**Priority:** P0
**Estimated Effort:** 0.5 day
**Depends On:** Nothing — ship immediately after deliberation
**Files:** `backend/parsers/options_flow_parser.py`, `backend/services/tradier_stream.py`

#### Context
Currently a single $11k mid-print on a 2-DTE contract passes all parser gates and enters the accumulator. The DTE-adjusted floor (Gate 6) only activates after registry warmup (~30 min post-deploy). Before warmup, the flat `min_premium=10_000` fallback means junk clears every gate. A hard floor at the parser level, independent of the accumulator and registry state, eliminates sub-threshold noise from ever entering `flow_events`.

#### Implementation

**Step 1 — Add constant and gate in `options_flow_parser.py`:**
```python
# After premium is calculated (fill * size * 100):
_MIN_EVENT_PREMIUM = 10_000  # $10k hard floor — parser-level gate

if premium < _MIN_EVENT_PREMIUM:
    return None  # drop before building OptionsFlowEvent
```

**Step 2 — Add dedicated stat counter in `tradier_stream.py`:**
```python
# In _stats dict initialisation:
"below_min_premium": 0,

# In _process_trade(), where parse_tradier_trade() returns None:
# Distinguish parse_failed from below_min_premium:
if ev is None:
    # Check if it was a premium floor drop vs genuine parse failure
    # (Parser should set a module-level flag or return a sentinel)
    _stats["parse_failed"] += 1
```

> **Note for PBE deliberation:** The cleanest approach is to have `parse_tradier_trade()` return a typed result: `OptionsFlowEvent | Literal["below_premium"] | None`. This avoids ambiguity in the `_stats` counter. Discuss whether to add the typed sentinel or use a module-level side-channel flag.

**Step 3 — Expose counter in `/health/stream` endpoint.**

#### Deliberation Questions for SA + PBE + QA
1. Should the floor be configurable via `ingestion_config` (DB-driven) or a hardcoded constant? Configurable is safer long-term but adds warmup dependency.
2. Should sub-floor ticks still be counted in `flow_events` with a `filtered=true` flag for analytics, or dropped entirely?
3. QA: What regression tests cover the premium floor path? Add to `REGRESSION_TESTING.md`.

#### Acceptance Criteria
- [ ] `parse_tradier_trade()` returns `None` for any event where `premium < 10_000`
- [ ] `_stats["below_min_premium"]` increments correctly — does NOT pollute `parse_failed`
- [ ] Counter visible in `/health/stream` response
- [ ] Unit test: feed tick with size=1, fill=50.00 (premium=$5k) → assert returns None
- [ ] Unit test: feed tick with size=2, fill=55.00 (premium=$11k) → assert returns OptionsFlowEvent
- [ ] Regression: existing parse tests still pass

---

### ING-003 — Wire `_DEFAULT_DTE_PREMIUM_TIERS` at Accumulator Instantiation
**Type:** Bug Fix / Configuration
**Priority:** P0
**Estimated Effort:** 0.25 day
**Depends On:** Nothing — ship immediately after deliberation
**Files:** `backend/services/tradier_stream.py`

#### Context
The accumulator is currently instantiated with `dte_premium_tiers=None`. This means `_get_episode_min_premium()` falls back to the flat `min_premium=10_000` for all events until the registry warms up and `set_dte_premium_tiers()` is called by stream workers. During the first ~30 minutes after a cold deploy on a trading day, every event above $10k clears Gate 6 regardless of DTE. A $12k 2-DTE lottery ticket clears the same as a $500k 45-DTE institutional print.

`_DEFAULT_DTE_PREMIUM_TIERS` is already defined in `repetition_accumulator.py` and is the correct default. It just needs to be passed at instantiation rather than injected post-init.

#### Implementation

```python
# In tradier_stream.py, accumulator instantiation:
from signals.repetition_accumulator import RepetitionAccumulator, _DEFAULT_DTE_PREMIUM_TIERS

accumulator = RepetitionAccumulator(
    window_minutes=30,
    min_trades=1,
    min_premium=10_000,
    dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,  # active from tick 1, not post-warmup
    min_sweeps=0,
    signal_cooldown=0,
)
```

The tier map (`_tier_map`) still defaults unknown tickers to T1 (strictest floor) until the registry confirms their tier — this is the safe default direction (too strict rather than too permissive).

#### Deliberation Questions for SA + PBE + QA
1. SA: Does wiring the DTE tiers before registry warmup have any unintended side effects on episode formation rate at cold-start? Specifically — will T1 floors drop legitimate early-session flow that would have passed post-warmup?
2. PBE: Confirm `_DEFAULT_DTE_PREMIUM_TIERS` is exported from `repetition_accumulator.py` (check `__all__` or module-level visibility).
3. QA: Add cold-start scenario test — simulate 5 ticks before registry warmup, assert DTE tier floors are applied correctly.

#### Acceptance Criteria
- [ ] Accumulator instantiated with `dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS`
- [ ] Unit test: tick with DTE=5 on T1 ticker → requires $50k premium to pass Gate 6 from tick 1
- [ ] Unit test: tick with DTE=5 on T1 ticker, premium=$30k → returns None (below $50k floor)
- [ ] No regression in existing accumulator tests

---

### ING-004 — Fallback `underlying_price` From Registry When Tick Has Zero
**Type:** Bug Fix
**Priority:** P0
**Estimated Effort:** 0.5 day
**Depends On:** Nothing — ship immediately after deliberation
**Files:** `backend/parsers/options_flow_parser.py`, `backend/services/symbol_registry.py`

#### Context
`underlying_price` on `OptionsFlowEvent` comes from `raw.get("underlying_price", 0)` in the Tradier timesale payload. Tradier frequently omits this field in timesale events, leaving `underlying_price = 0.0`. When `_classify_otm(strike, 0.0)` is called in the accumulator, it returns `"UNKNOWN"` and the DEEP_OTM 1.5x premium multiplier (Gate 7) is silently bypassed for every such tick — which is likely the majority of ticks.

`SymbolRegistry` already has a `stock_price(ticker: str) -> float` method that returns the price fetched at build time and refreshed every refresh cycle. This is the correct fallback.

#### Implementation

```python
# In parse_tradier_trade(), after OptionsFlowEvent ev is constructed
# and after registry lookup (which already sets ticker):

if ev.underlying_price == 0.0:
    from services.symbol_registry import get_registry
    _reg = get_registry()
    if _reg and _reg.is_ready():
        _sp = _reg.stock_price(ev.ticker)
        if _sp > 0:
            ev.underlying_price = _sp
```

> **PBE Note:** `get_registry()` is a module-level singleton getter — no async call, no IO. This is safe on the hot path. Confirm import does not create circular dependency between `parsers/` and `services/`.

#### Deliberation Questions for SA + PBE + QA
1. SA: Is the registry singleton safe to call from inside the parser without creating a tight coupling that breaks unit testability? Should `stock_price` be passed in as a parameter instead?
2. PBE: Confirm `get_registry()` does not block or have lock contention on the hot path. The `_stock_prices` dict is read-only between builds — verify no write lock needed for reads.
3. QA: How do we test this? Need a mock registry fixture that returns a known stock price. Add test: `underlying_price=0` in raw tick + mock registry returning 150.0 → assert `ev.underlying_price == 150.0`.

#### Acceptance Criteria
- [ ] `ev.underlying_price` is populated from registry fallback when raw tick has `underlying_price=0`
- [ ] Fallback only fires when `registry.is_ready()` is True (no fallback during cold-start)
- [ ] No circular import introduced between `parsers/` and `services/`
- [ ] Unit test: raw tick with `underlying_price=0`, mock registry → assert fallback applied
- [ ] Unit test: raw tick with `underlying_price=150.0` → assert registry NOT called (no unnecessary call)
- [ ] Unit test: raw tick with `underlying_price=0`, registry not ready → assert `ev.underlying_price` stays 0.0

---

### ING-005 — Align OTM Band Thresholds: Registry ↔ Accumulator
**Type:** Bug Fix / Consistency
**Priority:** P1
**Estimated Effort:** 1 day
**Depends On:** ING-004 (needs reliable `underlying_price` before OTM classification is meaningful)
**Files:** `backend/signals/repetition_accumulator.py`, `backend/services/tradier_stream.py`

#### Context
The registry filters contracts at build time using tier-specific `atm_pct`:
- T1: ±20% OTM (`t1_atm_pct = 0.20`)
- T2: ±15% OTM (`t2_atm_pct = 0.15`)
- T3: ±10% OTM (`t3_atm_pct = 0.10`)

The accumulator's `_classify_otm()` uses hardcoded thresholds of **2% (ATM boundary)** and **12% (DEEP_OTM boundary)** — completely independent of tier. This means:
- A T1 contract at 18% OTM is within the registry's T1 inclusion zone, but the accumulator classifies it as `DEEP_OTM` and applies a 1.5x premium penalty
- A T3 contract at exactly 10% OTM sits on the DEEP_OTM/STANDARD boundary in the accumulator despite being at the T3 outer edge

These are inconsistent and will suppress legitimate T1 large-cap flow with the wrong multiplier.

#### Implementation Options (Deliberation Required)

**Option A — Retire DEEP_OTM multiplier entirely:**
The registry already pre-filters OTM at the tier level. Any contract that cleared the registry IS within the acceptable OTM window for its tier. The DEEP_OTM multiplier is therefore double-counting a filter that already happened. Remove `_classify_otm()` gate and set `deep_otm_multiplier=1.0`.

**Option B — Pass tier `atm_pct` into the accumulator:**
Update `ingest_tick()` to receive the event's contract tier and use the registry's `atm_pct` threshold for that tier as the DEEP_OTM boundary instead of the hardcoded 12%.

```python
# In _classify_otm(), replace hardcoded 0.12:
def _classify_otm(self, strike: float, underlying: float, tier_atm_pct: float) -> str:
    if underlying <= 0:
        return "UNKNOWN"
    pct = abs(strike - underlying) / underlying
    if pct <= 0.02:
        return "ATM"
    if pct <= tier_atm_pct * 0.75:   # e.g. T1: 15%, T2: 11.25%
        return "STANDARD_OTM"
    return "DEEP_OTM"
```

**Option C — Keep multiplier but fix the threshold to match T2 (15%):**
Simple constant change: replace `0.12` with `0.15` as a middle-ground that roughly matches T2.

#### 🔴 Deliberation Required — SA + PBE + QA Must Decide Option A, B, or C
This is an architectural decision with signal volume implications. Option A reduces pipeline complexity. Option B is most correct but adds coupling. Option C is a quick patch.

#### Acceptance Criteria (regardless of option chosen)
- [ ] OTM classification thresholds are documented and consistent with registry tier definitions
- [ ] Unit test: T1 contract at 18% OTM → correct classification per chosen option
- [ ] Unit test: T3 contract at 10% OTM → correct classification per chosen option
- [ ] No change to existing passing accumulator tests without explicit update

---

### ING-006 — Directional Aggression Weighting on Premium Floor
**Type:** Feature / Gate Enhancement
**Priority:** P0
**Estimated Effort:** 1.5 days
**Depends On:** ING-001 (must confirm `order_side` field availability before full implementation)
**Files:** `backend/parsers/options_flow_parser.py`, `backend/parsers/bid_ask_classifier.py`, `backend/signals/repetition_accumulator.py`, `backend/models/flow_event.py` (OptionsFlowEvent dataclass)

#### Context
The current `is_aggressive` flag is a **fill-placement-only classifier** (`AT_ASK` or `ABOVE_ASK` = True). This misses the most important aggressive pattern in WSJ's methodology: **SELL PUT at bid** (conviction bullish — put writer taking assignment risk) and **SELL CALL at bid** (conviction bearish — call writer). Both are directionally decisive but marked `is_aggressive=False` today.

Additionally, `is_aggressive` is not used anywhere in the accumulator's premium floor calculation — passive prints accumulate premium toward the floor identically to aggressive prints.

#### 🔴 Tradier API Dependency
This story's SELL PUT / SELL CALL paths are **blocked on ING-001**. If Tradier does not expose `order_side`, those paths cannot fire. The BUY CALL / BUY PUT at-ask path ships regardless.

#### Implementation

**Step 1 — Add `is_directionally_aggressive()` to `bid_ask_classifier.py`:**
```python
def is_directionally_aggressive(
    bid_ask_class: str,
    order_side: str,
    contract_type: str,
) -> bool:
    """
    True when fill reflects committed directional intent:
      BUY  CALL / PUT  at or above ask  → aggressive opener
      SELL PUT         at or below bid  → conviction bullish (put writer)
      SELL CALL        at or below bid  → conviction bearish (call writer)
    Mid-prints and UNKNOWN order_side always return False.
    """
    side  = (order_side or "").strip().upper()
    ctype = (contract_type or "").strip().upper()
    ba    = (bid_ask_class or "").strip().upper()

    if side == "BUY" and ba in ("AT_ASK", "ABOVE_ASK"):
        return True
    if side == "SELL" and ctype == "PUT" and ba in ("AT_BID", "BELOW_BID"):
        return True
    if side == "SELL" and ctype == "CALL" and ba in ("AT_BID", "BELOW_BID"):
        return True
    return False
```

**Step 2 — Add `order_side` to `OptionsFlowEvent` dataclass:**
```python
order_side: str = "UNKNOWN"   # BUY | SELL | UNKNOWN
```

**Step 3 — Extract `order_side` from raw tick in `parse_tradier_trade()`:**
```python
# Only after ING-001 confirms the field name:
order_side = (
    raw.get("order_side") or
    raw.get("side") or
    raw.get("aggressor_side") or
    "UNKNOWN"
).upper()
ev.order_side = order_side
```

**Step 4 — Replace `is_aggressive` assignment in `parse_tradier_trade()`:**
```python
# OLD:
ev.is_aggressive = is_aggressive(ba_class)

# NEW:
ev.is_aggressive = is_directionally_aggressive(ba_class, order_side, contract_type)
```

**Step 5 — Add `is_aggressive` + `order_side` to `_DictEventWrapper.__slots__` in `repetition_accumulator.py`:**
```python
__slots__ = (
    ...,
    "is_aggressive",
    "order_side",
)

# In __init__:
self.is_aggressive = d.get("is_aggressive", False)
self.order_side    = d.get("order_side", "UNKNOWN")
```

**Step 6 — Add aggression discount to premium floor check in `ingest_tick()`:**
```python
# In RepetitionAccumulator.__init__:
self.aggression_discount = aggression_discount  # default 0.5

# Replace direct premium floor check:
aggressive_premium = sum(
    getattr(e, "premium", 0.0)
    for e in ep.events
    if getattr(e, "is_aggressive", False)
)
passive_premium = ep.total_premium - aggressive_premium
weighted_premium = aggressive_premium + (passive_premium * self.aggression_discount)

if weighted_premium < effective_min_prem:
    return None
```

#### Deliberation Questions for SA + PBE + QA
1. SA: Should `order_side` be persisted to `flow_events` DB table as a new column, or kept in-memory only? Persisting enables backtesting and multi-day repeat analysis (ING-007).
2. PBE: The `_DictEventWrapper` pattern reconstructs events from dicts — confirm all new fields are correctly passed through the dict→wrapper conversion path.
3. QA: What is the expected ratio of UNKNOWN vs BUY/SELL order_side in production? Add a `_stats["order_side_unknown"]` counter so we can monitor data quality post-deploy.

#### Acceptance Criteria
- [ ] `is_directionally_aggressive()` function exists in `bid_ask_classifier.py` with full test coverage
- [ ] `order_side` field added to `OptionsFlowEvent` with default `"UNKNOWN"`
- [ ] `is_aggressive` on events set via `is_directionally_aggressive()`, not `is_aggressive()` (old function)
- [ ] Accumulator premium floor uses aggression-weighted premium, not raw `total_premium`
- [ ] `_stats["order_side_unknown"]` counter tracking unknown order_side events
- [ ] Unit tests: SELL PUT AT_BID → `is_directionally_aggressive=True`; MID UNKNOWN CALL → False
- [ ] Unit test: episode with 50% aggressive premium, 50% passive → weighted floor = aggressive + 50% passive
- [ ] Old `is_aggressive(trade_type)` function retained but deprecated (do not delete — may be used elsewhere)

---

### ING-007 — Multi-Day Repeat Window Lookback
**Type:** Feature / New Gate
**Priority:** P1 — core WSJ repeat detection
**Estimated Effort:** 3 days
**Depends On:** ING-002 (min premium floor must be in place before multi-day lookback — prevents noise from polluting the historical view), ING-003 (DTE tiers active)
**Files:** `backend/services/flow_store.py`, `backend/services/tradier_stream.py`, Supabase migration

#### Context
The current 30-minute rolling window accumulator cannot detect WSJ-style repeat patterns, which by definition span multiple days (same contract printing multiple times across 2–5 trading days). Two events on the same AAPL $200C 2026-06-20 contract — one on Monday and one on Wednesday — are treated as entirely independent single-tick episodes. This is the most critical structural gap relative to WSJ methodology.

#### ⚠️ Infrastructure Prerequisite — Must Complete Before Any Code
**Supabase migration required FIRST:**
```sql
-- Migration: add_flow_events_contract_day_index
CREATE INDEX idx_flow_events_contract_day
ON flow_events (ticker, contract_type, strike, expiry, created_at DESC);

-- Also add order_side column if ING-006 decides to persist it:
ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS order_side TEXT DEFAULT 'UNKNOWN';
```
Run `supabase migration new add_flow_events_contract_day_index` locally, apply to prod, verify with `EXPLAIN ANALYZE` on the lookback query before writing any Python.

#### Implementation

**Step 1 — In-memory LRU cache for prior contract volume (avoid hot-path DB call):**
```python
# New file: utils/contract_day_cache.py
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

class PriorVolumeResult(NamedTuple):
    prior_premium:      float
    prior_trade_count:  int
    prior_days_active:  int   # distinct calendar days with at least 1 print

# Cache keyed by (ticker, contract_type, strike_str, expiry, date_str)
# Refreshes every 5 minutes (TTL enforced by cache wrapper, not lru_cache)
# Use cachetools.TTLCache with maxsize=2000, ttl=300
```

**Step 2 — New function `get_prior_contract_volume()` in `flow_store.py`:**
```python
async def get_prior_contract_volume(
    ticker:        str,
    contract_type: str,
    strike:        float,
    expiry:        str,
    lookback_days: int = 5,
) -> PriorVolumeResult:
    """
    Query flow_events for cumulative premium + trade_count + distinct days
    on this contract over the last N calendar days (excluding today).
    Uses idx_flow_events_contract_day index.
    Returns PriorVolumeResult(0, 0, 0) on any error.
    """
```

**Step 3 — Background pre-fetch, NOT hot-path inline call:**
Do NOT call `get_prior_contract_volume()` inside `_process_trade()` directly. Instead:
- After `persist_flow_event()` succeeds, enqueue the OCC symbol into a `asyncio.Queue`
- A background coroutine drains the queue, fetches prior volume, and updates an in-memory `_contract_repeat_cache: dict[str, PriorVolumeResult]`
- `ingest_tick()` in the accumulator reads from `_contract_repeat_cache` (non-blocking dict lookup)

**Step 4 — New gate in accumulator `ingest_tick()` after premium floor:**
```python
if self.multi_day_check_enabled:
    prior = self._contract_repeat_cache.get(ep_key)
    if prior and prior.prior_days_active >= self.min_repeat_days:
        ep.is_multi_day_repeat = True   # enrichment flag, not a hard drop
    elif self.require_multi_day and not ep.is_multi_day_repeat:
        return None  # hard gate — only emit if multi-day repeat confirmed
```

> **SA deliberation note:** Hard gate (`require_multi_day=True`) vs soft enrichment flag is an architectural decision with major signal volume implications. Start with enrichment flag only.

#### Deliberation Questions for SA + PBE + QA
1. SA: Should multi-day repeat detection be a hard gate (drops non-repeat episodes) or a soft enrichment flag (passes everything but marks repeats)? Recommend starting as enrichment flag to avoid suppressing legitimate single-day large prints.
2. PBE: `cachetools.TTLCache` vs custom TTL wrapper — confirm `cachetools` is an acceptable dependency or if stdlib `functools.lru_cache` with a timestamp check is preferred.
3. PBE: The background queue pattern decouples the DB call from the hot path, but introduces eventual consistency — a multi-day repeat may not be flagged on tick N if the background worker hasn't processed it yet. Is this acceptable?
4. QA: How do we integration-test multi-day lookback? Need a seeded `flow_events` fixture with historical rows. Add to regression test suite.

#### Acceptance Criteria
- [ ] Supabase migration applied and `EXPLAIN ANALYZE` confirms index is used
- [ ] `get_prior_contract_volume()` returns correct results for a seeded test dataset
- [ ] In-memory cache populated by background worker, NOT inline in `_process_trade()`
- [ ] `ep.is_multi_day_repeat` flag set correctly on episodes with ≥2 prior days of same-contract flow
- [ ] `_stats["multi_day_repeat_count"]` counter tracking flagged episodes
- [ ] No measurable latency increase on `_process_trade()` hot path (benchmark before/after)
- [ ] Unit test: mock cache with 2 prior days → `ep.is_multi_day_repeat = True`
- [ ] Unit test: mock cache empty → `ep.is_multi_day_repeat = False`

---

### ING-008 — Volume vs. OI Gate via Registry Injection
**Type:** Feature / New Gate
**Priority:** P1
**Estimated Effort:** 2 days
**Depends On:** ING-004 (registry integration pattern established), ING-005 (OTM classification stable before adding another registry-sourced field)
**Files:** `backend/signals/repetition_accumulator.py`, `backend/services/symbol_registry.py`, `backend/models/flow_event.py`

#### Context
WSJ uses **volume > open interest** as a confirmation that options flow represents new directional positioning rather than existing position management or hedging churn. Currently `open_interest` on `OptionsFlowEvent` comes from the raw Tradier timesale tick (`raw.get("open_interest", 0)`) which is **almost always 0** — Tradier does not include per-contract OI in timesale events. The OI data exists in `ContractMeta` inside `SymbolRegistry`, fetched from the full options chain at build time.

The gate: episode's cumulative `size` (total contracts traded) must reach or exceed the OI for that contract before it confirms as a genuine new-position signal.

#### 🔴 OCC / Tradier Chain API Data Verification Required
**Action before implementation:** Verify that `open_interest` in the options chain response from `get_option_chain_bulk()` is reliably populated for T1/T2 tickers.

**How to verify:**
1. Run a one-off chain fetch for a known liquid ticker (e.g., AAPL, SPY) using the existing `get_option_chain_bulk()` utility
2. Print OI values for 5–10 contracts across different expirations
3. Confirm OI is non-zero for near-term liquid strikes
4. Check if OI is stale (end-of-prior-day) or intraday-updated

**Expected behavior:** OI from chain fetch = prior day's OI (standard — exchanges report OI daily after settlement). This is acceptable since WSJ's vol > OI check is a directional signal, not a precision metric.

#### Implementation

**Step 1 — Add `open_interest` to `_DictEventWrapper.__slots__`:**
```python
__slots__ = (..., "open_interest")
self.open_interest = d.get("open_interest", 0)
```

**Step 2 — In `parse_tradier_trade()`, source OI from registry if tick OI = 0:**
```python
# After registry lookup, if meta is found:
if meta and ev.open_interest == 0:
    ev.open_interest = meta.open_interest  # from ContractMeta, populated at chain build
```

**Step 3 — Add vol/OI gate to `ingest_tick()` in accumulator:**
```python
if self.vol_oi_check_enabled and registry_is_ready:
    total_size  = sum(getattr(e, "size", 0) for e in ep.events)
    latest_oi   = getattr(ep.events[-1], "open_interest", 0)
    if latest_oi > 0 and total_size < latest_oi * self.vol_oi_min_ratio:
        _stats["vol_oi_suppressed"] += 1
        return None
```

Default: `vol_oi_min_ratio = 1.0`, `vol_oi_check_enabled = False` (soft launch — enable via config after validating signal rate impact).

**Step 4 — Add `vol_oi_suppressed` counter to `_stats` and `/health/stream`.**

#### Deliberation Questions for SA + PBE + QA
1. SA: OI from chain build is end-of-prior-day. Intraday volume from episode accumulation is real-time. The ratio is therefore directionally correct but not same-snapshot precise. Is this acceptable, or do we need intraday OI updates (which would require a periodic chain re-fetch)?
2. PBE: Should `vol_oi_check_enabled` default to `False` and be activated via `ingestion_config` after a 1-week signal rate observation period? Recommend yes.
3. QA: OI = 0 is a known failure mode. Add explicit test: OI=0 on event → gate skipped (not triggered). OI=500, total_size=300 → gate fires, episode dropped.

#### Acceptance Criteria
- [ ] `open_interest` on `OptionsFlowEvent` sourced from `ContractMeta` registry when tick OI = 0
- [ ] `_DictEventWrapper` includes `open_interest` in `__slots__`
- [ ] Vol/OI gate implemented but **disabled by default** (`vol_oi_check_enabled=False`)
- [ ] Gate can be enabled via `ingestion_config` without code deploy
- [ ] `_stats["vol_oi_suppressed"]` counter exposed in `/health/stream`
- [ ] Unit test: OI=0 → gate skipped
- [ ] Unit test: OI=500, total_episode_size=300, ratio=1.0 → episode dropped
- [ ] Unit test: OI=500, total_episode_size=600, ratio=1.0 → episode passes
- [ ] OCC/Tradier chain API OI data quality verified and documented in `docs/FIXES.md`

---

## Sprint Exit Criteria
All 8 stories pass their acceptance criteria AND:
- [ ] No regression in existing passing tests (`pytest backend/`)
- [ ] `/health/stream` shows all new counters (`below_min_premium`, `order_side_unknown`, `multi_day_repeat_count`, `vol_oi_suppressed`)
- [ ] 3-way deliberation sign-off documented for every story (record in `docs/FIXES.md` under story ID)
- [ ] `docs/ARCHITECTURE.md` updated to reflect new gate structure
- [ ] `docs/CHANGELOG.md` updated with sprint summary

---

*Sprint created: 2026-05-03 | Owner: Dhruv Patel | Classification: P0 — WSJ Ingestion Alignment*
