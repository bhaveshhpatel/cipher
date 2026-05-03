# SPRINT: WSJ Ingestion Alignment — P0
**Priority:** HIGHEST — blocks all downstream signal quality
**Sprint Goal:** Align the ingestion pipeline (Gates 1–11) with the WallStreetJesus repeat-flow methodology before any signal layer work begins.
**Review Requirement:** ⚠️ EVERY story in this sprint requires a 3-way deliberation session before implementation begins:
- **Senior Architect (SA)** — architectural impact, data flow, registry coupling
- **Principal Backend Engineer (PBE)** — implementation correctness, hot-path safety, regression risk
- **Lead QA (QA)** — test coverage, observable stat counters, regression test additions

No story moves to `In Progress` without sign-off from all three roles.

---

## Resolved Pre-Sprint Research

### ING-001 — Tradier `order_side` Field — ✅ CLOSED (Resolved Before Sprint Start)
**Finding:** Tradier's timesale WebSocket stream does **not** include `order_side`, `side`, or `aggressor_side` in the tick payload. This is a platform-level limitation — Tradier's documented timesale fields are: `type`, `symbol`, `exchange`, `bid`, `ask`, `last`, `size`, `date`, `open`, `high`, `low`, `close`, `prevclose`. No aggressor-side field exists.

**Resolution:** Fill-placement relative to the bid/ask spread is the industry-standard proxy for aggression when true `order_side` is unavailable. CBOE LiveVol, Unusual Whales, and all major retail options flow tools use this same heuristic. WallStreetJesus himself almost certainly uses fill-at-ask as his aggression proxy since true `order_side` requires OPRA full-feed access (institutional-tier cost).

**Impact on ING-006:** The `order_side` parameter is **removed** from `is_directionally_aggressive()`. Aggression is determined entirely from `bid_ask_class + contract_type`:
- `AT_ASK` / `ABOVE_ASK` on any contract type → aggressive (buyer paying up)
- `AT_BID` / `BELOW_BID` on PUT → conviction bullish (put seller writing at bid)
- `AT_BID` / `BELOW_BID` on CALL → conviction bearish (call seller writing at bid)
- `MID` on anything → passive / ambiguous

This is actually **more correct** for WSJ purposes than `order_side` alone — put selling at bid IS aggressive bullish positioning regardless of exchange-reported aggressor flag.

**Documented in:** `docs/ORDER_SIDE_RESOLUTION.md`

---

## Sprint Order (Strict — Dependencies Enforced)

| Order | Story ID | Title | Depends On | Can Ship? |
|-------|----------|-------|------------|-----------|
| ~~1~~ | ~~ING-001~~ | ~~Verify Tradier `order_side` field~~ | — | ✅ CLOSED — resolved pre-sprint |
| 1 | **ING-002** | Hard per-event $10k premium floor at parser | — | ✅ NOW — deliberation complete (2026-05-03) |
| 2 | **ING-003** | Wire `_DEFAULT_DTE_PREMIUM_TIERS` at accumulator init | — | ✅ NOW |
| 3 | **ING-004** | Fallback `underlying_price` from registry | — | ✅ NOW |
| 4 | **ING-005** | Align OTM band thresholds registry ↔ accumulator | ING-004 | After ING-004 |
| 5 | **ING-006** | Directional aggression weighting on premium floor | ~~ING-001~~ resolved | ✅ UNBLOCKED — ships after ING-002 |
| 6 | **ING-007** | Multi-day repeat window lookback (DB + cache) | ING-002, ING-003 | After infra prereqs |
| 7 | **ING-008** | Volume vs. OI gate via registry injection | ING-004, ING-005 | After ING-004+005 |

---

## Story Detail

---

### ING-002 — Hard Per-Event $10k Premium Floor at Parser
**Type:** Feature / Gate Addition
**Priority:** P0
**Estimated Effort:** 0.5 day
**Depends On:** Nothing — ship immediately
**Files:** `backend/parsers/options_flow_parser.py`, `backend/services/tradier_stream.py`
**GitHub Issue:** [#57](https://github.com/bhaveshhpatel/cipher/issues/57)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-03)
**All three roles signed off. Story cleared for implementation.**

#### Deliberation Outcomes

**SA-Q1: Hardcoded vs. DB-driven floor — DECIDED: Hardcoded now, admin-configurable later**
- `_MIN_EVENT_PREMIUM = 10_000` defined at module level in `options_flow_parser.py`
- Floor is active at import time — no DB dependency, no cold-start gap
- Future path: when admin config page is built, wire through `ingestion_config` key `"min_event_premium"` with `10_000` as hardcoded cold-start fallback
- Follow-up story filed: **ING-002-CONFIG** (see bottom of document)
- Do NOT add TODO comments in code — the follow-up story is the tracking mechanism

**SA-Q2: Floor placement — DECIDED: Parser only, after dedup, gate order unchanged**
- Dedup cache operates on raw tick before premium is known — floor cannot apply there
- Current gate order: `dedup → parse → accumulate → persist` is correct
- `_MIN_EVENT_PREMIUM` gate fires inside `parse_tradier_trade()` after `premium = fill * size * 100`
- No gate reordering needed

**SA-Q3 (found in code review): Caller in `_process_trade()` currently uses `if not ev` — CRITICAL**
- `"below_premium"` sentinel is truthy — `if not ev` will NOT catch it
- If not fixed: sentinel passes through, hits `ev.ticker`, and crashes silently
- **Caller update is mandatory and in-scope for this PR**
- `_process_trade()` must check `result == "below_premium"` BEFORE the `if not ev` / `parse_failed` branch

**PBE-Q1: Sentinel vs. exception vs. dataclass — DECIDED: Sentinel**
- Return type: `Union[OptionsFlowEvent, Literal["below_premium"], None]`
- Named exception adds try/except overhead on the hot path
- Dataclass adds complexity for a 0.5-day story
- Caller must handle 3-state return — enforced in this PR

**PBE-Q2: Other callers of `parse_tradier_trade()` — DECIDED: Audit required before merge**
- Only production caller is `_process_trade()` in `tradier_stream.py`
- Unit tests asserting `result is None` for below-floor inputs must be updated to assert `result == "below_premium"`
- All test callers must be audited before PR merges

**PBE-Q3: Gate placement — DECIDED: Earliest possible exit after premium is known**
- Gate fires after `size == 0` guard and after `premium = fill * size * 100`
- Before OCC symbol parsing, before `OptionsFlowEvent` construction

**QA-Q1: Boundary value test matrix — ALL 6 CASES REQUIRED:**

| Input | Expected return | Counter impact |
|---|---|---|
| `size=1, fill=50.00` → premium=$5,000 | `"below_premium"` | `below_min_premium` +1, `parse_failed` unchanged |
| `size=1, fill=99.99` → premium=$9,999 | `"below_premium"` | `below_min_premium` +1 |
| `size=1, fill=100.00` → premium=$10,000 | `OptionsFlowEvent` | passes (floor is exclusive `<`) |
| `size=1, fill=100.01` → premium=$10,001 | `OptionsFlowEvent` | passes |
| `size=2, fill=55.00` → premium=$11,000 | `OptionsFlowEvent` | passes |
| `size=0` (existing guard) | `None` | `parse_failed` +1 (existing path unchanged) |

**QA-Q2: `parse_failed` must NOT increment on sentinel returns**
- `parse_failed` = genuine parse error (bad data, missing fields, exception)
- `below_min_premium` = clean filter drop (valid data, intentional gate)
- A surge in `below_min_premium` must not look like a parsing error spike in Railway logs

**QA-Q3: `"below_min_premium": 0` must be in `_stats` init block at module level**
- Key must exist before first tick arrives — no `KeyError` from `/health/stream` on cold start

#### Implementation

**Step 1 — Add constant and gate in `options_flow_parser.py`:**
```python
# Module-level constant — hardcoded safe default.
# Future: wire through ingestion_config key "min_event_premium" with this as fallback (ING-002-CONFIG).
_MIN_EVENT_PREMIUM = 10_000

# Inside parse_tradier_trade(), after: premium = fill * size * 100
# and after: if size == 0: return None
if premium < _MIN_EVENT_PREMIUM:
    return "below_premium"  # sentinel — not a parse error, clean data drop
```

**Step 2 — Return type annotation:**
```python
from typing import Optional, Union, Literal

def parse_tradier_trade(raw: dict) -> Union[OptionsFlowEvent, Literal["below_premium"], None]:
```

**Step 3 — Update caller in `tradier_stream.py`:**
```python
result = parse_tradier_trade(trade_payload)
if result == "below_premium":
    _stats["below_min_premium"] += 1
    return
if result is None:
    _stats["parse_failed"] += 1
    log.info(
        "[flow] parse_tradier_trade returned None for symbol=%r "
        "(size=%s bid=%s ask=%s last=%s) — tick dropped",
        trade_payload.get("symbol"),
        trade_payload.get("size"),
        trade_payload.get("bid"),
        trade_payload.get("ask"),
        trade_payload.get("last"),
    )
    return
ev = result
```

**Step 4 — Add counter to `_stats` init block:**
```python
_stats = {
    ...
    "below_min_premium": 0,  # ING-002: clean drops at parser premium floor
    "parse_failed":      0,
    ...
}
```

**Step 5 — Expose counter in `/health/stream` endpoint.**
`get_stats()` returns `dict(_stats)` — no additional change if key is in init block.

#### Acceptance Criteria
- [ ] `_MIN_EVENT_PREMIUM = 10_000` defined at module level in `options_flow_parser.py`
- [ ] `parse_tradier_trade()` returns `"below_premium"` for `premium < 10_000`
- [ ] Gate fires after `size == 0` guard, after `premium = fill * size * 100`, before OCC parsing and `OptionsFlowEvent` construction
- [ ] Return type annotation updated to `Union[OptionsFlowEvent, Literal["below_premium"], None]`
- [ ] `_stats["below_min_premium"]` initialised to `0` in module-level `_stats` dict
- [ ] `_process_trade()` checks `result == "below_premium"` BEFORE `if not ev` / `parse_failed` branch
- [ ] `_stats["below_min_premium"]` increments on sentinel — does NOT increment `parse_failed`
- [ ] `"below_min_premium"` counter visible in `/health/stream` from first request
- [ ] All 6 QA boundary test cases pass
- [ ] All existing callers of `parse_tradier_trade()` in tests audited — tests asserting `None` for below-floor inputs updated to assert `"below_premium"`
- [ ] All existing parse tests (non-below-floor) pass without modification
- [ ] No regression in `_stats["parse_failed"]` behaviour for genuine parse errors

---

### ING-002-CONFIG — Wire `_MIN_EVENT_PREMIUM` Through Admin Config Page *(Follow-Up — File When Admin Page Story Is In Scope)*
**Type:** Feature / Configuration
**Priority:** P2 — quality of life, not blocking
**Depends On:** ING-002 (merged), Admin config page story
**Scope:**
- Add `"min_event_premium"` key to `ingestion_config` table with default `10_000`
- Update `options_flow_parser.py` to read from config at module init: `_MIN_EVENT_PREMIUM = get_config("min_event_premium", fallback=10_000)`
- Hardcoded constant remains as cold-start fallback — DB read is best-effort
- Surface in admin panel for live floor adjustment without redeploy

---

### ING-003 — Wire `_DEFAULT_DTE_PREMIUM_TIERS` at Accumulator Instantiation
**Type:** Bug Fix / Configuration
**Priority:** P0
**Estimated Effort:** 0.25 day
**Depends On:** Nothing — ship immediately after deliberation
**Files:** `backend/services/tradier_stream.py`

#### Context
The accumulator is instantiated with `dte_premium_tiers=None`. This means `_get_episode_min_premium()` falls back to the flat `min_premium=10_000` for all events until the registry warms up and `set_dte_premium_tiers()` is called by stream workers. During the first ~30 minutes after a cold deploy, every event above $10k clears Gate 6 regardless of DTE. A $12k 2-DTE lottery ticket clears the same floor as a $500k 45-DTE institutional print.

`_DEFAULT_DTE_PREMIUM_TIERS` is already defined in `repetition_accumulator.py`. It just needs to be passed at instantiation.

#### Implementation

```python
# In tradier_stream.py:
from signals.repetition_accumulator import RepetitionAccumulator, _DEFAULT_DTE_PREMIUM_TIERS

accumulator = RepetitionAccumulator(
    window_minutes=30,
    min_trades=1,
    min_premium=10_000,
    dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,  # active from tick 1
    min_sweeps=0,
    signal_cooldown=0,
)
```

Unknown tickers default to T1 (strictest floor) until registry confirms their tier — safe direction is too strict, not too permissive.

#### 3-Way Deliberation Questions
**SA:**
1. Does wiring DTE tiers before registry warmup risk dropping legitimate early-session T2/T3 flow that would pass the correct tier floor post-warmup? Specifically — does defaulting unknown tickers to T1 create a cold-start suppression window that distorts the episode record for the first 30 minutes?
2. Should the pre-warmup default tier be T3 (most permissive) rather than T1, to ensure no flow is dropped before we know the correct tier?

**PBE:**
1. Confirm `_DEFAULT_DTE_PREMIUM_TIERS` is accessible at module level (not inside a class or function scope) — verify import path `from signals.repetition_accumulator import _DEFAULT_DTE_PREMIUM_TIERS` works without triggering side effects.
2. Confirm `set_dte_premium_tiers()` called post-warmup still correctly overrides the default — no double-application.

**QA:**
1. Add cold-start scenario test: simulate 5 ticks at DTE=5 before registry warmup → assert $50k T1 floor applied (not $10k flat fallback).
2. Add post-warmup transition test: warmup fires, tier changes from T1 to T2 for a ticker → assert next tick uses T2 floor ($25k for DTE≤7).

#### Acceptance Criteria
- [ ] Accumulator instantiated with `dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS`
- [ ] Unit test: DTE=5, T1 ticker, premium=$30k pre-warmup → Gate 6 drops (below $50k T1 floor)
- [ ] Unit test: DTE=5, T1 ticker, premium=$60k pre-warmup → Gate 6 passes
- [ ] Post-warmup `set_dte_premium_tiers()` still overrides correctly
- [ ] No regression in existing accumulator tests

---

### ING-004 — Fallback `underlying_price` From Registry When Tick Has Zero
**Type:** Bug Fix
**Priority:** P0
**Estimated Effort:** 0.5 day
**Depends On:** Nothing — ship immediately after deliberation
**Files:** `backend/parsers/options_flow_parser.py`

#### Context
`underlying_price` on `OptionsFlowEvent` comes from `raw.get("underlying_price", 0)`. Tradier frequently omits this field in timesale events, leaving `underlying_price = 0.0`. When `_classify_otm(strike, 0.0)` is called in the accumulator, it returns `"UNKNOWN"` and the DEEP_OTM 1.5x premium multiplier (Gate 7) is silently bypassed. This is likely the majority of ticks.

`SymbolRegistry.stock_price(ticker)` already returns the price fetched at build time, refreshed every cycle. Zero additional infrastructure needed.

#### Implementation

```python
# In parse_tradier_trade(), after ev is constructed:
if ev.underlying_price == 0.0:
    from services.symbol_registry import get_registry
    _reg = get_registry()
    if _reg and _reg.is_ready():
        _sp = _reg.stock_price(ev.ticker)
        if _sp > 0:
            ev.underlying_price = _sp
```

#### 3-Way Deliberation Questions
**SA:**
1. Calling `get_registry()` from inside `parsers/` creates a dependency from the parser layer into the services layer. Does this violate the layered architecture? Should `underlying_price` be injected as a parameter to `parse_tradier_trade()` instead (cleaner but requires caller change)?
2. If the registry is not ready (cold-start), `underlying_price` stays 0.0 and OTM classification stays UNKNOWN — should there be a log warning so cold-start suppression is visible?

**PBE:**
1. `get_registry()` is a module-level singleton read — no IO, no lock. Confirm this is safe to call synchronously inside an async hot path without introducing any await-free blocking risk.
2. Confirm `_stock_prices` dict on `SymbolRegistry` has no write-lock that could block during a concurrent `build()` refresh.

**QA:**
1. Mock registry fixture required: `get_registry()` returning mock with `stock_price("AAPL") = 150.0` and `is_ready() = True`.
2. Three test cases: (a) `underlying_price=0` + ready registry → fallback applied; (b) `underlying_price=150.0` → registry NOT called; (c) `underlying_price=0` + registry not ready → stays 0.0.

#### Acceptance Criteria
- [ ] `ev.underlying_price` populated from `registry.stock_price()` when tick value is 0
- [ ] Fallback only fires when `registry.is_ready()` is True
- [ ] No circular import between `parsers/` and `services/`
- [ ] All three QA test cases pass
- [ ] Log warning emitted when underlying_price stays 0.0 after fallback attempt fails

---

### ING-005 — Align OTM Band Thresholds: Registry ↔ Accumulator
**Type:** Bug Fix / Consistency
**Priority:** P1
**Estimated Effort:** 1 day
**Depends On:** ING-004 (reliable `underlying_price` required before OTM classification is meaningful)
**Files:** `backend/signals/repetition_accumulator.py`, `backend/services/tradier_stream.py`

#### Context
Registry filters contracts at build time using tier-specific `atm_pct` (T1: ±20%, T2: ±15%, T3: ±10%). The accumulator's `_classify_otm()` uses hardcoded thresholds of 2% (ATM) and 12% (DEEP_OTM) — completely independent of tier.

Result: A T1 contract at 18% OTM cleared the registry (within ±20%) but the accumulator classifies it DEEP_OTM and applies a 1.5x premium penalty. This incorrectly penalises contracts the registry deliberately included.

#### Implementation Options

**Option A — Retire DEEP_OTM multiplier entirely (Recommended):**
The registry already pre-filters OTM at the tier level. Any contract that cleared `_build_ticker()` IS within the acceptable OTM window. The accumulator's DEEP_OTM gate double-counts a filter already applied. Remove `_classify_otm()` gate, set `deep_otm_multiplier=1.0`.

**Option B — Pass tier `atm_pct` into accumulator:**
Update `_classify_otm()` to accept `tier_atm_pct` and use `tier_atm_pct * 0.75` as the DEEP_OTM boundary.

**Option C — Fix constant from 12% to 20% (quick patch):**
Change hardcoded `0.12` to `0.20` to match T1 max — still inconsistent for T2/T3 but stops penalising T1 flow.

#### 3-Way Deliberation Questions
**SA:**
1. Option A removes a filter layer entirely. Is there a scenario where a contract passes the registry (within `atm_pct`) but is still genuinely too deep-OTM to be a credible WSJ signal? If yes, Option A throws away that distinction.
2. If Option B is chosen — does passing `tier` into the accumulator create unacceptable coupling between Layer 1 (registry) and Layer 5 (accumulator) in the 6-layer architecture?

**PBE:**
1. If Option A: audit every call site of `deep_otm_multiplier` and `_classify_otm()` — confirm nothing downstream relies on the DEEP_OTM classification for display or scoring.
2. If Option B: `tier` is available on `ContractMeta` from the registry lookup — confirm it is passed through `OptionsFlowEvent` to the `_DictEventWrapper` correctly.

**QA:**
1. Regardless of option: add regression test matrix covering T1 at 18% OTM, T2 at 14% OTM, T3 at 9% OTM — assert correct classification per chosen option.
2. Confirm existing `test_classify_otm` tests are updated, not deleted.

#### Acceptance Criteria
- [ ] OTM thresholds documented and consistent with registry tier definitions
- [ ] Option chosen and recorded in `docs/FIXES.md` under ING-005 with SA rationale
- [ ] T1 contract at 18% OTM no longer incorrectly penalised
- [ ] All 3 QA regression cases pass

---

### ING-006 — Directional Aggression Weighting on Premium Floor
**Type:** Feature / Gate Enhancement
**Priority:** P0
**Estimated Effort:** 1 day *(reduced from 1.5 — ING-001 resolution removes order_side extraction work)*
**Depends On:** ING-002 (premium floor active before aggression weighting is meaningful)
**Files:** `backend/parsers/bid_ask_classifier.py`, `backend/parsers/options_flow_parser.py`, `backend/signals/repetition_accumulator.py`

#### Context
The current `is_aggressive` flag is fill-placement only: `AT_ASK`/`ABOVE_ASK` = True, everything else = False. This misses the second most important aggressive pattern: **put selling at bid** (conviction bullish) and **call selling at bid** (conviction bearish). Both are directionally decisive but marked `is_aggressive=False` today.

**ING-001 Resolution Impact:** `order_side` is NOT available from Tradier. Aggression is determined entirely from `bid_ask_class + contract_type`. This is the correct and complete solution — no secondary data source needed.

Additionally, `is_aggressive` is currently not used in the accumulator's premium floor calculation at all — passive mid-prints accumulate premium identically to aggressive at-ask prints.

#### Implementation

**Step 1 — Replace `is_aggressive()` with `is_directionally_aggressive()` in `bid_ask_classifier.py`:**
```python
def is_directionally_aggressive(
    bid_ask_class: str,
    contract_type: str,
) -> bool:
    """
    Determines aggressive directional intent from fill placement + contract type.
    No order_side required — fill placement is the industry-standard proxy
    (Tradier timesale does not expose order_side; see docs/ORDER_SIDE_RESOLUTION.md).

    AT_ASK / ABOVE_ASK on any contract  → buyer paying up (aggressive open)
    AT_BID / BELOW_BID on PUT           → put writer selling at bid (conviction bullish)
    AT_BID / BELOW_BID on CALL          → call writer selling at bid (conviction bearish)
    MID on anything                     → passive / ambiguous → False
    """
    ba    = (bid_ask_class or "").strip().upper()
    ctype = (contract_type or "").strip().upper()

    if ba in ("AT_ASK", "ABOVE_ASK"):
        return True
    if ba in ("AT_BID", "BELOW_BID") and ctype in ("PUT", "CALL"):
        return True
    return False
```

**Step 2 — Update `parse_tradier_trade()` to use new function:**
```python
# OLD:
ev.is_aggressive = is_aggressive(ba_class)

# NEW:
ev.is_aggressive = is_directionally_aggressive(ba_class, contract_type)
```

**Step 3 — Add `is_aggressive` to `_DictEventWrapper.__slots__` in `repetition_accumulator.py`:**
```python
__slots__ = (..., "is_aggressive")
self.is_aggressive = d.get("is_aggressive", False)
```

**Step 4 — Add aggression-weighted premium check in `ingest_tick()`:**
```python
# In RepetitionAccumulator.__init__:
self.aggression_discount: float = aggression_discount  # default 0.5

# In ingest_tick(), replace raw premium floor check:
aggressive_premium = sum(
    getattr(e, "premium", 0.0)
    for e in ep.events
    if getattr(e, "is_aggressive", False)
)
passive_premium    = ep.total_premium - aggressive_premium
weighted_premium   = aggressive_premium + (passive_premium * self.aggression_discount)

if weighted_premium < effective_min_prem:
    return None
```

**Step 5 — Retain old `is_aggressive(trade_type)` function as deprecated (do not delete).**

#### 3-Way Deliberation Questions
**SA:**
1. `AT_BID`/`BELOW_BID` on both PUT and CALL now returns `True` for `is_directionally_aggressive`. This means a mid-market maker filling a CALL at bid (routine hedging) would be classified as aggressive. Is the contract_type distinction sufficient, or do we need a size threshold (e.g., only flag AT_BID as aggressive if `size >= N`) to avoid marking routine small fills?
2. Should `is_aggressive` be persisted as a column in `flow_events` for backtesting and multi-day repeat analysis (ING-007 will need this for pattern quality scoring)?

**PBE:**
1. The `_DictEventWrapper` dict is constructed from `OptionsFlowEvent.__dict__` — confirm `is_aggressive` is included in that dict serialisation path and not filtered out anywhere.
2. `aggression_discount=0.5` is a magic number. Should it be configurable via `ingestion_config` from day one, or hardcoded initially and promoted to config after observing signal volume impact?

**QA:**
1. Test matrix required:
   - `AT_ASK + CALL` → True
   - `AT_ASK + PUT` → True
   - `AT_BID + PUT` → True (put writer)
   - `AT_BID + CALL` → True (call writer)
   - `MID + CALL` → False
   - `MID + PUT` → False
   - `BELOW_BID + PUT` → True
   - `ABOVE_ASK + CALL` → True
2. Accumulator test: episode with 2 events — $80k aggressive + $40k passive → weighted = $80k + $20k = $100k. Assert passes $100k floor, fails $110k floor.

#### Acceptance Criteria
- [ ] `is_directionally_aggressive(bid_ask_class, contract_type)` replaces `is_aggressive(trade_type)` in parser
- [ ] All 8 QA test matrix cases pass
- [ ] Accumulator uses aggression-weighted premium for Gate 6 floor check
- [ ] `aggression_discount` parameter on `RepetitionAccumulator` with default 0.5
- [ ] Old `is_aggressive()` retained as deprecated with docstring noting replacement
- [ ] `is_aggressive` field available in `_DictEventWrapper`

---

### ING-007 — Multi-Day Repeat Window Lookback
**Type:** Feature / New Gate
**Priority:** P1 — core WSJ repeat detection
**Estimated Effort:** 3 days
**Depends On:** ING-002 (noise floor in place), ING-003 (DTE tiers active)
**Files:** `backend/services/flow_store.py`, `backend/services/tradier_stream.py`, `backend/utils/contract_day_cache.py` (new), Supabase migration

#### Context
The 30-minute rolling window accumulator cannot detect WSJ-style repeat patterns, which span multiple days — same contract printing across 2–5 trading days. Two events on the same AAPL $200C 2026-06-20 on Monday and Wednesday are treated as completely independent episodes. This is the most critical structural gap relative to WSJ methodology.

#### ⚠️ Infrastructure Prerequisite — Supabase Migration FIRST
```sql
-- Migration name: add_flow_events_contract_day_index
CREATE INDEX idx_flow_events_contract_day
ON flow_events (ticker, contract_type, strike, expiry, created_at DESC);

-- Add order_side column (decision from ING-006 SA deliberation Q2):
ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS order_side TEXT DEFAULT 'UNKNOWN';

-- Add is_aggressive column for backtesting:
ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS is_aggressive BOOLEAN DEFAULT FALSE;
```
Apply migration, then run `EXPLAIN ANALYZE` on the lookback query to confirm index usage before writing any Python.

#### Implementation

**Step 1 — New `utils/contract_day_cache.py`:**
```python
from typing import NamedTuple
from cachetools import TTLCache
import asyncio

class PriorVolumeResult(NamedTuple):
    prior_premium:     float
    prior_trade_count: int
    prior_days_active: int  # distinct calendar days with ≥1 qualifying print

# TTL=300s (5 min refresh), maxsize=2000 contracts
_cache: TTLCache = TTLCache(maxsize=2000, ttl=300)
_cache_lock = asyncio.Lock()
```

**Step 2 — `get_prior_contract_volume()` in `flow_store.py`:**
```python
async def get_prior_contract_volume(
    ticker: str, contract_type: str, strike: float, expiry: str,
    lookback_days: int = 5,
) -> PriorVolumeResult:
    """
    Query flow_events for cumulative premium + trade_count + distinct active days
    on this exact contract over the last N calendar days (excluding today).
    Uses idx_flow_events_contract_day. Returns (0, 0, 0) on any error.
    """
```

**Step 3 — Background queue worker, NOT hot-path inline call:**
- After `persist_flow_event()` succeeds in `_process_trade()`, enqueue OCC symbol into `asyncio.Queue`
- Background coroutine drains queue → calls `get_prior_contract_volume()` → updates `_contract_repeat_cache`
- `ingest_tick()` reads from cache via non-blocking dict lookup

**Step 4 — Enrichment flag in accumulator (NOT a hard drop gate — start soft):**
```python
if self.multi_day_check_enabled:
    prior = self._contract_repeat_cache.get(ep_key)
    ep.is_multi_day_repeat = (
        prior is not None and prior.prior_days_active >= self.min_repeat_days
    )
    # Hard gate optional — default disabled:
    if self.require_multi_day and not ep.is_multi_day_repeat:
        _stats["multi_day_not_met"] += 1
        return None
```

#### 3-Way Deliberation Questions
**SA:**
1. Hard gate vs. soft enrichment flag: recommend starting as enrichment flag (`require_multi_day=False`) to preserve the full episode record while we observe how many episodes qualify as multi-day repeats. Flip to hard gate only after 2 weeks of production data confirms the flag fires on expected patterns. SA sign-off required before flipping.
2. Lookback window: 5 calendar days vs 5 trading days — market closed on weekends means a Friday–Monday print is 3 calendar days but 2 trading days apart. Should lookback use calendar days (simpler) or trading days (more accurate for WSJ's pattern)?
3. Should `prior_days_active` count calendar days or days-with-qualifying-flow (i.e., days where premium ≥ floor)? Including noise days could inflate the repeat count.

**PBE:**
1. `cachetools.TTLCache` — confirm it is an existing dependency in `requirements.txt`. If not, evaluate stdlib alternative (`dict` + manual TTL timestamp check).
2. Background queue pattern introduces eventual consistency — a tick arriving 200ms after a DB write may not see the prior volume result yet. This is acceptable for an enrichment flag but not for a hard gate. Confirm approach with SA before wiring hard gate.
3. `asyncio.Queue` in `_process_trade()` — confirm no backpressure risk if DB is slow (queue grows unbounded). Add `maxsize=5000` and drop oldest on overflow with a `_stats["repeat_cache_queue_overflow"]` counter.

**QA:**
1. Integration test requires seeded `flow_events` fixture with rows on 3 prior days for the same contract. Assert `prior_days_active=3` returned.
2. Test cache TTL expiry — after 5 minutes, cache should re-fetch from DB, not serve stale data.
3. Performance benchmark: `_process_trade()` latency before vs. after background queue addition. Target: no measurable delta on hot path.

#### Acceptance Criteria
- [ ] Supabase migration applied; `EXPLAIN ANALYZE` confirms index hit
- [ ] `get_prior_contract_volume()` returns correct results for seeded fixture
- [ ] Background worker populates cache; hot path is non-blocking
- [ ] `ep.is_multi_day_repeat` flag set correctly
- [ ] `_stats["multi_day_repeat_count"]` and `_stats["multi_day_not_met"]` counters in `/health/stream`
- [ ] No measurable latency increase on `_process_trade()` (benchmark required)
- [ ] All QA test cases pass

---

### ING-008 — Volume vs. OI Gate via Registry Injection
**Type:** Feature / New Gate
**Priority:** P1
**Estimated Effort:** 2 days
**Depends On:** ING-004 (registry integration pattern), ING-005 (OTM classification stable)
**Files:** `backend/signals/repetition_accumulator.py`, `backend/parsers/options_flow_parser.py`

#### Context
WSJ uses **volume > open interest** to confirm options flow is new directional positioning, not existing position management. `open_interest` from Tradier timesale is almost always 0. The correct source is `ContractMeta.open_interest` from the registry, fetched from the full options chain at build time.

#### 🔴 Tradier Chain API Data Verification Required
**Before any implementation:** Run a one-off chain fetch on a liquid ticker (AAPL, SPY) via `get_option_chain_bulk()` and inspect `open_interest` values:
1. Are near-term liquid strikes non-zero?
2. Is OI end-of-prior-day (expected) or intraday-updated?
3. Are T3 tickers (smaller cap) reliably populated or frequently zero?

Document findings in `docs/FIXES.md` under ING-008 before writing any gate logic.

#### Implementation

**Step 1 — Source OI from registry in `parse_tradier_trade()`:**
```python
# After registry lookup:
if meta and ev.open_interest == 0:
    ev.open_interest = meta.open_interest
```

**Step 2 — Add `open_interest` to `_DictEventWrapper.__slots__`:**
```python
__slots__ = (..., "open_interest")
self.open_interest = d.get("open_interest", 0)
```

**Step 3 — Vol/OI gate in `ingest_tick()` (disabled by default):**
```python
if self.vol_oi_check_enabled:
    total_size = sum(getattr(e, "size", 0) for e in ep.events)
    latest_oi  = getattr(ep.events[-1], "open_interest", 0)
    if latest_oi > 0 and total_size < latest_oi * self.vol_oi_min_ratio:
        _stats["vol_oi_suppressed"] += 1
        return None
```

Defaults: `vol_oi_check_enabled=False`, `vol_oi_min_ratio=1.0`. Enable via `ingestion_config` after 1-week signal rate observation.

#### 3-Way Deliberation Questions
**SA:**
1. OI from chain build is end-of-prior-day. Intraday episode volume is real-time. The ratio is directionally correct but not same-snapshot precise. Is prior-day OI sufficient for WSJ's methodology, or does this create too many false positives early in the trading day (when intraday vol is naturally low relative to OI)?
2. For T3 tickers where OI is frequently 0 or unreliable from the chain fetch — should the gate auto-skip (current design) or should T3 tickers have a lower `vol_oi_min_ratio` (e.g., 0.5)?

**PBE:**
1. `vol_oi_check_enabled` toggled via `ingestion_config` without code deploy — confirm `ingestion_config` has a mechanism for live boolean feature flags that the accumulator can poll without restart.
2. `vol_oi_min_ratio` as a configurable float: should this be per-tier (T1: 1.0, T2: 0.75, T3: 0.5) rather than a single global value?

**QA:**
1. Critical edge case: OI=0 → gate MUST skip silently (not drop the episode). Add explicit test.
2. Test: OI=500, total_size=499, ratio=1.0 → dropped. OI=500, total_size=500, ratio=1.0 → passes.
3. Confirm gate is observably disabled when `vol_oi_check_enabled=False` — `vol_oi_suppressed` counter stays 0.

#### Acceptance Criteria
- [ ] `open_interest` sourced from `ContractMeta` when tick OI = 0
- [ ] `_DictEventWrapper` includes `open_interest`
- [ ] Gate implemented but disabled by default; toggled via `ingestion_config`
- [ ] `_stats["vol_oi_suppressed"]` in `/health/stream`
- [ ] Tradier chain API OI quality documented in `docs/FIXES.md` under ING-008
- [ ] All 3 QA edge case tests pass

---

## Sprint Exit Criteria
All 7 stories pass acceptance criteria AND:
- [ ] No regression in existing passing tests (`pytest backend/`)
- [ ] `/health/stream` exposes all new counters: `below_min_premium`, `multi_day_repeat_count`, `multi_day_not_met`, `vol_oi_suppressed`
- [ ] 3-way deliberation sign-off documented for every story in `docs/FIXES.md`
- [ ] `docs/ARCHITECTURE.md` updated to reflect new gate structure
- [ ] `docs/CHANGELOG.md` updated with sprint summary
- [ ] `docs/ORDER_SIDE_RESOLUTION.md` referenced from `docs/ARCHITECTURE.md`

---

*Sprint created: 2026-05-03 | Last updated: 2026-05-03 (ING-002 deliberation complete; ING-002-CONFIG follow-up scoped) | Owner: Dhruv Patel | Classification: P0 — WSJ Ingestion Alignment*
