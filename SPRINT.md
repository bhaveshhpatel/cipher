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
> **The deliberation points are architectural concerns that apply to the formula and scoring
> system generally — they are not specific to any single ticker or trade example. Examples
> used in scenario analysis are illustrative only.**

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

**3. Tier-relative BUY/SELL thresholds in `build_composite()`:**

```python
_TIER_THRESHOLDS = {
    "WHALE":         0.65,
    "INSTITUTIONAL": 0.60,
    "LARGE":         0.55,
    "RETAIL":        0.50,
}
```

#### Deliberation Points

1. **(SA)** Confirm `_TIER_CEILINGS` values are correct. LARGE ceiling: $500k (top of band) or $250k (midpoint)?
2. **(SA)** Dollar-amount vs. Vol/OI ratio: should a future story replace `vwp_factor` with a unified `flow_quality_factor`?
3. **(SA + BE)** `strong_sentiment` read from `ep.events[-1]` — should it use majority-vote across all events?
4. **(BE)** `episode_influence_tier()` called twice in `build_composite()` — refactor to compute once.
5. **(QA)** Regression matrix: WHALE unchanged, LARGE floor coverage, RETAIL coverage, sentiment discount, gaming resistance, single-print HOLD across all tiers.
6. **(QA)** `COMPOSITE_SCORE_CEILING` — remove, update to 0.95, or retain as soft hint.

#### Acceptance Criteria

- [ ] `_TIER_CEILINGS` dict defined at module level in `composite_signal_engine.py`
- [ ] `compute_flow_score()` uses `episode_influence_tier(ep)` to select ceiling
- [ ] `_TIER_THRESHOLDS` dict defined at module level
- [ ] `build_composite()` uses tier-relative threshold for rec logic
- [ ] `otm_factor` slot exists in formula at 5% weight with `0.50` neutral value (hardcoded until S10)
- [ ] `sector_score` weight reduced from 10% to 5%
- [ ] WHALE episode composite scores are within ±0.005 of S6 scores (regression guard)
- [ ] AAOI $311k scenario produces composite ≥ 0.55 and recommendation = BUY
- [ ] GLXY $263k single-print scenario produces HOLD
- [ ] EBAY $180k scenario produces HOLD
- [ ] LYB $120k scenario produces HOLD
- [ ] All existing `test_composite_signal_engine.py` tests updated to new weight split
- [ ] New parametrized test: `test_tier_relative_flow_score[WHALE/INSTITUTIONAL/LARGE/RETAIL]`
- [ ] New test: `test_buy_threshold_by_tier`
- [ ] New test: `test_single_print_hold_across_tiers`

---

### ⏳ S10 — `underlying_price` Population + OTM Factor in Composite
**Priority: Scope after S9 — do not start until S9 is merged.**
**Status: ⏳ Blocked on S9**

> ⚠️ **DELIBERATION REQUIRED before implementation begins.**

#### Problem Statement

`underlying_price` on every `OptionsFlowEvent` is permanently `0.0` in production. The Tradier
`timesale` stream does not carry the underlying spot price. Without it: (1) OTM/ITM classification
is impossible, (2) the `otm_factor` slot reserved in S9 remains at its neutral 0.50 fallback.

#### Architecture

- `SymbolRegistry` gains `_spot_cache` + `refresh_spot_prices()` (Tradier REST batch quotes, every 60s)
- `parse_tradier_trade()` enriches `ev.underlying_price` from registry cache when `== 0.0`
- `compute_otm_factor()` implemented in `composite_signal_engine.py` using piecewise moneyness scale

#### Deliberation Points

1. **(SA)** Stale spot price acceptability after market close.
2. **(SA + BE)** `otm_factor` uses `ep.events[-1].underlying_price` — use most recent non-zero instead?
3. **(BE)** Tradier REST batch limit — guard for watchlists > 50 tickers.
4. **(QA)** Test matrix: `underlying_price=0.0` fallback, CALL/PUT ITM/ATM/OTM/deep-OTM cases, parser enrichment path.
5. **(QA)** `is_synthetic_quote=True` interaction — force `otm_factor=0.50` neutral for synthetic quotes?

#### Acceptance Criteria

- [ ] `SymbolRegistry` has `_spot_cache: dict[str, float]`
- [ ] `SymbolRegistry.refresh_spot_prices(tickers)` calls Tradier REST, populates cache
- [ ] `refresh_spot_prices` called from `refresh_loop()` every 60s
- [ ] `SymbolRegistry.get_spot(ticker)` returns `0.0` for unknown tickers (no KeyError)
- [ ] `parse_tradier_trade()` enriches `ev.underlying_price` from registry spot cache when `== 0.0`
- [ ] `compute_otm_factor()` implemented in `composite_signal_engine.py`
- [ ] `otm_factor` in `build_composite()` uses `compute_otm_factor()` with live value; falls back to `0.50`
- [ ] `underlying_price` visible in `/health/stream` or debug endpoint
- [ ] AAOI $311k CALL + stock $22 (deep OTM) → composite < 0.55 → HOLD
- [ ] AAOI $311k CALL + stock $36 (slight ITM) → composite ≥ 0.55 → BUY
- [ ] All `test_composite_signal_engine.py` tests updated for `otm_factor` active weight

---

### ⏳ S11 — Full Composite Activation: Real Backtest Score (S8) + Sector Score (S6-POST-1)
**Priority: Future sprint — scope after S10 is merged.**
**Status: ⏳ Blocked on S9 + S10 + S6-POST-1 + S8**

> ⚠️ **DELIBERATION REQUIRED before implementation begins.**

Final weight split (proposed — subject to panel deliberation once S8 backtest data available):

| Component | S9/S10 Weight | S11 Weight |
|---|---|---|
| `flow_score` | 55% | **50%** |
| `volume_premium_factor` | 20% | **18%** |
| `premium_tier_score` | 15% | **12%** |
| `otm_factor` | 5% | **5%** |
| `sector_score` | 5% (reserved 0.0) | **8%** |
| `backtest_score` | 0% | **7%** |
| **TOTAL** | **100%** | **100%** |

#### Acceptance Criteria

- [ ] S6-POST-1 merged (sector_score wired, normalization defined)
- [ ] S8 merged (real backtest win-rate from Supabase, outcome tracking in place)
- [ ] S9 merged + S10 merged
- [ ] Final weight split deliberated and documented in `docs/cipher_apex_story_and_sprint_plan.md`
- [ ] `COMPOSITE_SCORE_CEILING` constant decision implemented
- [ ] `build_composite()` uses all 6 active components with correct weights
- [ ] End-to-end test: mock all 6 component values, assert composite formula arithmetic correct
- [ ] Regression: WHALE episodes still require ≥0.65 for BUY
- [ ] Deploy notes: frontend consumers notified of formula change before merge

---

## Sprint 5 — Repeat Conviction Engine

> **⚠️ IMMEDIATE FIX — S12 requires deliberation before implementation begins.**
> Root cause identified in architecture review 2026-05-02: the `RepetitionAccumulator` operates
> exclusively on a 30-minute intraday window, missing multi-session institutional accumulation
> entirely. Simultaneously, the composite formula ranks premium size over repeat conviction —
> structurally backwards relative to the highest-conviction signal patterns observed in
> institutional options flow. S12 fixes both. S12 may run in parallel with S9 deliberation
> but the weight split conflict between S9 and S12 must be resolved before either begins
> implementation (see S12 Deliberation Point 5).
>
> GitHub Issue: [#56](https://github.com/bhaveshhpatel/cipher/issues/56)

---

### 🔴 S12 — Dual-Window Repeat Conviction Engine: Cross-Session Accumulation + Composite Score Reweight
**Priority: IMMEDIATE FIX — Do not defer.**
**Status: 🟢 Queued — panel deliberation required before implementation**

> ⚠️ **DELIBERATION REQUIRED before implementation begins.**
> **Senior Software Director**, **Platform Backend Engineer**, and **QA Lead** must deliberate
> on this story and sign off before any code is written. Deliberation points are listed
> explicitly below. See GitHub Issue [#56](https://github.com/bhaveshhpatel/cipher/issues/56)
> for full context.
> **Work must never be pushed directly to `main`. Always branch + PR.**

#### Problem Statement

The `RepetitionAccumulator` (`backend/signals/repetition_accumulator.py`) operates exclusively
on a short intraday rolling window (`window_minutes=30`). This correctly catches intraday
stacking but **misses the most institutionally significant pattern entirely**: the same
ticker + contract + expiry accumulating call or put flow across **multiple separate trading
sessions**.

Simultaneously, `composite_signal_engine.py` (`build_composite()`) uses a formula where premium
size is baked into 3 of 4 active components:

```
flow_score * 0.55 + vwp_factor * 0.20 + premium_tier * 0.15 + sector * 0.10
```

Where `flow_score` itself = `(premium/10M) * 0.65 + acceleration * 0.15 + trade_count * 0.20`.

A **$2M intraday print on a large-cap with no repeat scores higher than a $100K 3-day repeat
sweep on a small-cap.** That is structurally backwards. The composite score currently ranks
**size**, not **conviction**.

#### Background: WSJ Repeat Pattern Analysis

Analysis of institutional options flow signal patterns identifies two distinct timescales at
which "repeat" flow fires — both must be detected:

**Intraday Repeat** — same ticker, same contract, multiple sweeps within hours. This is
**urgency**. Someone is building a position aggressively right now. The current accumulator
with `window_minutes=30` catches this but the window is too tight — intraday repeats span
the full trading session (6.5 hours), not 30 minutes.

**Multi-Day Repeat** — same ticker, same expiry, call flow showing up Monday then again
Wednesday. This is **conviction**. Someone is accumulating without urgency, which is actually
*more* institutional. The current architecture has **zero coverage** for this.

**Signal Taxonomy (Actionable vs. Informational):**

| Signal Tier | Pattern | Example | Action |
|---|---|---|---|
| Tier 1 — Repeat Sweeper | `repeat_type=BOTH` or `MULTI_DAY` 3+ sessions, P/C ≥ 10:1, SWEEP confirmed | `$NOK REPEAT SWEEPER BUYING — Put/Call: 11k/116k` | **SIGNAL — surface immediately** |
| Tier 1 — Size Repeat | Multi-session + single large sweep bypass | `$YPF SIZE REPEAT SWEEPER BUYING` | **SIGNAL — surface immediately** |
| Tier 2 — First Observation | Single session, P/C ≥ 8:1, no repeat yet | `$VG Put/Call: 777/11k` | **WATCH — monitor for follow-through** |
| Tier 3 — Informational | Macro data, analyst ratings, sentiment indices, geopolitical headlines | AAII, GS desk commentary, CTA positioning | **NOISE — do not surface** |

Tier 2 upgrades to Tier 1 **only when the same ticker+contract+expiry shows qualifying flow
in a second session.** That second-session confirmation is the precise trigger missing from
the current architecture.

#### Proposed Fix

##### Part 1 — Unified Dual-Window Episode Model

Do **not** build two separate detectors. Extend `RepetitionEpisode` and
`RepetitionAccumulator` to track both windows simultaneously on the **same episode key**.

> **Critical:** The episode key must remain `ticker|contract_type|strike|expiry`. Do NOT
> loosen it to `ticker|contract_type`. Institutional flow tracking the same strike+expiry
> contract across sessions — ticker-level repeat alone is insufficient and will produce false
> positives. The existing `_key()` method is correct and must not be changed.

**New `RepeatEpisode` dataclass:**

```python
@dataclass
class RepeatEpisode:
    ticker: str
    contract_type: str
    expiry: str

    # Intraday window — rolling 6.5hr session
    intraday_events: List[FlowEvent]
    intraday_sweep_count: int
    intraday_pc_ratio: float

    # Multi-day window — rolling 5 calendar days
    session_dates: List[date]          # unique calendar days with qualifying flow
    per_session_pc_ratios: List[float]
    per_session_had_sweep: List[bool]

    # Computed
    @property
    def repeat_type(self) -> str:
        intraday = len(self.intraday_events) >= 3
        multiday = len(set(self.session_dates)) >= 2

        if intraday and multiday:
            return "BOTH"        # strongest — WSJ "SIZE REPEAT SWEEPER"
        if multiday:
            return "MULTI_DAY"   # institutional accumulation
        if intraday:
            return "INTRADAY"    # urgency play
        return "NONE"
```

**Session log addition to `RepetitionAccumulator.__init__()`:**

```python
self.session_log: Dict[str, List[date]] = {}
# key -> list of calendar dates with qualifying Gate-1 flow
```

**Session log update in `ingest_tick()`, after Gate-1 passes:**

```python
today = ev_ts.date()
dates = self.session_log.setdefault(key, [])
if not dates or dates[-1] != today:
    dates.append(today)

# Prune to rolling 5-day window
cutoff = today - timedelta(days=5)
self.session_log[key] = [d for d in dates if d >= cutoff]

# Attach to episode
ep.session_dates = self.session_log[key]
ep.repeat_type = _classify_repeat(ep)  # INTRADAY / MULTI_DAY / BOTH
```

**Intraday window expansion:**
Change default `window_minutes` from `30` → `390` (6.5 hours = full market session).
This is a breaking behavioral change. All callers and tests relying on the 30-minute default
must be audited before implementation begins (see Deliberation Point 2).

##### Part 2 — Alert Level Logic Based on `repeat_type`

Layer `repeat_type` on top of existing premium thresholds in `get_alert_level()`.
**Premium is a size filter. `repeat_type` is a conviction filter.** A $75K episode that is
`MULTI_DAY + SWEEP` must rank higher than a $500K intraday-only episode without sweeps.

| `repeat_type` | Sessions | Sweep Present | Alert Level | Signal Language |
|---|---|---|---|---|
| INTRADAY | 1 day, ≥3 events | No | WATCH | Raw P/C ratio post |
| INTRADAY | 1 day, ≥3 events | Yes | ALERT | `BULL FLOW DETECTED` |
| INTRADAY | 1 day, accelerating | Yes | STRONG_SIGNAL | `SIZE SWEEPER DETECTED` |
| MULTI_DAY | 2 days | No | ALERT | Ticker + P/C day 2 observation |
| MULTI_DAY | 2 days | Yes | STRONG_SIGNAL | `REPEAT BULL FLOW` |
| MULTI_DAY | 3+ days | Yes | CONVICTION | `REPEAT SWEEPER BUYING` |
| BOTH | Same day + prior session | Yes | CONVICTION | `SIZE REPEAT SWEEPER BUYING` |

##### Part 3 — Composite Score Reweight: Conviction Over Size

The composite score must answer: **"How much institutional repeat conviction is behind this
flow?"** — not "How much total dollar premium printed?"

**Current vs. Proposed Weight Split:**

| Component | Current Weight | Proposed Weight | Rationale |
|---|---|---|---|
| `repeat_conviction` *(new)* | 0% | **40%** | `BOTH > MULTI_DAY > INTRADAY` — primary signal |
| `flow_score` (premium + accel) | 55% | **25%** | Still matters, not dominant |
| `pc_ratio_skew` *(new)* | 0% | **20%** | 10:1+ ratio is a hard institutional tell |
| `volume_premium_factor` | 20% | **10%** | Useful but secondary |
| `premium_tier_score` | 15% | **5%** | Size confirms but does not drive |
| `sector_score` | 10% | **0%** | Still unimplemented — leave at 0 |
| **TOTAL** | 100% | **100%** | |

**`compute_repeat_conviction()` (proposed):**

```python
def compute_repeat_conviction(ep: RepeatEpisode) -> float:
    if ep.repeat_type == "BOTH":
        return 1.0
    if ep.repeat_type == "MULTI_DAY":
        days = len(set(ep.session_dates))
        return min(0.90, 0.60 + (days - 2) * 0.15)  # 2=0.60, 3=0.75, 4+=0.90
    if ep.repeat_type == "INTRADAY":
        return 0.50 if ep.is_accelerating else 0.30
    return 0.0
```

**`compute_pc_ratio_skew()` (proposed):**

```python
def compute_pc_ratio_skew(ep: RepeatEpisode) -> float:
    """
    Scores directional skew of P/C ratio.
    >= 20:1 -> 1.00  (extreme institutional skew)
    >= 10:1 -> 0.85  (strong — repeat sweeper territory)
    >= 5:1  -> 0.60  (moderate)
    >= 3:1  -> 0.35  (mild)
    <  3:1  -> 0.10  (no meaningful conviction)
    """
    ratio = ep.intraday_pc_ratio
    if ratio >= 20: return 1.00
    if ratio >= 10: return 0.85
    if ratio >= 5:  return 0.60
    if ratio >= 3:  return 0.35
    return 0.10
```

##### Part 4 — Three-Tier Output Classification

| Composite Score | Output Tier | Action | Equivalent Pattern |
|---|---|---|---|
| ≥ 0.75 | **SIGNAL** | Surface immediately | `REPEAT SWEEPER BUYING` |
| 0.50 – 0.74 | **WATCH** | Log, monitor for second session | Raw `Put/Call: 11k/116k` |
| < 0.50 | **NOISE** | Do not surface | Macro context posts |

#### Files Affected

- `backend/signals/repetition_accumulator.py` — add `session_log`, `repeat_type` classification, `RepeatEpisode` dataclass, intraday window expansion to 390 minutes
- `backend/signals/composite_signal_engine.py` — add `compute_repeat_conviction()`, `compute_pc_ratio_skew()`, reweight formula, update `build_composite()`, three-tier output classification
- `backend/tests/test_repetition_accumulator.py` — new tests for cross-session logic, session pruning, `repeat_type` classification
- `backend/tests/test_composite_signal_engine.py` — update all tests for new weight split; new parametrized tests for `repeat_conviction` scoring

#### Deliberation Points

> ⚠️ All deliberation points are architectural concerns about the formula's general behavior.
> Where specific examples appear, they are illustrative only.

1. **(Senior Software Director)** `repeat_conviction` at 40% dominates the composite score. A `BOTH`-type episode scores 1.0 regardless of premium size, which can produce composite > 0.75 SIGNAL even on a small-cap with $30K total premium if the P/C ratio is also skewed. Is this correct production behavior? Or should `repeat_conviction` weight only apply if `total_premium >= T3_floor` for the ticker's tier?

2. **(Senior Software Director + Platform Backend Engineer)** The proposed `window_minutes` default change from 30 → 390 is a breaking behavioral change. All existing callers relying on the 30-minute default will now accumulate across an entire session. Audit required: (a) which callers pass `window_minutes` explicitly vs. rely on default, (b) whether the 390-minute intraday window interacts correctly with the multi-day `session_log` at session boundaries — events from the prior session's window tail must not bleed into the next day's intraday count.

3. **(Platform Backend Engineer)** `session_log` is a dict keyed by episode key, appended on every Gate-1 pass. Under concurrent 64-worker execution, two workers can attempt `setdefault` + `append` on the same key simultaneously. The `_tier_map_lock` pattern from S4 (Finding 2) applies here. A `threading.Lock` on `session_log` writes is required. Confirm locking strategy before implementation.

4. **(Platform Backend Engineer)** `intraday_pc_ratio` on `RepeatEpisode` requires knowing total call and put volume for the underlying ticker within the session — not currently tracked at episode level. Confirm data source: (a) derive from the episode's own events only (this contract's trades — implementable immediately), or (b) fetch from a ticker-level P/C aggregator (full ticker flow across all contracts — requires new aggregation layer). The institutional P/C ratio (e.g., `$NOK Put/Call: 11k/116k`) is ticker-wide, not contract-specific. Panel must decide scope.

5. **(Senior Software Director)** This story's proposed weight split (`repeat_conviction=40%, flow_score=25%, pc_ratio=20%, vwp=10%, prem_tier=5%`) conflicts with S9's weight split (`flow_score=55%, vwp=20%, prem_tier=15%, otm_factor=5%, sector=5%`). **These cannot both be correct simultaneously.** Panel must decide: (a) does S12 supersede S9's weight split entirely, (b) does S9 land first with tiered ceiling fixes and S12 reweights on top, or (c) does S9 fix the formula shape and S12 adds new components without touching S9 weights? Sequencing between S9 and S12 must be resolved before either begins implementation.

6. **(QA Lead)** Regression test matrix must cover: (a) `repeat_type=NONE` → composite < 0.50 (noise) for all tier levels, (b) `INTRADAY` without sweep → WATCH band, (c) `INTRADAY` with acceleration + sweep → STRONG_SIGNAL, (d) `MULTI_DAY` 2 sessions + sweep → STRONG_SIGNAL, (e) `MULTI_DAY` 3+ sessions + sweep → CONVICTION, (f) `BOTH` → CONVICTION floor regardless of premium tier, (g) session pruning: events older than 5 calendar days dropped from `session_dates`, (h) session boundary: event at 11:59 PM Day 1 and 12:01 AM Day 2 (UTC) register as two distinct `session_dates`, (i) concurrent `session_log` write safety under 64 workers.

7. **(QA Lead)** `COMPOSITE_SCORE_CEILING` constant interaction: with `repeat_conviction=40%` at `1.0` and `pc_ratio_skew=20%` at `1.0`, the maximum achievable composite before `flow_score` and `vwp_factor` is already `0.60`. A CONVICTION episode can realistically hit `0.85+`. Confirm whether `COMPOSITE_SCORE_CEILING=0.90` (set in S6) should be updated to `1.0` or retained as a soft cap.

#### Acceptance Criteria

- [ ] `RepeatEpisode` dataclass defined with `intraday_events`, `intraday_sweep_count`, `intraday_pc_ratio`, `session_dates`, `per_session_pc_ratios`, `per_session_had_sweep`, and `repeat_type` computed property
- [ ] `RepetitionAccumulator.__init__()` has `session_log: Dict[str, List[date]]`
- [ ] `session_log` updated in `ingest_tick()` after Gate-1 pass; pruned to 5-day rolling window
- [ ] `ep.session_dates` and `ep.repeat_type` attached to episode on every qualifying tick
- [ ] `_classify_repeat()` function correctly returns `BOTH`, `MULTI_DAY`, `INTRADAY`, or `NONE`
- [ ] `intraday_events` window expanded to 390 minutes (or deliberated alternative)
- [ ] All callers of `RepetitionAccumulator` audited for `window_minutes` default reliance
- [ ] `session_log` writes protected by threading lock (analogous to `_tier_map_lock`)
- [ ] `compute_repeat_conviction()` implemented in `composite_signal_engine.py`
- [ ] `compute_pc_ratio_skew()` implemented in `composite_signal_engine.py`
- [ ] `build_composite()` uses new weight split: `repeat_conviction=40%, flow_score=25%, pc_ratio=20%, vwp=10%, prem_tier=5%`
- [ ] Three-tier output classification added: SIGNAL (≥0.75), WATCH (0.50–0.74), NOISE (<0.50)
- [ ] Alert level in `get_alert_level()` uses `repeat_type` as primary gate, premium as amplifier
- [ ] MULTI_DAY + SWEEP + 3 sessions → CONVICTION regardless of premium size
- [ ] INTRADAY only, no sweep → WATCH
- [ ] All existing `test_repetition_accumulator.py` tests pass with 390-min window change (or updated)
- [ ] All existing `test_composite_signal_engine.py` tests updated to new weight split
- [ ] New parametrized test: `test_repeat_type_classification[NONE/INTRADAY/MULTI_DAY/BOTH]`
- [ ] New test: `test_session_log_pruning` — events older than 5 days do not count toward `session_dates`
- [ ] New test: `test_composite_signal_tiers` — SIGNAL, WATCH, NOISE boundaries at 0.75 and 0.50
- [ ] New test: `test_concurrent_session_log_write` — 64 concurrent workers, no race on `session_log`
- [ ] `COMPOSITE_SCORE_CEILING` constant updated or documented per panel deliberation
- [ ] `docs/cipher_apex_story_and_sprint_plan.md` updated with this story definition

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
22. ⚪  S9            — Tier-relative flow_score normalization + dead-weight redistribution  ← IMMEDIATE FIX
23. ⏳  S10           — underlying_price population + OTM factor activation  ← BLOCKED on S9
24. ⏳  S11           — Full composite activation (backtest + sector)  ← BLOCKED on S9 + S10 + S6-POST-1 + S8
───────────────────────────────────────────────────────────────────────────────────

── SPRINT 5 — REPEAT CONVICTION ENGINE ─────────────────────────────────────────────────────
25. 🔴  S12           — Dual-window repeat conviction engine: cross-session accumulation + composite reweight  ← IMMEDIATE FIX / deliberation required (#56)
         ↳ NOTE: Weight split conflict with S9 must be resolved before either S9 or S12 begins implementation.
───────────────────────────────────────────────────────────────────────────────────

── FUTURE SPRINT ───────────────────────────────────────────────────────────────────────────
26. ⏳  S8            — Real backtest score from flow_events (requires signal_outcomes architecture)
───────────────────────────────────────────────────────────────────────────────────

── PARALLEL / ANYTIME ─────────────────────────────────────────────────────────────────
27. 🟢  ING-1         — Ingestion rewrite + delta chain fetch (#6)
28. 🟢  C8            — Decouple persist/signal tier (#2)
29. ⚪  #22           — Hoist get_registry import
30. ⚪  #28           — Fix misleading flush loop test
31. ⚪  #41           — _flush_loop orphaned flush tasks (cancel-on-shutdown or document)
32. ⚪  #42           — _get_tier_map double-guard redundancy (clean up or document)
33. ⚪  #53           — detect_ladder() deterministic group selection when multiple groups qualify
───────────────────────────────────────────────────────────────────────────────────
```

---

## Quick Reference

- **"What is next?"** → S12 (IMMEDIATE FIX — dual-window repeat conviction engine). Panel deliberation required before either begins. **Weight split conflict between S9 and S12 must be resolved first — see S12 Deliberation Point 5.** S9 might get closed
- **"What unblocks S6-POST-1?"** → (1) Decide `sector_score` normalization function — document in spec. (2) Confirm or add `RepetitionAccumulator.get_all_active_episodes()`. See issue [#55](https://github.com/bhaveshhpatel/cipher/issues/55).
- **"What unblocks S7?"** → Review `stream_worker.py` to confirm whether `_process_trade()` runs sequentially or via task scheduling.
- **"What unblocks S10?"** → S9 merged.
- **"What unblocks S11?"** → S9 + S10 + S6-POST-1 + S8 all merged.
- **"What is remaining?"** → Every row not marked ✅ — steps 20 through 33.
- **"What is the full plan?"** → Read [`docs/cipher_apex_story_and_sprint_plan.md`](docs/cipher_apex_story_and_sprint_plan.md) for story definitions, then this file for current status.
- **After every merge** → Mark row ✅, update version below.
- **After every panel review** → File issues for all findings, add rows to this file before merging.
- **Workflow rule** → Branch + PR always. Never push directly to `main`.

---

*Last updated: 2026-05-02 — Sprint 5 added: S12 (IMMEDIATE FIX — dual-window repeat conviction engine, cross-session accumulation, composite score reweight from size-ranking to conviction-ranking). GitHub Issue [#56](https://github.com/bhaveshhpatel/cipher/issues/56). Panel deliberation required before S12 implementation begins (Senior Software Director + Platform Backend Engineer + QA Lead). Critical: weight split conflict between S9 and S12 must be resolved before either story begins implementation — see S12 Deliberation Point 5. S9 deliberation points expanded 2026-05-01 (GLXY single-print analysis, INSTITUTIONAL ceiling review, Vol/OI flow_quality_factor, contract-size blind spot). All deliberation points are general formula concerns — specific trade examples are illustrative only.*
*Version: 5.0*
