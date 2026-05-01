# Cipher Apex Signal Pipeline — Engineering Spec

## Document Role
This is the engineering-spec version of the Apex story and sprint plan. It is intended to be pushed into the repo as an implementation-grade planning document, with concrete code examples, explicit acceptance criteria, and test expectations for each story.

## Non-Negotiable Delivery Rules
- New files must have 100% line and branch coverage.
- Every changed branch in an existing file must be covered by tests.
- The full regression suite must pass before merge.
- Direction invariants are CI gate tests and cannot regress.
- No swarm code may exist outside the new Apex-scoped implementation.
- Fake backtest values must not influence production composite scoring.

## Execution Order
1. S0 — Swarm cleanup.
2. S1 — Alert threshold reconciliation and emit-cache flush.
3. S2 — Parser and detector layer fixes.
4. S2.5 — DB migration for direction fields.
5. S3 — Apex L1 signal gate.
6. S4 — Apex L2 dual-window accumulator.
7. S5 — Apex L4 ladder detection.
8. S6 — Apex L3 composite overhaul and hot-path corrections.
9. S7 — Tiered swarm and circuit breaker, only after stream worker review.
10. S8 — Real backtest score, future sprint.

---

## S0 — Swarm Cleanup
**Type:** prerequisite cleanup  
**Status:** must land first

### Objective
Remove dead ensemble/swarm infrastructure from the current ingestion/composite layer so the codebase has one deterministic composite path before the new Apex-only swarm is introduced later.

### Files
- `backend/signals/composite_signal_engine.py`
- `backend/simulation/ensemble_runner.py`
- Any tests mocking the old async swarm path

### Concrete Changes
Remove all old async swarm hooks from `composite_signal_engine.py`.

#### Before
```python
from simulation.ensemble_runner import run_ensemble as _original_run_ensemble

run_ensemble = _original_run_ensemble

async def build_composite_async(ep, accumulator):
    result = await run_ensemble(ep, accumulator)
    ...
```

#### After
```python
# build_composite() remains the only active path.
# No run_ensemble import.
# No async composite path.
```

### Implementation Notes
- Do not delete `simulation/ensemble_runner.py` until grep confirms no tests or utilities still import it.
- First mark it deprecated with a top-level comment if references still exist.
- Remove patch-compatibility comments and alias wiring.

### Acceptance Criteria
- `build_composite_async()` no longer exists.
- No `run_ensemble` import remains in `composite_signal_engine.py`.
- Tests pass without import failures.

### Tests
- `tests/test_composite_signal_engine.py`
- Grep/migration validation for old mock targets

---

## S1 — Alert Level Threshold Reconciliation + Emit Cache Flush
**Type:** bug fix  
**Depends on:** S0

### Objective
Bring alert-level thresholds into line with the approved Apex bands and prevent stale debounce state from causing bad re-emits or false de-escalations after deploy.

### Files
- `backend/signals/repetition_accumulator.py`
- Stream startup path where `_signal_last_emit` can be reset

### Concrete Changes

#### Threshold example
```python
def get_alert_level(self, ep: RepetitionEpisode) -> str:
    prem = ep.total_premium
    if prem >= 2_000_000:
        return "CONVICTION"
    if prem >= 500_000:
        return "STRONG_SIGNAL"
    if prem >= 100_000:
        return "ALERT"
    return "WATCH"
```

#### Cache flush example
```python
# on startup / stream boot boundary
_signal_last_emit.clear()
```

### Acceptance Criteria
- Thresholds match the approved architecture bands.
- Startup clears stale signal emit state.
- Historical rows are not backfilled.

### Tests
```python
def test_alert_level_boundaries():
    ...

def test_signal_emit_cache_flushed_on_startup():
    ...
```

---

## S2 — Parser + Detector Layer Fixes
**Type:** feature + bug fix  
**Depends on:** S0

### Objective
Replace naive contract-type sentiment with intelligent execution-aware direction inference, preserve SELL PUT = BULLISH end to end, and improve whale/shark classification fidelity.

### New File
#### `backend/parsers/order_side_classifier.py`
```python
from typing import NamedTuple

_BUY_CLASSES = frozenset({"ABOVE_ASK", "AT_ASK"})
_SELL_CLASSES = frozenset({"AT_BID", "BELOW_BID"})

class OrderDirection(NamedTuple):
    order_side: str
    sentiment: str
    strong_sentiment: bool
    execution_mechanic: str   # NEW — additive; does not change direction semantics


_MECHANIC_MAP = {
    ("BUY",     "CALL"): "DIRECTIONAL_LONG",
    ("BUY",     "PUT"):  "DIRECTIONAL_SHORT",
    ("SELL",    "PUT"):  "PASSIVE_BULLISH",
    ("SELL",    "CALL"): "PASSIVE_BEARISH",
    ("UNKNOWN", "CALL"): "AMBIGUOUS_LONG",
    ("UNKNOWN", "PUT"):  "AMBIGUOUS_SHORT",
}


def classify_order_direction(
    bid_ask_class: str,
    contract_type: str,
    is_synthetic: bool,
) -> OrderDirection:
    if is_synthetic:
        fallback_sentiment = "BULLISH" if contract_type == "CALL" else "BEARISH"
        mechanic = _MECHANIC_MAP.get(("UNKNOWN", contract_type), "AMBIGUOUS_LONG")
        return OrderDirection("UNKNOWN", fallback_sentiment, False, mechanic)

    if bid_ask_class in _BUY_CLASSES:
        sentiment = "BULLISH" if contract_type == "CALL" else "BEARISH"
        mechanic = _MECHANIC_MAP[("BUY", contract_type)]
        return OrderDirection("BUY", sentiment, True, mechanic)

    if bid_ask_class in _SELL_CLASSES:
        sentiment = "BEARISH" if contract_type == "CALL" else "BULLISH"
        mechanic = _MECHANIC_MAP[("SELL", contract_type)]
        return OrderDirection("SELL", sentiment, True, mechanic)

    fallback_sentiment = "BULLISH" if contract_type == "CALL" else "BEARISH"
    mechanic = _MECHANIC_MAP.get(("UNKNOWN", contract_type), "AMBIGUOUS_LONG")
    return OrderDirection("UNKNOWN", fallback_sentiment, False, mechanic)


def order_side_to_direction(order_side: str, contract_type: str) -> str:
    if order_side == "BUY":
        return "REPEAT_BUY" if contract_type == "CALL" else "REPEAT_SELL"
    if order_side == "SELL":
        return "REPEAT_BUY" if contract_type == "PUT" else "REPEAT_SELL"
    return "REPEAT_BUY" if contract_type == "CALL" else "REPEAT_SELL"
```

**Mechanic taxonomy (complete, non-overlapping):**

| order_side | contract_type | execution_mechanic  | Market interpretation                             |
|------------|---------------|---------------------|---------------------------------------------------|
| BUY        | CALL          | DIRECTIONAL_LONG    | Long call; net premium outlay; urgency/conviction |
| BUY        | PUT           | DIRECTIONAL_SHORT   | Long put hedge or short bet; premium outlay       |
| SELL       | PUT           | PASSIVE_BULLISH     | Put selling; short vega; income or floor view     |
| SELL       | CALL          | PASSIVE_BEARISH     | Call selling; short gamma; income or hedge        |
| UNKNOWN    | CALL          | AMBIGUOUS_LONG      | Mid-print or synthetic call — intent unclear      |
| UNKNOWN    | PUT           | AMBIGUOUS_SHORT     | Mid-print or synthetic put — intent unclear       |

### Dataclass Changes
#### `backend/parsers/options_flow_parser.py`
Add fields to `OptionsFlowEvent`:
```python
order_side: str = "UNKNOWN"
strong_sentiment: bool = False
daily_volume: int = 0
execution_mechanic: str = "AMBIGUOUS_LONG"   # persists alongside order_side
```

### Bid/Ask Classifier Changes
#### `backend/parsers/bid_ask_classifier.py`
```python
def is_sell_aggressive(trade_type: str) -> bool:
    return trade_type in ("AT_BID", "BELOW_BID")
```

### Trade Type Detector Changes
#### `backend/parsers/trade_type_detector.py`
```python
def detect_trade_type(size, premium, exchange_cnt, fill_count):
    if exchange_cnt >= 3 and fill_count >= 3:
        return "SWEEP"
    if (size >= 500 and fill_count == 1) or (premium >= 500_000 and exchange_cnt <= 2):
        return "BLOCK"
    if fill_count >= 5 and size >= 100:
        return "SPLIT"
    return "SINGLE"


def is_golden_sweep(trade_type, premium, is_directionally_aggressive):
    if trade_type == "SWEEP" and is_directionally_aggressive and premium >= 500_000:
        return True
    if trade_type == "BLOCK" and premium >= 1_000_000:
        return True
    return False
```

### Parser Changes
#### First-pass direction inference
```python
ba_class = classify_bid_ask(fill, effective_bid, effective_ask)
aggressive = is_aggressive(ba_class)
is_sell_agr = is_sell_aggressive(ba_class)
is_directionally_aggressive = aggressive or is_sell_agr

golden = is_golden_sweep(ttype, premium, is_directionally_aggressive)
direction = classify_order_direction(ba_class, ctype, is_synthetic_quote)
```

#### Event creation
```python
ev = OptionsFlowEvent(
    ...
    bid_ask_class=ba_class,
    is_aggressive=aggressive,
    order_side=direction.order_side,
    sentiment=direction.sentiment,
    strong_sentiment=direction.strong_sentiment,
    execution_mechanic=direction.execution_mechanic,
    ...
)
```

#### Conviction scoring fix
```python
raw_conviction = round(
    min(
        (0.4 if is_directionally_aggressive else 0.15)
        + (0.25 if golden else 0.0)
        + min(premium / 10_000_000, 0.25)
        + dte_urgency,
        1.0,
    ),
    3,
)
```

#### Registry enrichment fix
```python
if reg and reg.is_ready():
    meta = reg.lookup(symbol)
    if meta:
        ev.ticker = meta.ticker
        ev.strike = meta.strike
        ev.expiry = meta.expiry
        ev.contract_type = meta.contract_type
        ev.dte = meta.dte
        ev.open_interest = meta.open_interest

        direction = classify_order_direction(
            ev.bid_ask_class,
            ev.contract_type,
            ev.is_synthetic_quote,
        )
        ev.order_side          = direction.order_side
        ev.sentiment           = direction.sentiment
        ev.strong_sentiment    = direction.strong_sentiment
        ev.execution_mechanic  = direction.execution_mechanic   # re-derived, not overwritten

    if ev.underlying_price == 0.0:
        up = reg.stock_price(ev.ticker)
        if up > 0:
            ev.underlying_price = up

    if ev.daily_volume == 0:
        ev.daily_volume = reg.get_daily_volume(ev.ticker)
```

### Symbol Registry Changes
#### `backend/services/symbol_registry.py`
```python
self._raw_quotes: dict[str, dict] = {}
```

In `build()`:
```python
self._raw_quotes = raw_quotes
```

Add accessor:
```python
def get_daily_volume(self, ticker: str) -> int:
    try:
        return int(self._raw_quotes.get(ticker, {}).get("volume", 0) or 0)
    except (TypeError, ValueError):
        return 0
```

### Repetition Episode Direction Fix
#### `backend/signals/repetition_accumulator.py`
```python
@property
def dominant_direction(self) -> str:
    buy_prem = 0.0
    sell_prem = 0.0
    for e in self.events:
        direction = order_side_to_direction(
            getattr(e, "order_side", "UNKNOWN"),
            getattr(e, "contract_type", "CALL"),
        )
        if direction == "REPEAT_BUY":
            buy_prem += getattr(e, "premium", 0.0)
        else:
            sell_prem += getattr(e, "premium", 0.0)
    return "REPEAT_BUY" if buy_prem >= sell_prem else "REPEAT_SELL"
```

### Acceptance Criteria
- SELL PUT resolves to bullish sentiment when quote placement indicates initiated selling.
- Registry enrichment never naively overwrites sentiment.
- BLOCK detection catches high-premium low-exchange whales.
- Golden BLOCK is supported.
- `underlying_price` and `daily_volume` enrich correctly.
- `execution_mechanic` is derived, persisted, and re-derived on registry enrichment.

### Required Invariant Tests

> **Deliberation note (Architect + Principal Engineer, April 30 2026):**
> The original CI gate only covered SELL-side invariants. The principal engineer raised that
> BUY CALL = BULLISH and BUY PUT = BEARISH are equally regressionable — a parser refactor
> breaking BUY PUT direction would not be caught. The architect agreed: both axes must be
> gated. All four quadrants are now required CI invariants (Issue 8 resolution).

```python
# ── SELL-side invariants (original) ──────────────────────────────────────────
def test_sell_put_is_bullish_sentiment():
    result = classify_order_direction("AT_BID", "PUT", False)
    assert result.order_side == "SELL"
    assert result.sentiment == "BULLISH"
    assert result.strong_sentiment is True

def test_sell_call_is_bearish_sentiment():
    result = classify_order_direction("AT_BID", "CALL", False)
    assert result.order_side == "SELL"
    assert result.sentiment == "BEARISH"
    assert result.strong_sentiment is True

def test_sell_put_maps_to_repeat_buy():
    assert order_side_to_direction("SELL", "PUT") == "REPEAT_BUY"

def test_sell_call_maps_to_repeat_sell():
    assert order_side_to_direction("SELL", "CALL") == "REPEAT_SELL"

# ── BUY-side invariants (added — Issue 8) ────────────────────────────────────
def test_buy_call_is_bullish_sentiment():
    result = classify_order_direction("AT_ASK", "CALL", False)
    assert result.order_side == "BUY"
    assert result.sentiment == "BULLISH"
    assert result.strong_sentiment is True

def test_buy_put_is_bearish_sentiment():
    result = classify_order_direction("AT_ASK", "PUT", False)
    assert result.order_side == "BUY"
    assert result.sentiment == "BEARISH"
    assert result.strong_sentiment is True

def test_buy_call_maps_to_repeat_buy():
    assert order_side_to_direction("BUY", "CALL") == "REPEAT_BUY"

def test_buy_put_maps_to_repeat_sell():
    assert order_side_to_direction("BUY", "PUT") == "REPEAT_SELL"
```

### Required Test Files
- `tests/test_order_side_classifier.py`
- `tests/test_direction_invariants.py`  ← must cover all 8 quadrant invariants above + 6 mechanic invariants (14 direction + 6 mechanic = 20 total assertions)
- `tests/test_bid_ask_classifier.py`
- `tests/test_trade_type_detector.py`
- `tests/test_options_flow_parser.py`
- `tests/test_repetition_accumulator.py`

---

## S2.5 — Supabase Migration: `order_side` + `strong_sentiment` + `execution_mechanic`
**Type:** DB migration  
**Depends on:** S2

### SQL
```sql
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS order_side TEXT
    CHECK (order_side IN ('BUY', 'SELL', 'UNKNOWN'))
    DEFAULT 'UNKNOWN',
  ADD COLUMN IF NOT EXISTS strong_sentiment BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS execution_mechanic TEXT
    CHECK (execution_mechanic IN (
      'DIRECTIONAL_LONG',
      'DIRECTIONAL_SHORT',
      'PASSIVE_BULLISH',
      'PASSIVE_BEARISH',
      'AMBIGUOUS_LONG',
      'AMBIGUOUS_SHORT'
    ))
    DEFAULT 'AMBIGUOUS_LONG';

CREATE INDEX IF NOT EXISTS idx_flow_events_order_side
  ON flow_events (order_side);

UPDATE flow_events
SET order_side = 'BUY',
    strong_sentiment = FALSE
WHERE order_side = 'UNKNOWN';
```

### Persistence Payload Example
```python
await persist_flow_event({
    ...
    "order_side":         ev.order_side,
    "strong_sentiment":   ev.strong_sentiment,
    "execution_mechanic": ev.execution_mechanic,   # NEW
})
```

### Acceptance Criteria
- New fields persist without schema errors.
- Existing readers remain compatible.
- `execution_mechanic` column is populated from first write; retroactive enrichment is not supported on a high-volume stream table.

> **QA Lead note:** The mechanic column must land in S2.5 alongside `order_side` and
> `strong_sentiment`. Retroactive enrichment of the column is not feasible on a
> high-volume stream table. Any future S8 backtest stratification by mechanic type
> requires the column to be populated from first write.

---

## S3 — Apex L1: `signal_gate.py`
**Type:** new module  
**Depends on:** S2

### Objective
Filter low-quality flow before it reaches accumulation.

### New File Example
#### `backend/signals/signal_gate.py`
```python
from typing import NamedTuple

class GateVerdict(NamedTuple):
    passed: bool
    reason: str


def passes_signal_gate(ev, tier: int) -> GateVerdict:
    spread_pct = ((ev.ask - ev.bid) / ev.ask) if ev.ask > 0 and ev.ask > ev.bid else 0.0
    if ev.ask > 0 and spread_pct > 0.50:
        return GateVerdict(False, "spread_too_wide")

    min_premium = {
        1: {"SWEEP": 50_000, "BLOCK": 100_000, "SPLIT": 150_000, "SINGLE": 250_000},
        2: {"SWEEP": 25_000, "BLOCK": 50_000,  "SPLIT": 100_000, "SINGLE": 150_000},
        3: {"SWEEP": 25_000, "BLOCK": 50_000,  "SPLIT": 100_000, "SINGLE": 150_000},
    }[tier]

    if ev.premium < min_premium.get(ev.trade_type, 999999999):
        return GateVerdict(False, "premium_below_floor")

    return GateVerdict(True, "passed")
```

### Acceptance Criteria
- Wide-spread junk is rejected (spread > 50% of ask, uniform across all tiers).
- Tier-specific premium floors are enforced.
- High-quality SELL PUT flow can pass.

### Tests
- `tests/test_signal_gate.py`
- 100% branch coverage

---

## S4 — Apex L2: Dual-Window Accumulator
**Type:** refactor + feature  
**Depends on:** S3

### Objective
Move from a simplistic threshold accumulator to a market-aware episode gate that handles LEAPS, ATM flow, deep OTM flow, and single massive sweeps correctly.

### ATM Band Definition

> **Deliberation note (Architect + Principal Engineer, April 30 2026):**
> The architect flagged that "ATM eligible" with no numeric boundary will be implemented
> inconsistently across engineers. The principal engineer proposed ±2% of underlying price
> as a practical ATM band — tight enough to exclude clear OTM, wide enough to capture
> ATM prints on high-underlying-price names like NVDA ($900+) where a ±$5 strike gap
> is less than 1%. The architect accepted ±2% as the working definition. This must be
> expressed as a fraction of underlying price, not an absolute dollar amount (Issue 6 resolution).

ATM is defined as:

```
abs(strike - underlying_price) / underlying_price <= 0.02
```

Contracts satisfying this condition are ATM-eligible and accumulate at standard premium floors (no OTM multiplier applied).

### Default Tier Table
```python
_DEFAULT_DTE_PREMIUM_TIERS = {
    7:    (50_000,    25_000),
    30:   (500_000,   100_000),
    90:   (1_000_000, 500_000),
    9999: (2_000_000, 1_000_000),
}
```

### Core Method Example
```python
def _get_episode_min_premium(self, ep):
    if not self.dte_premium_tiers:
        return self.min_premium
    latest_dte = getattr(ep.events[-1], "dte", 0) if ep.events else 0
    tier = self._tier_map.get(ep.ticker, 3)
    col = 0 if tier == 1 else 1
    for dte_max in sorted(self.dte_premium_tiers):
        if latest_dte <= dte_max:
            return self.dte_premium_tiers[dte_max][col]
    return self.min_premium
```

### Sweep Bypass — Semantics Clarification

> **Deliberation note (Architect + Principal Engineer, April 30 2026):**
> The original spec used `ep.trade_count == 1` without defining what `trade_count` counts.
> The architect raised that this is ambiguous between episode event count and fill count
> within a single stream tick. The principal engineer clarified: `trade_count` is the
> number of `OptionsFlowEvent` objects accumulated in the episode, not the fill_count
> field within a single tick. A single-event episode means exactly one qualifying event
> entered the accumulator for this (ticker, strike, expiry) key. This is the intended
> bypass condition (Issue 7 resolution).

```python
# trade_count = len(ep.events) — number of OptionsFlowEvents in this episode.
# NOT fill_count within a single stream tick.
is_single_whale = (
    self.sweep_bypass_premium > 0
    and len(ep.events) == 1                          # one episode event, not one fill
    and getattr(ep.events[-1], "trade_type", "") == "SWEEP"
    and ep.total_premium >= self.sweep_bypass_premium
)
```

### Deep OTM Example
```python
if self.deep_otm_multiplier > 1.0 and otm_pct > 0.12:
    deep_floor = effective_min_prem * self.deep_otm_multiplier
    if ep.total_premium < deep_floor:
        return None
```

### OTM Classification Logic
```python
otm_pct = abs(strike - underlying_price) / underlying_price if underlying_price > 0 else 0.0

if otm_pct <= 0.02:
    otm_band = "ATM"           # standard floor applies
elif otm_pct <= 0.12:
    otm_band = "STANDARD_OTM"  # standard floor applies
else:
    otm_band = "DEEP_OTM"      # 1.5x floor multiplier applies
```

### Signal Accumulator Example
```python
signal_accumulator = RepetitionAccumulator(
    window_minutes=10,
    min_trades=3,
    min_premium=100_000,
    min_sweeps=3,
    sweep_bypass_premium=500_000,
    otm_band=(0.00, 0.25),
    deep_otm_multiplier=1.5,
    dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
)
```

### Acceptance Criteria
- LEAPS are not automatically discarded.
- ATM is defined as `abs(strike - underlying_price) / underlying_price <= 0.02`; contracts in this band accumulate at standard floors.
- Deep OTM (> 12%) requires 1.5× premium floor.
- Single-event episodes (`len(ep.events) == 1`) of type SWEEP at >= $500K bypass `min_sweeps`.
- `underlying_price > 0` is required before OTM classification; events without underlying price fall back to standard floor.

---

## S5 — Apex L4: Cross-Contract Ladder Detection
**Type:** new module  
**Depends on:** S4

### Objective
Detect coordinated multi-strike positioning on the same ticker and expiry.

### Example Sketch
```python
class LadderSignal(NamedTuple):
    ticker: str
    expiry: str
    strikes: list[float]
    total_premium: float


def detect_ladder(active_eps):
    grouped = {}
    for ep in active_eps:
        key = (ep.ticker, ep.expiry)
        grouped.setdefault(key, []).append(ep)

    for (ticker, expiry), eps in grouped.items():
        strikes = sorted({ep.strike for ep in eps})
        if len(strikes) >= 3:
            return LadderSignal(
                ticker=ticker,
                expiry=expiry,
                strikes=strikes,
                total_premium=sum(ep.total_premium for ep in eps),
            )
    return None
```

### Acceptance Criteria
- Fires only on coordinated same-expiry structures.
- Ignores unrelated expiries.
- Expires stale ladder state.

---

## S6 — Apex L3: Composite Formula Overhaul + Hot Path Corrections
**Type:** refactor + bug fix  
**Depends on:** S2, S2.5, S4, S5

### Objective
Remove fake backtest influence, use episode-level semantics, and ensure hot-path publishing preserves the real direction of flow.

### Composite Helpers
```python
def episode_influence_tier(ep):
    prem = ep.total_premium
    if prem >= 2_000_000:
        return "WHALE"
    if prem >= 500_000:
        return "INSTITUTIONAL"
    if prem >= 100_000:
        return "LARGE"
    return "RETAIL"
```

### Composite Formula and Score Ceiling

> **Deliberation note (Architect + Principal Engineer, April 30 2026):**
> The architect identified that with `sector_score = 0.0` and `backtest_score = 0.0`,
> the weights only sum to 0.90, meaning composite_score is silently capped at 0.90
> until S5 ladder data is wired in. The principal engineer argued against redistributing
> the 0.10 to other weights mid-sprint because it would change the scoring baseline and
> break threshold calibration done against the 0.55/0.20/0.15 split. Decision: leave
> weights unchanged, document the ceiling explicitly, and expose it in the composite
> output payload so frontend consumers can normalize if needed (Issue 5 resolution).

```python
flow_s = round(flow_s_raw * (1.0 if latest.strong_sentiment else 0.80), 3)
bt_s = 0.0
vwp_f = volume_weighted_premium_factor(ep)
prem_t = premium_tier_score(ep)
sector_s = 0.0   # reserved — activates when S5 ladder context is wired

# NOTE: while sector_s == 0.0, maximum achievable composite_score is 0.90.
# This is intentional. Do not redistribute the 0.10 weight; it is reserved
# for ladder/context data. Frontend consumers should treat scores > 0.85 as
# effectively maximum conviction in the pre-S5 period.
comp = round(
    flow_s * 0.55
    + bt_s * 0.00
    + vwp_f * 0.20
    + prem_t * 0.15
    + sector_s * 0.10,
    3,
)
```

### Composite Bus Payload — Score Ceiling Field
```python
composite_msg = {
    "type": "composite_signal",
    "data": {
        "signal": {
            "ticker":                  composite.ticker,
            "recommendation":          composite.recommendation,
            "composite_score":         composite.composite_score,
            "composite_score_ceiling": 0.90,   # explicit — remove when sector_score activates
            "flow_score":              composite.flow_score,
            "backtest_score":          composite.backtest_score,
            "volume_premium_factor":   composite.volume_premium_factor,
            "reasoning":               composite.reasoning,
            "alert_level":             alert_level,
            "order_side":              ev.order_side,
            "strong_sentiment":        ev.strong_sentiment,
            "execution_mechanic":      ev.execution_mechanic,   # NEW — additive, non-breaking
        },
        "episode": {
            "contract_type":   sig_ep.contract_type,
            "direction":       direction,
            "influence_tier":  episode_influence_tier(sig_ep),
            "total_premium":   sig_ep.total_premium,
            "trade_count":     sig_ep.trade_count,
            "is_accelerating": sig_ep.is_accelerating,
            "timestamp":       ev.timestamp.isoformat(),
        },
    },
}
```

> Existing downstream consumers can ignore `execution_mechanic` safely. It is additive.

### Hot Path Direction Fix
#### `backend/services/tradier_stream.py`
```python
# before
if sig_ep.contract_type == "CALL":
    direction = "REPEAT_BUY"
elif sig_ep.contract_type == "PUT":
    direction = "REPEAT_SELL"
else:
    direction = "REPEAT_BUY" if ev.sentiment == "BULLISH" else "REPEAT_SELL"

# after
direction = sig_ep.dominant_direction
```

### Demo-Mode Direction Fix
```python
order_side_demo = rng.choices(["BUY", "SELL", "UNKNOWN"], weights=[60, 25, 15])[0]
direction = order_side_to_direction(order_side_demo, ctype)
```

### Acceptance Criteria
- Production composite score ignores fake backtest output.
- SELL PUT campaigns persist and publish as bullish direction.
- Episode influence tier uses episode premium.
- Order-side metadata and `execution_mechanic` are available to downstream consumers.
- `composite_score_ceiling` field is present in bus payload and set to `0.90` until sector_score activates.
- When S5 ladder context is wired, `sector_score` receives a real value and `composite_score_ceiling` is removed from the payload.

---

## S7 — Tiered Swarm + Circuit Breaker
**Type:** new feature  
**Depends on:** S6  
**Status:** blocked pending stream worker review

### Objective
Add the only allowed swarm path, and only at the Apex layer.

### Example Shape
```python
async def build_apex_swarm_composite(ep, accumulator):
    return await asyncio.wait_for(_swarm_impl(ep, accumulator), timeout=2.0)
```

### Circuit Breaker Example
```python
if failures >= 3:
    breaker_open_until = now + 300
    return build_composite(ep, accumulator)
```

### Hard Rule
Do not begin implementation until `stream_worker.py` confirms the runtime model is safe for async model calls.

---

## S8 — Real Backtest Score from `flow_events`
**Type:** future feature  
**Depends on:** S6

### Objective
Replace seeded fake backtest scoring with real historical win-rate data.

### Example Direction
```python
def get_real_backtest_score(ticker, contract_type, dte_bucket):
    # query aggregated signal outcomes from flow_events-derived analytics table
    ...
```

### Hard Rule
Until this lands, production composite scoring must keep `backtest_score` at zero weight.

---

## Directional Invariants — CI Gate Section

> **Updated April 30 2026 (Issue 8 resolution):** All four direction quadrants are now
> required CI invariants. The original list covered only SELL side. BUY CALL and BUY PUT
> are equally regressionable and must be gated.
>
> **Updated May 1 2026 (Issue 9 resolution):** 6 mechanic assertions added. Total CI gate
> assertion count is now 20 (14 direction + 6 mechanic).

These invariants must exist in a dedicated test file and run as a hard gate.

```python
def test_direction_invariants():
    # SELL-side invariants (original)
    assert classify_order_direction("AT_BID", "PUT", False).sentiment == "BULLISH"
    assert classify_order_direction("BELOW_BID", "PUT", False).sentiment == "BULLISH"
    assert classify_order_direction("AT_BID", "PUT", False).order_side == "SELL"
    assert classify_order_direction("AT_BID", "PUT", False).strong_sentiment is True
    assert order_side_to_direction("SELL", "PUT") == "REPEAT_BUY"
    assert order_side_to_direction("SELL", "CALL") == "REPEAT_SELL"

    # BUY-side invariants (added — Issue 8)
    assert classify_order_direction("AT_ASK", "CALL", False).sentiment == "BULLISH"
    assert classify_order_direction("AT_ASK", "CALL", False).order_side == "BUY"
    assert classify_order_direction("AT_ASK", "CALL", False).strong_sentiment is True
    assert classify_order_direction("AT_ASK", "PUT", False).sentiment == "BEARISH"
    assert classify_order_direction("AT_ASK", "PUT", False).order_side == "BUY"
    assert classify_order_direction("AT_ASK", "PUT", False).strong_sentiment is True
    assert order_side_to_direction("BUY", "CALL") == "REPEAT_BUY"
    assert order_side_to_direction("BUY", "PUT") == "REPEAT_SELL"


# ── Execution Mechanic Invariants (added — Issue 9) ─────────────────────────
def test_mechanic_invariants():
    c = classify_order_direction

    assert c("AT_ASK",    "CALL", False).execution_mechanic == "DIRECTIONAL_LONG"
    assert c("ABOVE_ASK", "CALL", False).execution_mechanic == "DIRECTIONAL_LONG"
    assert c("AT_ASK",    "PUT",  False).execution_mechanic == "DIRECTIONAL_SHORT"
    assert c("ABOVE_ASK", "PUT",  False).execution_mechanic == "DIRECTIONAL_SHORT"
    assert c("AT_BID",    "PUT",  False).execution_mechanic == "PASSIVE_BULLISH"
    assert c("BELOW_BID", "PUT",  False).execution_mechanic == "PASSIVE_BULLISH"
    assert c("AT_BID",    "CALL", False).execution_mechanic == "PASSIVE_BEARISH"
    assert c("BELOW_BID", "CALL", False).execution_mechanic == "PASSIVE_BEARISH"
    assert c("MID",       "CALL", False).execution_mechanic == "AMBIGUOUS_LONG"
    assert c("MID",       "PUT",  False).execution_mechanic == "AMBIGUOUS_SHORT"
    assert c("AT_ASK",    "CALL", True).execution_mechanic  == "AMBIGUOUS_LONG"
    assert c("AT_ASK",    "PUT",  True).execution_mechanic  == "AMBIGUOUS_SHORT"
```

---

## Issue 9 — Story Impact Summary

> **Deliberation note (Architect + Principal Engineer + Lead QA, May 1 2026):**
> BUY CALL and SELL PUT both resolve to REPEAT_BUY (delta-positive), but their execution
> mechanics are structurally different. BUY CALL is an aggressive directional bet (long
> premium, urgency signal). SELL PUT is passive positioning (short vega, income or floor
> view). Collapsing them into REPEAT_BUY discards the mechanic dimension. The panel agreed
> not to alter direction semantics — all downstream consumers stay unchanged — but to add
> `execution_mechanic` as a new additive metadata field on `OrderDirection`, `OptionsFlowEvent`,
> the persistence payload, and the composite bus payload. Direction, dominant_direction, and
> all thresholds are unchanged. (Issue 9 resolution)

| Story | Change type        | What changes                                                      |
|-------|--------------------|-------------------------------------------------------------------|
| S2    | Sub-task addition  | `OrderDirection` gets 4th field; `classify_order_direction()` updated; `OptionsFlowEvent` gets `execution_mechanic` field |
| S2.5  | Migration addition | `execution_mechanic` column added to S2.5 SQL and persist payload |
| S6    | Payload addition   | `execution_mechanic` added to composite bus signal block          |
| S2    | New CI invariants  | 6 mechanic assertions added to `test_direction_invariants.py`     |

**No new story sprint slot required.** No direction semantics changed. No downstream
consumers break. `dominant_direction` on `RepetitionEpisode` is unchanged — it aggregates
premium-weighted direction only, not mechanics. The mechanic field is per-event only.

---

## Suggested Sprint Packaging

### Sprint 1
- S0
- S1
- S2
- S2.5

### Sprint 2
- S3
- S4
- S5

### Sprint 3
- S6
- S7 if stream-worker review clears async safety

### Future Sprint
- S8

---

## Release Notes Guidance
- S2 and S2.5 should release together because persistence schema must exist before new hot-path writes.
- S6 should not start until S4 and S5 semantics are stable.
- S7 remains blocked until concurrency review is complete.
- S8 is intentionally separated so fake backtest influence does not silently creep back into production.
- S6 introduces `composite_score_ceiling = 0.90` in the bus payload. This field must be removed from the payload when S5 ladder data is wired into `sector_score`.
- `execution_mechanic` is additive across S2, S2.5, and S6. Downstream consumers that do not read it are unaffected.
