# Cipher — APEX Sprint Execution Tracker

> ## ⚠️ How to read this file
>
> **Story definitions live in:** [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md)
> That file is the canonical spec — acceptance criteria, scope, test requirements, architectural
> deliberation notes. Read it before starting any story.
>
> **This file tracks:** execution state — what is merged, what is open, what is blocked, exact
> build order, gate status, and dynamically added post-merge stories from panel reviews.
>
> **Rule:** Before every PR merge — Senior Architect + Principal Backend Engineer + Lead QA
> deliberate on the diff. Small fixes go inline on the PR. Anything needing a separate PR gets a
> numbered story added here AND filed as a GitHub issue. No story lives only in conversation.
> **Work must never be pushed directly to `main`. Always branch + PR.**
>
> **When answering "What is next?" or "What is remaining?"** — read this file AND
> `docs/cipher_apex_story_and_sprint_plan.md` together. Issues alone are insufficient.

---

## Legend

| Symbol | Meaning |
|---|---|
| ✅ | Merged to `main` |
| 🔴 | Hard gate — next story cannot start until this is closed |
| 🟡 | Must close before current phase merges |
| 🟢 | Queued — no current blocker |
| ⏳ | Blocked — waiting on gates above |
| ⚪ | Low priority / quality / anytime |

---

## Sprint 1 — Foundation + Parser + Stream Wiring

### Completed

| Story | Description | PR | Status |
|---|---|---|---|
| S0 | Swarm cleanup — deprecate `ensemble_runner.py`, wire `CompositeEngine` | [#18](https://github.com/bhaveshhpatel/cipher/pull/18) | ✅ |
| S1 | Alert level threshold reconciliation + emit-cache flush | [#19](https://github.com/bhaveshhpatel/cipher/pull/19) | ✅ |
| S2 | Parser + detector layer fixes — direction inference, `order_side_classifier`, tier wiring into stream worker hot path | [#21](https://github.com/bhaveshhpatel/cipher/pull/21) | ✅ |
| S2-POST-5 | `_refresh_tier_map` test coverage — 5 tests | [#26](https://github.com/bhaveshhpatel/cipher/issues/26) | ✅ |
| S2-POST-6 | `_process_tick` registry avg_volume lookup path — 3 tests | [#27](https://github.com/bhaveshhpatel/cipher/issues/27) | ✅ |
| S2-POST-inline | Inline fixes 2, 3, 6 — patch comment, dead params, `is_ready` mock | [#36](https://github.com/bhaveshhpatel/cipher/pull/36) | ✅ |
| S2-POST-8 | Test isolation — `reset_tier_map_globals` autouse fixture; saves/restores `_tier_map_cache`, `_tier_map_ts`, `_tier_map_refresh_task` | [#37](https://github.com/bhaveshhpatel/cipher/pull/37) | ✅ |
| S2.5 | DB migration: `order_side` (BUY/SELL/UNKNOWN + index) + `strong_sentiment` (bool) + `execution_mechanic` (6-value enum) on `flow_events` | [#38](https://github.com/bhaveshhpatel/cipher/pull/38) | ✅ |
| S0.5 | Delete deprecated `simulation/ensemble_runner.py` | [#39](https://github.com/bhaveshhpatel/cipher/pull/39) | ✅ |
| S2-POST-2 | Flush loop done callback — `.add_done_callback` + `_on_flush_done` error logger | [#23](https://github.com/bhaveshhpatel/cipher/issues/23) | ✅ |
| S2-POST-3 | Tier map refresh double-spawn race — `_tier_map_refresh_in_progress` flag | [#24](https://github.com/bhaveshhpatel/cipher/issues/24) | ✅ |
| S2-POST-4 | `CancelledError` re-raised in `StreamWorker.run()` | [#25](https://github.com/bhaveshhpatel/cipher/issues/25) | ✅ |
| S2-POST-9 | Happy path `_refresh_tier_map` test asserts `_tier_map_refresh_task` state post-call | [#31](https://github.com/bhaveshhpatel/cipher/issues/31) | ✅ |
| S2-POST-10 | Exception test asserts `log.warning` emitted when `assign_tiers` raises | [#32](https://github.com/bhaveshhpatel/cipher/issues/32) | ✅ |
| S2-POST-11 | Test: `_get_tier_map` when refresh task already running (not done) | [#33](https://github.com/bhaveshhpatel/cipher/issues/33) | ✅ |
| S2-POST-12 | Test: inner registry exception path in `_process_tick` (`_avg_volume_by_ticker.get()` raises) | [#34](https://github.com/bhaveshhpatel/cipher/issues/34) | ✅ |
| S2-POST-13 | Test: `assign_tiers` returns empty dict `{}` in `_refresh_tier_map` | [#35](https://github.com/bhaveshhpatel/cipher/issues/35) | ✅ |
| S2-POST (PR #40) | `ensemble_runner.py` deprecated stub + `_tier_map_refresh_in_progress` save/restore in fixture + concurrent stale call test; caplog logger fix (inline) | [#40](https://github.com/bhaveshhpatel/cipher/pull/40) | ✅ |

### Low Priority / Anytime ⚪

| Story | Description | Issue | Status |
|---|---|---|---|
| S2-POST-1 | Hoist `get_registry` import out of `_process_tick` hot path | [#22](https://github.com/bhaveshhpatel/cipher/issues/22) | ⚪ Open |
| S2-POST-7 | Fix misleading `test_flush_loop_creates_task_not_blocking` name + intent | [#28](https://github.com/bhaveshhpatel/cipher/issues/28) | ⚪ Open |
| S2-POST-14 | `_flush_loop` orphaned flush tasks — cancel-on-shutdown or document as intentional | [#41](https://github.com/bhaveshhpatel/cipher/issues/41) | ⚪ Open |
| S2-POST-15 | `_get_tier_map` double-guard redundancy — remove stale `task.done()` clause or document belt-and-suspenders | [#42](https://github.com/bhaveshhpatel/cipher/issues/42) | ⚪ Open |

---

## Sprint 2 — Apex L1 / L2 / L4 Signal Layers

> ✅ **S5 is merged. Sprint 2 signal-layer foundation is complete.**
> Full story definitions: [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md)

### Completed

| Story | Description | PR | Status |
|---|---|---|---|
| S3 | Apex L1: `signal_gate.py` — spread gate (uniform 50%) + tier-aware premium floors per trade type; direction-agnostic; 100% branch coverage | [#43](https://github.com/bhaveshhpatel/cipher/pull/43) | ✅ |
| S4 | Apex L2: Dual-window accumulator — DTE-adjusted floors, OTM/ATM/deep-OTM classification, whale-conviction sweep bypass, LEAPS eligibility | [#44](https://github.com/bhaveshhpatel/cipher/pull/44) | ✅ |
| S4-POST-1 | `deep_otm_multiplier=1.0` — add explicit reject-then-pass test pair to pin `> 1.0` branch (not coincidental floor pass) | [#45](https://github.com/bhaveshhpatel/cipher/issues/45) | ✅ |
| S4-POST-2 | `_max_dte_key=None` guard — test (+ optional runtime guard) for BE-1 cache when `dte_premium_tiers={}` | [#46](https://github.com/bhaveshhpatel/cipher/issues/46) | ✅ |
| S4-POST-3 (BE-3) | `_ev_attr` / `_make_key` dict-key bug — regression tests pinning key isolation and object/dict key parity; production-path confirmed as test-only | [#47](https://github.com/bhaveshhpatel/cipher/issues/47) / [#48](https://github.com/bhaveshhpatel/cipher/pull/48) | ✅ |
| BE-3 F2 | `asyncio.run()` fix — replace deprecated `get_event_loop().run_until_complete()` in test helper | [#49](https://github.com/bhaveshhpatel/cipher/issues/49) / [#51](https://github.com/bhaveshhpatel/cipher/pull/51) | ✅ |
| BE-3 F3 | `contract_type` isolation test — CALL vs PUT same ticker/strike/expiry must produce two distinct episode keys | [#50](https://github.com/bhaveshhpatel/cipher/issues/50) / [#51](https://github.com/bhaveshhpatel/cipher/pull/51) | ✅ |
| S5 | Apex L4: Cross-contract ladder detection — multi-strike same-expiry coordination, detection primitive for later `sector_score` wiring | [#52](https://github.com/bhaveshhpatel/cipher/pull/52) | ✅ |

### Low Priority / Anytime ⚪

| Story | Description | Issue | Status |
|---|---|---|---|
| S5-POST-1 | `detect_ladder()` — document or enforce deterministic group selection when multiple `(ticker, expiry)` groups qualify | [#53](https://github.com/bhaveshhpatel/cipher/issues/53) | ⚪ Open |

---

## Sprint 3 — Apex L3 Composite + Swarm

> ✅ **S6 merged via PR #54 (2026-05-01). S6-POST-1 blocked on spec definition. S7 blocked pending stream worker concurrency review.**
> Full story definitions: [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md)

### Completed

| Story | Description | PR | Status |
|---|---|---|---|
| S6 | Apex L3: Composite formula overhaul — remove fake backtest, episode-level influence tier, `dominant_direction` hot-path fix (SELL PUT → REPEAT_BUY), `composite_score_ceiling` field, `order_side`/`strong_sentiment`/`execution_mechanic`/`premium_tier_score` in bus payload | [#54](https://github.com/bhaveshhpatel/cipher/pull/54) | ✅ |

### Blocked ⏳

| Story | Description | Issue | Status |
|---|---|---|---|
| S6-POST-1 | Wire `detect_ladder()` into `_process_trade()` hot path; pass `sector_score` into `build_composite()`; remove `composite_score_ceiling` from bus payload. **Blocked on spec definition: (1) `sector_score` normalization function undefined — binary wiring not acceptable; (2) `RepetitionAccumulator` cross-episode enumeration API unconfirmed.** | [#55](https://github.com/bhaveshhpatel/cipher/issues/55) | ⏳ Blocked |
| S7 | Tiered swarm + circuit breaker — Apex-only, timeout-bounded, deterministic fallback. **Blocked: review `stream_worker.py` to confirm whether `_process_trade()` runs sequentially or via task scheduling before scoping S7.** | TBD | ⏳ Blocked |

---

## Sprint 4 — Composite Score Formula Integrity

> **⚠️ IMMEDIATE FIX SPRINT — S9 is the first story. Must not be deferred.**
> Root cause identified in panel review 2026-05-01: composite `flow_score` uses a flat $10M
> normalization ceiling regardless of episode tier, structurally suppressing LARGE and RETAIL
> tier signals to scores of 0.10–0.25 on real flow that legitimately qualifies as significant
> within its tier. Additionally, the dead 10% sector weight is not redistributed, causing every
> composite score to be computed against a formula that only sums to 90% of its effective range.
> S9 fixes both. S10 and S11 are scoped after S9 and may not begin until S9 is merged.

---

### 🔴 S9 — Tier-Relative `flow_score` Normalization + Dead-Weight Redistribution
**Priority: IMMEDIATE FIX — Do not defer.**
**Status: 🟢 Queued (no blocker)**

> ⚠️ **DELIBERATION REQUIRED before implementation begins.**
> Senior Architect, Principal Backend Engineer, and Lead QA must deliberate on this story
> and sign off before any code is written. Deliberation points are listed explicitly below.

#### Problem Statement

`compute_flow_score()` in `composite_signal_engine.py` normalizes `total_premium` against a flat
$10,000,000 ceiling regardless of the episode's influence tier. This ceiling was designed for
WHALE-tier flow. Applied to a LARGE-tier episode (e.g., $311k), the premium component of
`flow_score` computes as `311_000 / 10_000_000 = 0.031`, producing a `flow_score` of ~0.22.

Simultaneously, `sector_score` carries a 10% weight that is permanently 0.0 (reserved for S6-POST-1).
This means the formula's active weights only sum to 90%, and composite scores are structurally
under-reported by ~8–10 points relative to the intended 0–1 scale.

These two bugs compound: a real, significant LARGE-tier episode with strong sentiment, multiple
trades, and above-threshold premium cannot achieve a BUY recommendation because its `flow_score`
is deflated by a ceiling that is irrelevant to its tier context.

#### Root Cause (files)

- `backend/signals/composite_signal_engine.py` → `compute_flow_score()` — flat $10M ceiling
- `backend/signals/composite_signal_engine.py` → `build_composite()` — weight split does not
  renormalize when `sector_score=0.0` and `backtest_score` weight is `0.00`

#### Proposed Fix

**1. Tier-relative normalization ceilings in `compute_flow_score()`:**

```python
_TIER_CEILINGS = {
    "WHALE":         10_000_000,   # unchanged
    "INSTITUTIONAL":  2_000_000,   # top of INSTITUTIONAL band
    "LARGE":            500_000,   # top of LARGE band
    "RETAIL":           100_000,   # top of RETAIL band
}

def compute_flow_score(ep: RepetitionEpisode) -> float:
    tier    = episode_influence_tier(ep)
    ceiling = _TIER_CEILINGS[tier]
    prem    = min(ep.total_premium / ceiling, 1.0)
    accel   = 0.15 if ep.is_accelerating else 0.0
    trades  = min(ep.trade_count / 20, 0.20)
    raw     = round(min(1.0, prem * 0.65 + accel + trades), 3)
    strong  = getattr(ep.events[-1], "strong_sentiment", False) if ep.events else False
    return round(raw * (1.0 if strong else 0.80), 3)
```

**2. Renormalized weights (active components only, sector reserved at 5% for S6-POST-1 wire-up):**

| Component | S6 Weight | S9 Weight | Rationale |
|---|---|---|---|
| `flow_score` | 55% | **55%** | Dominant signal — unchanged |
| `volume_premium_factor` | 20% | **20%** | Unchanged |
| `premium_tier_score` | 15% | **15%** | Unchanged |
| `otm_factor` | — | **5%** | Reserved slot for S10 (see below); neutral 0.50 fallback until S10 |
| `sector_score` | 10% | **5%** | Reduced from 10% to 5%; remainder given to `otm_factor` slot |
| `backtest_score` | 0% | **0%** | Remains zeroed until S8 |
| **TOTAL** | **100%** | **100%** | Formula now sums to 100% at all times |

> **Deliberation point (SA):** The `otm_factor` slot at 5% will hold a neutral value of 0.50
> until S10 lands. This means the formula is correct from day one of S9 — no dead weight,
> no under-reporting. When S10 activates `otm_factor` with real spot prices, scores adjust
> upward for ITM/ATM flow and downward for deep OTM — but the formula does not change shape.

**3. Tier-relative BUY/SELL thresholds in `build_composite()`:**

```python
_TIER_THRESHOLDS = {
    "WHALE":         0.65,
    "INSTITUTIONAL": 0.60,
    "LARGE":         0.55,
    "RETAIL":        0.50,
}

tier      = episode_influence_tier(ep)
threshold = _TIER_THRESHOLDS[tier]
if comp >= threshold and sentiment == "BULLISH": rec = "BUY"
elif comp >= threshold and sentiment == "BEARISH": rec = "SELL"
else: rec = "HOLD"
```

#### Scenario Analysis (evidence for deliberation)

The following table shows scores under the current (S6) formula vs. the S9 formula across
representative episodes from 2026-05-01 live flow:

**AAOI — $311k CALL, Strike=$35, 5 trades, strong_sentiment=True, OI=800, event_prem=$62k**

| Formula | `flow_score` | `vwp_f` | `prem_t` | Composite | Signal | Threshold |
|---|---|---|---|---|---|---|
| S6 (flat $10M) | 0.220 | 0.775 | 0.250 | 0.314 | HOLD | 0.65 |
| S9 (tiered $500k) | 0.604 | 0.775 | 0.250 | 0.550 | **BUY** | 0.55 |

**EBAY — $180k CALL, 3 trades, strong_sentiment=False, OI=1200:**

| Formula | `flow_score` | Composite | Signal |
|---|---|---|---|
| S6 | 0.130 | 0.209 | HOLD |
| S9 | 0.307 | 0.340 | HOLD |

EBAY correctly stays HOLD — weak sentiment, only 3 trades, no acceleration. The formula
differentiates signal quality rather than promoting everything.

**LYB — $120k CALL, 3 trades, strong_sentiment=False, OI=600:**

| Formula | `flow_score` | Composite | Signal |
|---|---|---|---|
| S6 | 0.126 | 0.240 | HOLD |
| S9 | 0.245 | 0.340 | HOLD |

**WHALE safety check — SPY $5M CALL, 12 trades, accelerating, strong_sentiment=True, OI=50k:**

| Formula | `flow_score` | Composite | Signal | Threshold |
|---|---|---|---|---|
| S6 | 0.675 | 0.541 | HOLD | 0.65 |
| S9 | 0.675 | 0.584 | HOLD | 0.65 |

WHALE episodes use the same $10M ceiling — unchanged. The 0.65 WHALE threshold is appropriately
demanding; a $5M episode with acceleration and strong sentiment scores 0.584 and correctly stays
HOLD until more trades accumulate or premium crosses a higher band. WHALE BUY requires strong
conviction across all components.

**Gaming resistance check — $100k LARGE, OI=50 (thin), 3 trades, no accel, no strong sentiment:**

```
flow_score = min(100k/500k,1.0)*0.65 + 0 + min(3/20,0.20)*0.80 = 0.104 + 0.12 = 0.224 -> *0.80 = 0.179
vwp_factor = min(1.0, 33_000 / (50 * 100)) = min(1.0, 6.6) = 1.0  # thin OI actually elevates vwp
composite  = 0.179*0.55 + 1.0*0.20 + 0.0*0.15 + 0.50*0.05 = 0.098+0.20+0+0.025 = 0.323 → HOLD
```

A barely-LARGE episode with thin OI and no quality markers stays HOLD at 0.323 vs. the 0.55
LARGE threshold. The formula does not auto-promote weak prints.

#### Deliberation Points

1. **(SA)** Confirm `_TIER_CEILINGS` values are correct. Specifically: should LARGE ceiling be
   $500k (top of the LARGE band = bottom of INSTITUTIONAL) or $250k (midpoint)? Midpoint would
   make the ceiling more conservative for large-but-not-institutional episodes.

2. **(SA + BE)** The `strong_sentiment` field is read from `ep.events[-1]` inside
   `compute_flow_score()`. This means the discount applies based on the most recent event, not
   the dominant sentiment across the episode. Is this correct, or should it use a majority-vote
   across all events in `ep.events`?

3. **(BE)** `episode_influence_tier()` is called twice in `build_composite()` — once for the
   ceiling lookup and once for the threshold lookup. Confirm this is acceptable or refactor to
   compute once and pass down.

4. **(QA)** Regression test matrix must cover: (a) WHALE episode scores unchanged vs. S6,
   (b) LARGE episode floor coverage (at $100k, $250k, $499k), (c) RETAIL episode at $50k and
   $99k, (d) `strong_sentiment=False` discount still applies after tier normalization,
   (e) gaming resistance: thin-OI barely-LARGE episode stays HOLD.

5. **(QA)** `COMPOSITE_SCORE_CEILING` constant must be updated or documented. With S9's weight
   split, the theoretical maximum composite score is no longer structurally capped at 0.90 —
   it is now a full 1.0 scale (sector at 5% is no longer dead). Document whether
   `COMPOSITE_SCORE_CEILING` should be removed, updated to 0.95 (sector reserved), or kept as
   a soft frontend hint.

#### Acceptance Criteria

- [ ] `_TIER_CEILINGS` dict defined at module level in `composite_signal_engine.py`
- [ ] `compute_flow_score()` uses `episode_influence_tier(ep)` to select ceiling
- [ ] `_TIER_THRESHOLDS` dict defined at module level
- [ ] `build_composite()` uses tier-relative threshold for rec logic
- [ ] `otm_factor` slot exists in formula at 5% weight with `0.50` neutral value (hardcoded until S10)
- [ ] `sector_score` weight reduced from 10% to 5%
- [ ] WHALE episode composite scores are within ±0.005 of S6 scores (regression guard)
- [ ] AAOI $311k scenario produces composite ≥ 0.55 and recommendation = BUY
- [ ] EBAY $180k scenario produces HOLD
- [ ] LYB $120k scenario produces HOLD
- [ ] All existing `test_composite_signal_engine.py` tests updated to new weight split
- [ ] New parametrized test: `test_tier_relative_flow_score[WHALE/INSTITUTIONAL/LARGE/RETAIL]`
- [ ] New test: `test_buy_threshold_by_tier` — each tier hits BUY at exactly its threshold

---

### ⏳ S10 — `underlying_price` Population + OTM Factor in Composite
**Priority: Scope after S9 — do not start until S9 is merged.**
**Status: ⏳ Blocked on S9**

> ⚠️ **DELIBERATION REQUIRED before implementation begins.**
> Senior Architect, Principal Backend Engineer, and Lead QA must deliberate on this story
> and sign off before any code is written. Deliberation points are listed explicitly below.

#### Problem Statement

`underlying_price` on every `OptionsFlowEvent` is permanently `0.0` in production. The Tradier
`timesale` stream does not carry the underlying spot price in its event payload —
`parse_tradier_trade()` reads `raw.get("underlying_price", 0)` which always returns `0` because
the field does not exist in stream data.

This has two downstream consequences:

1. **OTM/ITM classification is impossible.** Without knowing the spot price relative to the
   strike, the system cannot distinguish a $311k CALL on a stock trading 20% below strike (deep
   OTM lottery ticket) from a $311k CALL on a stock trading at the money (real institutional
   interest). These have meaningfully different signal quality.

2. **The `otm_factor` slot reserved in S9 remains at its neutral 0.50 fallback**, contributing
   equally regardless of actual moneyness. Once S10 lands, real spot prices replace the neutral
   fallback and the formula becomes fully active.

#### Architecture

**Source of truth:** Tradier REST quotes endpoint.
```
GET /v1/markets/quotes?symbols=AAPL,TSLA,NVDA,...
Response: quotes.quote[].last  →  current spot price
```

This is a batch endpoint — all watchlist tickers can be fetched in a single call.

**Implementation plan:**

1. **`symbol_registry.py`** — add `_spot_cache: dict[str, float]` and
   `async def refresh_spot_prices(tickers: list[str])` method. Called from existing
   `refresh_loop()` every 60 seconds. One REST call per batch.

2. **`symbol_registry.py`** — add `def get_spot(self, ticker: str) -> float` returning
   `self._spot_cache.get(ticker, 0.0)`.

3. **`options_flow_parser.py`** — after the existing registry lookup block:
   ```python
   if reg and reg.is_ready() and ev.underlying_price == 0.0:
       spot = reg.get_spot(ev.ticker)
       if spot > 0:
           ev.underlying_price = spot
   ```

4. **`composite_signal_engine.py`** — replace the hardcoded `otm_factor = 0.50` with a real
   computation using `ep.events[-1].underlying_price`.

#### OTM Factor Formula

The proposed `otm_factor` function uses a piecewise moneyness scale:

```python
def compute_otm_factor(strike: float, underlying: float, contract_type: str) -> float:
    """
    Returns a [0.30, 1.0] quality score based on moneyness.
    Falls back to neutral 0.50 if underlying_price is 0.

    For CALL:  moneyness = underlying / strike
    For PUT:   moneyness = strike / underlying

    Breakpoints (same for both):
      moneyness >= 1.05  →  ITM:       1.00  (in-the-money, highest quality)
      0.95–1.05          →  ATM:       0.85  (at-the-money)
      0.80–0.95          →  OTM:       0.50–0.85 (linear interpolation)
      < 0.80             →  Deep OTM:  0.30  (lottery territory)
    """
    if underlying <= 0:
        return 0.50
    m = (underlying / strike) if contract_type == "CALL" else (strike / underlying)
    if m >= 1.05: return 1.00
    if m >= 0.95: return 0.85
    if m >= 0.80: return round(0.50 + (m - 0.80) / (0.95 - 0.80) * 0.35, 3)
    return 0.30
```

#### Scenario Analysis — AAOI with Spot Price Variants

**AAOI $311k CALL, Strike=$35, flow_score=0.604, vwp_f=0.775, prem_t=0.250**
(S9 weights: flow=0.55, vwp=0.20, prem=0.15, otm=0.05)

| Scenario | Underlying | Moneyness | OTM Factor | Composite | Signal |
|---|---|---|---|---|---|
| No spot (S9 neutral) | 0.00 | — | 0.500 | 0.550 | **BUY** |
| Deep ITM ($42) | 42.00 | 1.200 | 1.000 | 0.575 | **BUY** |
| Slight ITM ($36) | 36.00 | 1.029 | 0.850 | 0.567 | **BUY** |
| ATM ($35) | 35.00 | 1.000 | 0.850 | 0.567 | **BUY** |
| Slight OTM ($33) | 33.00 | 0.943 | 0.833 | 0.566 | **BUY** |
| OTM ($30) | 30.00 | 0.857 | 0.633 | 0.556 | **BUY** |
| Deep OTM ($25) | 25.00 | 0.714 | 0.300 | 0.540 | **HOLD** |

Key observations:
- AAOI remains BUY across all realistic moneyness scenarios (slight OTM to ITM).
- Deep OTM ($25 stock vs. $35 strike = 28.6% OTM) correctly demotes to HOLD — this is
  lottery-ticket territory where $311k CALL flow carries less signal quality.
- The `otm_factor` at 5% weight is a quality modifier, not a binary gate. It adjusts by
  ~0.025 max — it does not flip a strong signal to noise.

**AAOI with acceleration (is_accelerating=True, stock=$36):**

| Scenario | flow_score | OTM Factor | Composite | Signal |
|---|---|---|---|---|
| Accel + slight ITM | 0.754 | 0.850 | 0.650 | **BUY** |

An accelerating $311k AAOI episode with the stock near the strike scores 0.650 — comfortably
above the 0.55 LARGE threshold. Strong signal, correctly promoted.

**Broader matrix — all tiers:**

| Ticker | Tier | Total Prem | Underlying | OTM_f | Flow | Composite | Signal |
|---|---|---|---|---|---|---|---|
| AAOI | LARGE | $311k | $36.00 (ITM) | 0.850 | 0.604 | 0.567 | **BUY** |
| AAOI | LARGE | $311k | $30.00 (OTM) | 0.633 | 0.604 | 0.556 | **BUY** |
| AAOI | LARGE | $311k | $22.00 (deep OTM) | 0.300 | 0.604 | 0.540 | HOLD |
| EBAY | LARGE | $180k | $58.50 (ATM) | 0.850 | 0.307 | 0.349 | HOLD |
| LYB | LARGE | $120k | $69.00 (ATM) | 0.850 | 0.245 | 0.348 | HOLD |
| SPY | WHALE | $5M | $515 (ATM) | 0.850 | 0.675 | 0.584 | HOLD |
| NVDA | INST. | $800k | $895 (ATM) | 0.850 | 0.610 | 0.548 | HOLD |

EBAY and LYB correctly stay HOLD even with a spot price known — their weakness is
`flow_score` (no strong sentiment, only 3 trades), not the OTM factor.

#### Deliberation Points

1. **(SA)** Confirm spot price refresh cadence. 60 seconds aligns with existing
   `refresh_loop()`. If market is closed (after 4pm ET), the cache can hold last known spot.
   Is stale spot price (e.g., previous close) acceptable for after-hours flow, or should
   `underlying_price = 0.0` and `otm_factor = 0.50` neutral be forced outside market hours?

2. **(SA + BE)** `otm_factor` uses `ep.events[-1].underlying_price`. If the last event has
   `underlying_price=0.0` but earlier events have real values, the factor falls back to neutral.
   Should it use the most recent non-zero value across `ep.events` instead?

3. **(BE)** Tradier REST quotes endpoint rate limit is 120 requests/minute on sandbox and
   higher on production. A batch call with 50 tickers counts as 1 request. Confirm watchlist
   size does not exceed batch limit and add a guard if it does (split into batches of 50).

4. **(QA)** Test matrix must include: (a) `underlying_price=0.0` → `otm_factor=0.50` fallback,
   (b) CALL ITM, ATM, OTM, deep-OTM cases, (c) PUT ITM, ATM, OTM, deep-OTM cases,
   (d) `refresh_spot_prices` — mock Tradier REST, confirm cache population, (e) `get_spot`
   returns 0.0 for unknown ticker, (f) parser enrichment path: registry ready + spot > 0
   overwrites `ev.underlying_price`.

5. **(QA)** Confirm `is_synthetic_quote=True` interaction: synthetic quotes already apply a
   40% conviction haircut. Should `otm_factor` also be forced to `0.50` neutral for synthetic
   quotes to avoid double-penalizing a contract whose bid/ask we don't know?

#### Acceptance Criteria

- [ ] `SymbolRegistry` has `_spot_cache: dict[str, float]`
- [ ] `SymbolRegistry.refresh_spot_prices(tickers)` calls Tradier REST, populates cache
- [ ] `refresh_spot_prices` called from `refresh_loop()` every 60s
- [ ] `SymbolRegistry.get_spot(ticker)` returns `0.0` for unknown tickers (no KeyError)
- [ ] `parse_tradier_trade()` enriches `ev.underlying_price` from registry spot cache when `ev.underlying_price == 0.0`
- [ ] `compute_otm_factor()` function implemented in `composite_signal_engine.py`
- [ ] `otm_factor` in `build_composite()` uses `compute_otm_factor()` with live value; falls back to `0.50` when `underlying_price == 0.0`
- [ ] `underlying_price` field visible in `/health/stream` or a dedicated debug endpoint
- [ ] AAOI $311k CALL + stock $22 (deep OTM) → composite < 0.55 → HOLD (regression guard)
- [ ] AAOI $311k CALL + stock $36 (slight ITM) → composite ≥ 0.55 → BUY (regression guard)
- [ ] All `test_composite_signal_engine.py` tests updated for `otm_factor` active weight

---

### ⏳ S11 — Full Composite Activation: Real Backtest Score (S8) + Sector Score (S6-POST-1)
**Priority: Future sprint — scope after S10 is merged.**
**Status: ⏳ Blocked on S9 + S10 + S6-POST-1 + S8**

> ⚠️ **DELIBERATION REQUIRED before implementation begins.**
> Senior Architect, Principal Backend Engineer, and Lead QA must deliberate on this story
> and sign off before any code is written. This story cannot begin until S9, S10, S6-POST-1,
> and S8 are all merged. Deliberation points are listed explicitly below.

#### Problem Statement

With S9 and S10 merged, the composite formula has four active components (flow, vwp, prem, otm)
summing to 95% of weight. The remaining 5% (`sector_score`) and 0% (`backtest_score`) are
reserved slots. S11 activates both when their upstream data is available:

- **`sector_score`** becomes available when S6-POST-1 wires `detect_ladder()` into the hot path
  and defines a normalization function (currently undefined — blocker documented in issue #55).
- **`backtest_score`** becomes available when S8 implements the 90-day rolling win-rate query
  from `flow_events` grouped by `(ticker, contract_type, dte_bucket, influence_tier)`.

#### Proposed Final Weight Split

| Component | S9/S10 Weight | S11 Weight | Notes |
|---|---|---|---|
| `flow_score` (tiered ceiling) | 55% | **50%** | Slight reduction to make room |
| `volume_premium_factor` | 20% | **18%** | Slight reduction |
| `premium_tier_score` | 15% | **12%** | Slight reduction |
| `otm_factor` | 5% | **5%** | Unchanged |
| `sector_score` | 5% (reserved 0.0) | **8%** | Activated — requires normalization fn |
| `backtest_score` | 0% | **7%** | Activated — requires S8 win-rate data |
| **TOTAL** | **100%** | **100%** | |

> **Note:** These weights are a starting proposal for deliberation — not a final decision.
> The panel must deliberate on the weight split once real backtest data is available to
> assess its variance and correlation with other components. A backtest win-rate signal with
> low variance (e.g., 0.60–0.70 for most tickers) behaves very differently from one with
> high variance (0.30–0.90). The panel should run a calibration pass on 30 days of historical
> `flow_events` before finalizing `backtest_score` weight.

#### Backtest Score Implementation (S8 dependency)

The existing `get_backtest_score()` stub in `backtest_validator.py` has the correct function
signature and is already called in `build_composite()` with weight `0.00`. The implementation
change is the query behind it:

```python
# Current (fake): seeded pseudo-random per (ticker, contract_type, dte_bucket, influence_tier)
# S8 implementation:
async def get_backtest_score(ticker, contract_type, dte, influence_tier) -> float:
    """
    Query flow_events for historical win-rate over rolling 90 days.
    Win = signal episode where underlying moved in predicted direction
         within DTE days of the episode timestamp.
    Returns 0.5 (neutral) if fewer than 10 historical episodes found.
    Cached per session with 1-hour TTL.
    """
    key = (ticker, contract_type, _dte_bucket(dte), influence_tier)
    # Supabase query: SELECT COUNT(*) won, COUNT(*) total FROM signal_history
    #   WHERE ticker=? AND contract_type=? AND dte_bucket=? AND influence_tier=?
    #   AND created_at >= NOW() - INTERVAL '90 days'
    # win = rows where direction matched subsequent price movement
    # Requires: outcome tracking column on signal_history (new migration needed)
```

> **Critical dependency:** S8 requires an `outcome` column on `signal_history` (or a
> separate `signal_outcomes` table) that records whether each signal's directional prediction
> was correct within the contract's DTE window. This data does not currently exist — it
> requires either a scheduled job that resolves outcomes post-DTE or an enrichment pass on
> historical flow. This architectural decision must be made before S8 scoping begins.

#### Sector Score Normalization (S6-POST-1 dependency)

`sector_score` is currently gated by issue #55 on two unresolved questions:
1. The normalization function for `detect_ladder()` output → `[0, 1]` score is undefined.
2. `RepetitionAccumulator.get_all_active_episodes()` API is unconfirmed.

S11 assumes both are resolved by S6-POST-1. The weight of 8% for `sector_score` in S11 is
provisional — if the ladder detection signal proves noisy or low-precision after initial wiring,
the panel should reduce the weight or gate it behind a confidence threshold.

#### `COMPOSITE_SCORE_CEILING` Resolution

`COMPOSITE_SCORE_CEILING = 0.90` was set in S6 when sector carried 10% dead weight. With S9,
the formula sums to 100% at all times (otm_factor neutral fallback ensures this). The constant
should be:
- **Removed** from `composite_signal_engine.py` and the bus payload after S11, OR
- **Updated to 1.0** to reflect the now-fully-active formula, OR
- **Retained as a soft cap** (e.g., `min(comp, 0.95)`) if the panel decides no episode should
  ever score as perfect conviction

The panel must decide this before S11 implementation begins.

#### Deliberation Points

1. **(SA)** Confirm the final weight split. Specifically: is `backtest_score` at 7% appropriate
   before calibration data exists? Consider starting at 5% and raising after 30 days of
   outcome data.

2. **(SA + BE)** `signal_outcomes` table design: what constitutes a "win"? Options:
   (a) underlying moved in predicted direction by expiry, (b) moved in predicted direction
   within 3 days, (c) option premium increased by ≥20% within 2 days. Each definition
   produces a different win-rate distribution and different signal quality.

3. **(SA)** Resolve `COMPOSITE_SCORE_CEILING` — remove, update, or retain as hard cap.

4. **(BE)** Confirm that activating `backtest_score` mid-sprint does not cause score
   discontinuities in the frontend (e.g., signals that were BUY at 0.55 composite may become
   HOLD if the backtest penalty is significant). Frontend consumers must be notified of the
   formula change before S11 deploys.

5. **(QA)** Regression test matrix must include: (a) scores with `backtest_score=0.5` (neutral)
   vs. `backtest_score=0.3` (poor) vs. `backtest_score=0.8` (strong), (b) composite ceiling
   behavior, (c) end-to-end: episode → `build_composite()` → bus payload → `signal_history`
   write with all 6 active components present.

#### Acceptance Criteria

- [ ] S6-POST-1 merged (sector_score wired, normalization defined)
- [ ] S8 merged (real backtest win-rate from Supabase, outcome tracking in place)
- [ ] S9 merged (tier-relative flow_score, tier thresholds)
- [ ] S10 merged (underlying_price populated, otm_factor live)
- [ ] Final weight split deliberated and documented in `docs/cipher_apex_story_and_sprint_plan.md`
- [ ] `COMPOSITE_SCORE_CEILING` constant decision implemented
- [ ] `build_composite()` uses all 6 active components with correct weights
- [ ] End-to-end test: mock all 6 component values, assert composite formula arithmetic correct
- [ ] Regression: WHALE episodes still require ≥0.65 for BUY
- [ ] Deploy notes: frontend consumers notified of formula change before merge

---

## Future Sprint

| Story | Description | Issue | Status |
|---|---|---|---|
| S8 | Real backtest score from `flow_events` — 90-day win-rate by (ticker, contract_type, dte_bucket); cache-controlled; re-enables backtest weight. **Requires `signal_outcomes` tracking table — architectural decision needed before scoping.** | TBD | ⏳ Future |

---

## Parallel Tracks (not blocked by APEX sprint)

| Story | Description | Issue | Status |
|---|---|---|---|
| ING-1 | Ingestion rewrite + delta chain fetch — eliminate duplicate Tradier call, fix upsert on HIT path, fix tier write-back, cut rebuild time ~6 min → ~30s | [#6](https://github.com/bhaveshhpatel/cipher/issues/6) | 🟢 Open |
| C8 | Decouple persist tier from signal tier in `_process_trade` — full tick history in `flow_events` for backtesting | [#2](https://github.com/bhaveshhpatel/cipher/issues/2) | 🟢 Open |

---

## Fully Ordered Build Sequence

Exact execution order. Do not start a story until everything above it in the same gate block is merged.

```
── SPRINT 1 ──────────────────────────────────────────────────────────────────────────────────
1.  ✅  S0            — Swarm cleanup (PR #18)
2.  ✅  S1            — Alert level reconciliation + emit-cache flush (PR #19)
3.  ✅  S2            — Parser + detector + stream worker tier wiring (PR #21)
4.  ✅  #26+#27       — _refresh_tier_map + _process_tick test coverage
5.  ✅  #36           — Inline fixes 2/3/6 (PR #36)
6.  ✅  #30           — S2-POST-8: test isolation / module global teardown (PR #37)
7.  ✅  #29           — S2.5: order_side + strong_sentiment + execution_mechanic DB migration (PR #38)
8.  ✅  #20           — S0.5: Delete ensemble_runner.py (PR #39)
9.  ✅  #23+#24+#25   — S2-POST-2/3/4: flush done callback, tier map race fix, CancelledError re-raise
10. ✅  #31–35        — S2-POST-9–13: 5 test coverage additions (PR #40)
11. ✅  PR #40        — ensemble_runner.py stub, concurrent stale call test, caplog logger fix
───────────────────────────────────────────────────────────────────────────────────

── SPRINT 2 ──────────────────────────────────────────────────────────────────────────────────
12. ✅  S3            — Apex L1: signal_gate.py (PR #43)
13. ✅  S4            — Apex L2: dual-window accumulator (PR #44)
14. ✅  #45           — S4-POST-1: deep_otm_multiplier=1.0 reject-then-pass test pair
15. ✅  #46           — S4-POST-2: _max_dte_key=None guard
16. ✅  #47/#48       — S4-POST-3 (BE-3): _ev_attr dict-key bug regression tests
17. ✅  #49+#50/#51   — BE-3 F2+F3: asyncio.run() fix + contract_type isolation test
18. ✅  S5            — Apex L4: ladder detection (PR #52)
───────────────────────────────────────────────────────────────────────────────────

── SPRINT 3 ──────────────────────────────────────────────────────────────────────────────────
19. ✅  S6            — Apex L3: composite formula overhaul (PR #54)
20. ⏳  S6-POST-1     — Wire detect_ladder() + sector_score into hot path (#55)  ← BLOCKED on spec definition
21. ⏳  S7            — Tiered swarm + circuit breaker  ← BLOCKED (stream worker review first)
───────────────────────────────────────────────────────────────────────────────────

── SPRINT 4 — COMPOSITE FORMULA INTEGRITY ──────────────────────────────────────────────────
22. 🔴  S9            — Tier-relative flow_score normalization + dead-weight redistribution  ← IMMEDIATE FIX
23. ⏳  S10           — underlying_price population + OTM factor activation  ← BLOCKED on S9
24. ⏳  S11           — Full composite activation (backtest + sector)  ← BLOCKED on S9 + S10 + S6-POST-1 + S8
───────────────────────────────────────────────────────────────────────────────────

── FUTURE SPRINT ───────────────────────────────────────────────────────────────────────────
25. ⏳  S8            — Real backtest score from flow_events (requires signal_outcomes architecture)
───────────────────────────────────────────────────────────────────────────────────

── PARALLEL / ANYTIME ─────────────────────────────────────────────────────────────────
26. 🟢  ING-1         — Ingestion rewrite + delta chain fetch (#6)
27. 🟢  C8            — Decouple persist/signal tier (#2)
28. ⚪  #22           — Hoist get_registry import
29. ⚪  #28           — Fix misleading flush loop test
30. ⚪  #41           — _flush_loop orphaned flush tasks (cancel-on-shutdown or document)
31. ⚪  #42           — _get_tier_map double-guard redundancy (clean up or document)
32. ⚪  #53           — detect_ladder() deterministic group selection when multiple groups qualify
───────────────────────────────────────────────────────────────────────────────────
```

---

## Quick Reference

- **"What is next?"** → S9 (IMMEDIATE FIX — tier-relative flow_score normalization). Panel deliberation required before implementation. S7 prep and S6-POST-1 spec definition can run in parallel.
- **"What unblocks S6-POST-1?"** → (1) Decide `sector_score` normalization function — document in spec. (2) Confirm or add `RepetitionAccumulator.get_all_active_episodes()`. See issue [#55](https://github.com/bhaveshhpatel/cipher/issues/55).
- **"What unblocks S7?"** → Review `stream_worker.py` to confirm whether `_process_trade()` runs sequentially or via task scheduling.
- **"What unblocks S10?"** → S9 merged.
- **"What unblocks S11?"** → S9 + S10 + S6-POST-1 + S8 all merged.
- **"What is remaining?"** → Every row not marked ✅ — steps 20 through 32.
- **"What is the full plan?"** → Read [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md) for story definitions, then this file for current status.
- **After every merge** → Mark row ✅, update version below.
- **After every panel review** → File issues for all findings, add rows to this file before merging.
- **Workflow rule** → Branch + PR always. Never push directly to `main`.

---

*Last updated: 2026-05-01 — S6 completed via PR #54. Sprint 4 added: S9 (IMMEDIATE FIX — tier-relative composite score normalization), S10 (underlying_price + OTM factor, scoped after S9), S11 (full composite activation with backtest + sector, future). Panel deliberation required before S9 implementation begins.*
*Version: 4.0*
