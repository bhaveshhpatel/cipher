# Cipher Apex QA Path Coverage Specification

## Purpose
This document is the Lead QA path-exhaustion specification for the Cipher Apex layered architecture. It is designed to do two things: first, define the smallest practical but still exhaustive set of trade and episode scenarios required to cover all materially distinct runtime paths in the current architecture; second, serve as the living source of truth for post-implementation integration tests, scenario replay tests, and end-to-end signal validation.

This is not a unit-test inventory. It is a path-coverage document. The goal is to ensure that every meaningful branch, gate, accumulator outcome, ladder outcome, and composite publication outcome is exercised by at least one deliberately constructed scenario.

**Last reconciled against codebase:** 2026-05-01. The test suite has grown significantly since the original 28-scenario specification. All scenarios and branch families below reflect the actual implementation as verified from the `backend/tests/` directory on `main`.

---

## QA Design Standard
A weak QA plan would list many trades but fail to prove path coverage. That is not sufficient here. The standard for this document is:

- Every scenario must represent a materially unique runtime path.
- Parameter variations that do not change the architecture path are not counted as unique paths.
- Both rejection paths and success paths must be covered.
- Where runtime behavior depends on an episode rather than a single trade, the scenario granularity must be an episode, not a trade.
- The same scenario may satisfy multiple branch obligations, but each branch must be explicitly mapped.
- The output of this document must be reusable as a post-implementation replay suite.

---

## Runtime Model Under Test
The runtime sequence under test is:

1. Layer 0 — Symbol Registry readiness and enrichment context.
2. Layer 1 — Stream ingestion entry into `_process_trade()` / `_process_tick()`.
3. Layer 2 — Parser and classifier.
4. Layer 3 — Deduplication and sweep upgrade.
5. Fan-out — persistence path and signal path diverge.
6. Apex L1 — Signal gate.
7. Apex L2 — Dual-window accumulator.
8. Apex L4 — Ladder detector.
9. Apex L3 — Composite scorer.
10. Apex L5 — Broadcast and persistence payload emission.
11. Apex S1 — Threshold Reconciliation (OI/premium/volume breach detection).
12. Apex S2 — Tier map refresh + stream worker tick processing.

The blocked future swarm path Apex L6 is excluded from this document because it is not in active runtime scope. The swarm engine (`test_swarm_engine.py`, `test_apex_s0_swarm_cleanup.py`) is present in the test suite as a cleanup/lifecycle harness only.

---

## Branch Inventory

The architecture exposes more than 30 distinct branch families once the S1 threshold reconciliation layer and the S2 tier-map/tick-processing layer are included. The inventory below reflects the implemented tests as source of truth.

### Layer 2 — Parser and Classifier
- Fill resolves to zero and event is rejected.
- Size resolves to zero and event is rejected.
- Quote is synthetic.
- Quote is real NBBO.
- Bid/ask classification resolves to ABOVE_ASK.
- Bid/ask classification resolves to AT_ASK.
- Bid/ask classification resolves to MID.
- Bid/ask classification resolves to AT_BID.
- Bid/ask classification resolves to BELOW_BID.
- Contract type is CALL.
- Contract type is PUT.
- Trade type resolves to SWEEP.
- Trade type resolves to BLOCK.
- Trade type resolves to SPLIT.
- Trade type resolves to SINGLE.
- Golden classification resolves to GOLDEN_SWEEP.
- Golden classification resolves to GOLDEN_BLOCK.
- Golden classification resolves to NONE.
- Registry is ready and enrichment occurs.
- Registry is not ready and fallback fields remain in force.
- strong_sentiment resolves to True.
- strong_sentiment resolves to False.

### Layer 3 — Deduplication
- Clean pass through dedup.
- Duplicate drop.
- Sweep upgrade from exchange fan-out.
- No sweep upgrade.
- Dedup TTL expiry allows reprocessing (covered by `test_dedup_clock_c020.py`).
- Dedup key collision on rounded fill within TTL.

### Apex L1 — Signal Gate
- Reject for spread above 50%.
- Reject for synthetic quote below institutional-quality floor.
- Reject for premium below tier-and-trade-type floor.
- Pass signal gate.

### Apex L2 — Accumulator
- DTE bucket 0–7.
- DTE bucket 8–30.
- DTE bucket 31–90.
- DTE bucket 91+.
- underlying_price available.
- underlying_price missing, fall back to standard floor without OTM classification.
- ATM path 0–2%.
- Standard OTM path 2–12%.
- Deep OTM path >12% with 1.5× multiplier.
- Sweep bypass fires for a single qualifying event.
- Sweep bypass does not fire because the episode has 2 events.
- Episode accumulates but does not yet qualify.
- Episode qualifies and emits.
- dominant_direction resolves to REPEAT_BUY.
- dominant_direction resolves to REPEAT_SELL.
- Alert level WATCH.
- Alert level ALERT.
- Alert level STRONG_SIGNAL.
- Alert level CONVICTION.
- Concurrent episode accumulation under async lock (covered by `test_accumulator_concurrency.py`).

### Apex L4 — Ladder Detector
- No ladder.
- Ladder fires for 3+ strikes, same ticker, same expiry.
- Cross-expiry guard prevents false ladder.

### Apex L3 — Composite Scorer
- strong_sentiment full-score path.
- weak sentiment 0.80× discount path.
- sector_score inactive, pre-ladder ceiling path.
- sector_score active, ladder-enhanced path.
- volume > OI score boost path.
- no volume > OI boost.
- influence tier RETAIL.
- influence tier LARGE.
- influence tier INSTITUTIONAL.
- influence tier WHALE.

### Apex L5 — Broadcast
- Composite payload includes `composite_score_ceiling`.
- Composite payload omits `composite_score_ceiling` because ladder context is active.
- Persistence path continues independently even when signal path rejects.
- Persistence decoupled from signal emission (covered by `test_persist_decouple_c008.py`).
- Persistence gate controls write-through independently (covered by `test_persist_gate_c002.py`).
- Signal cooldown prevents re-emission within window (covered by `test_signal_cooldown_c007.py`).

### Apex S1 — Threshold Reconciliation (`services/threshold_reconciliation.py`)
*New since original spec. Fully covered by `test_apex_s1_threshold_reconciliation.py`.*

- BreachType enum: OI_SPIKE, PREMIUM_FLOOD, VOLUME_SURGE, OI_COLLAPSE (all four values present and distinct).
- `_epoch_minute` quantises timestamp to 60-second bucket.
- `_breach_key` produces a (symbol, breach_type_value, epoch_minute) tuple.
- `_metrics_complete` returns False for None oi_delta, premium_usd, or volume_ratio.
- `_metrics_complete` returns False for NaN oi_delta, premium_usd, or volume_ratio.
- `_evaluate` emits no breaches when all values are below thresholds.
- `_evaluate` emits OI_SPIKE at exact threshold boundary.
- `_evaluate` emits OI_COLLAPSE at exact lower boundary.
- `_evaluate` emits PREMIUM_FLOOD when premium_usd meets threshold.
- `_evaluate` emits VOLUME_SURGE when volume_ratio meets threshold.
- `_evaluate` emits multiple breach types simultaneously when all thresholds exceeded.
- `get_thresholds_for_tier` returns a copy (not the live dict) for known tiers T1/T2/T3.
- `get_thresholds_for_tier` falls back to T3 for unknown tier strings.
- `reconcile` skips incomplete (None/NaN) metrics and increments `skipped` counter.
- `reconcile` increments `checked` and populates breaches for clean symbol.
- `reconcile` deduplicates same symbol + breach type within the same epoch-minute.
- `reconcile` allows re-fire for the same symbol + breach type in a different epoch-minute.
- `reconcile` calls `emit_fn` for each breach when provided.
- `reconcile` does not crash when `emit_fn=None`.
- `reconcile` swallows exceptions from a crashing `emit_fn` and continues processing remaining symbols.
- `reconcile` falls back to T3 when symbol is absent from the tier map.
- `reconcile` populates `elapsed_ms`.
- `reconcile` is serialised under asyncio lock (concurrent calls tested via `asyncio.gather`).
- `_maybe_evict` evicts entries when `_seen` exceeds `_seen_cap`.
- Module-level `get_reconciler` returns a singleton across multiple calls.
- Module-level `reconcile()` wrapper delegates to the singleton instance.
- `reset_dedup_cache` clears `_seen` and allows immediate re-fire.

### Apex S2 — Tier Map Refresh and Tick Processing (`services/stream_worker.py`)
*New since original spec. Fully covered by `test_apex_s2_tier_coverage.py` and `test_apex_s2_tier_wiring.py`.*

**`_refresh_tier_map` branch coverage:**
- Happy path: registry ready, `assign_tiers` returns a valid map — cache is populated, timestamp updated, `_tier_map_refresh_task` is None or done post-return.
- `get_registry()` returns None — early return, cache and timestamp unchanged.
- `registry.is_ready()` returns False — early return, cache and timestamp unchanged.
- `assign_tiers` raises RuntimeError — exception caught, WARNING logged containing exception context, cache unchanged (non-fatal).
- `assign_tiers` returns integer tiers (1/2/3) — cache must store string values "T1"/"T2"/"T3".
- `assign_tiers` returns empty dict `{}` — cache is set to `{}`, timestamp IS updated (edge case: cold-start tier engine). Downstream symbols fall back to T3 until next refresh.

**`_process_tick` registry lookup branch coverage:**
- `get_registry()` returns None — avg_volume falls back to 1.0, tick still lands in `_pending` with `volume_ratio = volume / 1.0`.
- Registry present but symbol absent from `_avg_volume_by_ticker` — `.get()` returns 0, fallback to 1.0, tick processed.
- `get_registry()` raises — exception swallowed, tick still processed with avg_volume=1.0.
- Inner `reg._avg_volume_by_ticker.get()` raises RuntimeError — exception swallowed, fallback to avg_volume=1.0, tick still lands in `_pending`.

**Test isolation contract:**
- `reset_tier_map_globals` autouse fixture saves and restores `_tier_map_cache`, `_tier_map_ts`, `_tier_map_refresh_task`, and `_tier_map_refresh_in_progress` around every test to prevent session-level pollution.

---

## Coverage Strategy
A brute-force combinatorial matrix would be useless. The correct QA approach is a deliberate scenario suite where each scenario is chosen because it forces at least one branch outcome not already covered by previous scenarios.

The suite uses 34 core path scenarios for the original Apex signal pipeline (QA-01 through QA-34), supplemented by the S1 threshold reconciliation scenarios (QA-S1-01 through QA-S1-22) and the S2 tier/tick scenarios (QA-S2-01 through QA-S2-10).

---

## Scenario Catalog — Apex Signal Pipeline

| ID | Granularity | Core intent | Terminal outcome |
|---|---|---|---|
| QA-01 | Trade | Zero fill parser guard | Rejected at Layer 2 |
| QA-02 | Trade | Zero size parser guard | Rejected at Layer 2 |
| QA-03 | Trade | Duplicate event | Dropped at Layer 3 |
| QA-04 | Trade | Sweep upgrade by multi-exchange dedup | Continues as upgraded SWEEP |
| QA-05 | Trade | Synthetic low-quality quote | Rejected at Apex L1 |
| QA-06 | Trade | Spread too wide | Rejected at Apex L1 |
| QA-07 | Trade | Premium below T1 SWEEP floor | Rejected at Apex L1 |
| QA-08 | Trade | Premium below T2/T3 SINGLE floor | Rejected at Apex L1 |
| QA-09 | Episode | Valid event accumulates but does not yet qualify | Held in Apex L2 |
| QA-10 | Episode | Single-event sweep bypass | Emits STRONG_SIGNAL, REPEAT_BUY |
| QA-11 | Episode | Sweep bypass negative at len(ep.events)=2 | Held until min_sweeps met |
| QA-12 | Episode | BUY PUT bearish BLOCK deep OTM | Emits REPEAT_SELL |
| QA-13 | Episode | SELL CALL bearish SPLIT standard OTM | Emits REPEAT_SELL |
| QA-14 | Episode | BUY CALL bullish LEAPS ATM | Emits REPEAT_BUY with ceiling |
| QA-15 | Trade | MID print weak sentiment path | Weak-score composite path |
| QA-16 | Trade | Synthetic but institutional-quality pass | Weak-score signal path |
| QA-17 | Episode | Deep OTM multiplier pass at 91+ DTE | Emits after multiplier check |
| QA-18 | Episode | underlying_price missing fallback | Emits using standard floor |
| QA-19 | Trade | Registry not ready | Fallback parse path |
| QA-20 | Episode | volume > OI boost path | Composite boost applied |
| QA-21 | Episode | WATCH alert path | Emits WATCH |
| QA-22 | Episode | ALERT alert path | Emits ALERT |
| QA-23 | Episode | STRONG_SIGNAL path without bypass | Emits STRONG_SIGNAL |
| QA-24 | Episode | CONVICTION path | Emits CONVICTION |
| QA-25 | Episode set | Ladder positive, same expiry, 3 strikes | Ladder fires |
| QA-26 | Episode set | Ladder negative, different expiries | No ladder |
| QA-27 | Episode | RETAIL influence tier | Composite emits RETAIL |
| QA-28 | Episode | WHALE influence tier with ladder active | Composite emits WHALE without ceiling |
| QA-29 | Trade | Dedup TTL expiry allows re-entry | Event accepted after TTL window |
| QA-30 | Trade | Dedup key collision on rounded fill | Event dropped within TTL |
| QA-31 | Trade | Persist-gate decoupled from signal | Persistence write fires even on signal reject |
| QA-32 | Trade | Signal cooldown suppresses re-emission | No duplicate broadcast within cooldown window |
| QA-33 | Episode | Concurrent episode accumulation under lock | No race condition on shared episode state |
| QA-34 | Trade | Persistence decoupled from Apex fanout | Persistence path is independent of signal path |

---

## Scenario Catalog — Apex S1 Threshold Reconciliation

| ID | Component | Core intent | Terminal outcome |
|---|---|---|---|
| QA-S1-01 | BreachType enum | All four values are distinct strings | Enum validated |
| QA-S1-02 | `_epoch_minute` | Timestamps within same 60-s window bucket equally | Helper validated |
| QA-S1-03 | `_epoch_minute` | Timestamps across minute boundary differ | Helper validated |
| QA-S1-04 | `_breach_key` | Key structure is (symbol, type_value, epoch_minute) | Helper validated |
| QA-S1-05 | `_metrics_complete` | None in any field returns False | Guard validated |
| QA-S1-06 | `_metrics_complete` | NaN in any field returns False | NaN guard validated |
| QA-S1-07 | `_evaluate` | No breach below all thresholds | Clean path |
| QA-S1-08 | `_evaluate` | OI_SPIKE at exact boundary | Boundary inclusive |
| QA-S1-09 | `_evaluate` | OI_COLLAPSE at exact lower boundary | Boundary inclusive |
| QA-S1-10 | `_evaluate` | PREMIUM_FLOOD when premium_usd meets threshold | Flood detected |
| QA-S1-11 | `_evaluate` | VOLUME_SURGE when volume_ratio meets threshold | Surge detected |
| QA-S1-12 | `_evaluate` | Multiple breaches simultaneously | Multi-breach emitted |
| QA-S1-13 | `reconcile` | NaN metric skips and increments `skipped` | Skipped counter correct |
| QA-S1-14 | `reconcile` | Dedup within same epoch-minute suppresses re-fire | Dedup active |
| QA-S1-15 | `reconcile` | Re-fire allowed in different epoch-minute | Dedup per-minute |
| QA-S1-16 | `reconcile` | `emit_fn` called for each breach | Callback wired |
| QA-S1-17 | `reconcile` | Crashing `emit_fn` does not halt remaining symbols | Resilience validated |
| QA-S1-18 | `reconcile` | Unknown symbol falls back to T3 tier | Tier fallback |
| QA-S1-19 | `reconcile` | Concurrent calls serialised under asyncio lock | Lock serialisation |
| QA-S1-20 | `_maybe_evict` | Evicts when `_seen` exceeds `_seen_cap` | Memory cap enforced |
| QA-S1-21 | `get_reconciler` | Returns same singleton across multiple calls | Singleton pattern |
| QA-S1-22 | `reset_dedup_cache` | Clears `_seen` and allows immediate re-fire | Reset path |

---

## Scenario Catalog — Apex S2 Tier Map and Tick Processing

| ID | Component | Core intent | Terminal outcome |
|---|---|---|---|
| QA-S2-01 | `_refresh_tier_map` | Happy path: cache populated, timestamp updated | Cache rebuilt correctly |
| QA-S2-02 | `_refresh_tier_map` | `get_registry()` returns None — early return | Cache and ts unchanged |
| QA-S2-03 | `_refresh_tier_map` | `registry.is_ready()` False — early return | Cache and ts unchanged |
| QA-S2-04 | `_refresh_tier_map` | `assign_tiers` raises — WARNING logged, cache unchanged | Non-fatal exception |
| QA-S2-05 | `_refresh_tier_map` | Integer tiers (1/2/3) converted to strings ("T1"/"T2"/"T3") | Type coercion |
| QA-S2-06 | `_refresh_tier_map` | `assign_tiers` returns `{}` — cache emptied, ts updated | Cold-start edge case |
| QA-S2-07 | `_process_tick` | `get_registry()` returns None — avg_volume fallback to 1.0 | Tick lands in `_pending` |
| QA-S2-08 | `_process_tick` | Symbol absent from `_avg_volume_by_ticker` — fallback to 1.0 | Tick processed |
| QA-S2-09 | `_process_tick` | `get_registry()` raises — exception swallowed, tick processed | Resilience validated |
| QA-S2-10 | `_process_tick` | Inner `.get()` on `_avg_volume_by_ticker` raises — fallback, tick lands in `_pending` | Inner exception path |

---

## Detailed Scenario Specifications

### QA-01 — Zero Fill Parser Guard
**Granularity:** Single trade

#### Input
- `last=None`
- `price=None`
- `bid=0`
- `ask=0`
- `size=25`

#### Journey
- Layer 1: Raw tick enters `_process_trade()`.
- Layer 2: Fill resolution computes `0.0`.
- Layer 2: Explicit `fill == 0` guard returns `None`.
- Layer 3 onward: Not reached.
- Persistence path: Not reached.
- Signal path: Not reached.

#### Signal decision
- No signal. No persistence write. Correct outcome is hard parser rejection.

---

### QA-02 — Zero Size Parser Guard
**Granularity:** Single trade

#### Input
- Valid non-zero fill
- `size=0`

#### Journey
- Layer 1: Tick enters.
- Layer 2: Fill resolves correctly.
- Layer 2: `size == 0` guard rejects event.
- Remaining layers are not reached.

#### Signal decision
- No signal. No persistence write.

---

### QA-03 — Duplicate Drop
**Granularity:** Single trade, assuming an identical clean event was already seen inside TTL

#### Input
- Same `occ_symbol`, `size`, and rounded fill as prior accepted event
- Arrives within dedup TTL

#### Journey
- Layer 1: Tick enters.
- Layer 2: Parses normally.
- Layer 3: Dedup cache hit identifies duplicate.
- Layer 3: Event is dropped.
- Persistence path: No second write.
- Signal path: No second evaluation.

#### Signal decision
- No new signal. Correct behavior is suppression of duplicate market-center reports.

---

### QA-04 — Sweep Upgrade Path
**Granularity:** Single trade as part of exchange fan-out cluster

#### Input
- Same execution observed across 3+ exchanges within 8 seconds
- Trade otherwise parses as non-SWEEP on raw event shape

#### Journey
- Layer 1: First exchange tick enters, parses as BLOCK or SINGLE.
- Layer 3: Subsequent exchange ticks are recognised as same execution; upgrade fires.
- Layer 3: trade_type is promoted to SWEEP.
- Apex L1: Re-evaluated against SWEEP premium floor.

#### Signal decision
- Continues as SWEEP. Upgrade path confirmed by `test_sweep_upgrade_c003.py`.

---

### QA-05 — Synthetic Low-Quality Quote Rejection
**Granularity:** Single trade

#### Input
- `is_synthetic=True`
- Quote quality below institutional-quality floor

#### Journey
- Layer 2: Parses. Synthetic flag set.
- Apex L1: Synthetic quality check fails.
- Signal path: Rejected.

#### Signal decision
- No signal. Persistence path may continue independently.

---

### QA-06 — Wide Spread Rejection
**Granularity:** Single trade

#### Input
- Spread > 50% of mid price

#### Journey
- Layer 2: Parses.
- Apex L1: Spread gate fires. Event rejected.

#### Signal decision
- No signal.

---

### QA-07 — Premium Below T1 SWEEP Floor
**Granularity:** Single trade

#### Input
- trade_type = SWEEP
- Symbol tier = T1
- premium < T1 SWEEP minimum threshold

#### Journey
- Layer 2: Parses as SWEEP.
- Apex L1: Premium floor check for T1/SWEEP fails.
- Signal path: Rejected.

#### Signal decision
- No signal.

---

### QA-08 — Premium Below T2/T3 SINGLE Floor
**Granularity:** Single trade

#### Input
- trade_type = SINGLE
- Symbol tier = T2 or T3
- premium < SINGLE minimum threshold

#### Journey
- Layer 2: Parses as SINGLE.
- Apex L1: Premium floor check for SINGLE fails.
- Signal path: Rejected.

#### Signal decision
- No signal.

---

### QA-09 — Episode Accumulates But Does Not Qualify
**Granularity:** Episode

#### Input
- Qualifying trade(s) that do not yet meet episode qualification thresholds (min_sweeps, min_premium, or alert level not reached)

#### Journey
- Apex L1: Gate passes.
- Apex L2: Event added to episode state.
- Apex L2: Qualification check fails (insufficient events or premium).
- Apex L3/L4/L5: Not reached.

#### Signal decision
- Episode held. No broadcast.

---

### QA-10 — Single-Event Sweep Bypass
**Granularity:** Episode

#### Input
- Single SWEEP trade that exceeds the bypass premium threshold
- Episode has exactly 1 event

#### Journey
- Apex L2: Sweep bypass path activated.
- Apex L3: Composite scored.
- Apex L5: Broadcasts with STRONG_SIGNAL, REPEAT_BUY.

#### Signal decision
- Emits STRONG_SIGNAL, REPEAT_BUY.

---

### QA-11 — Sweep Bypass Blocked at 2 Events
**Granularity:** Episode

#### Input
- Episode has 2 events (len(ep.events) == 2)
- First event would have triggered bypass if alone

#### Journey
- Apex L2: Sweep bypass condition checks `len(ep.events) == 1` — fails because 2 events.
- Episode holds until regular qualification thresholds are met.

#### Signal decision
- No bypass. Episode held until `min_sweeps` reached.

---

### QA-12 — BUY PUT Bearish BLOCK Deep OTM
**Granularity:** Episode

#### Input
- trade_type = BLOCK
- contract_type = PUT
- order_side = BUY
- OTM > 12% (deep OTM)
- DTE: any tier

#### Journey
- Apex L2: Deep OTM multiplier (1.5×) applied. dominant_direction = REPEAT_SELL.
- Apex L3: Composite scored for bearish.
- Apex L5: Emits REPEAT_SELL.

#### Signal decision
- Emits REPEAT_SELL.

---

### QA-13 — SELL CALL Bearish SPLIT Standard OTM
**Granularity:** Episode

#### Input
- trade_type = SPLIT
- contract_type = CALL
- order_side = SELL
- OTM 2–12% (standard OTM)

#### Journey
- Apex L2: Standard OTM path. dominant_direction = REPEAT_SELL.
- Apex L3: Composite scored.
- Apex L5: Emits REPEAT_SELL.

#### Signal decision
- Emits REPEAT_SELL.

---

### QA-14 — BUY CALL Bullish LEAPS ATM with Ceiling
**Granularity:** Episode

#### Input
- trade_type = BLOCK or SWEEP
- contract_type = CALL
- order_side = BUY
- DTE 91+ (LEAPS bucket)
- OTM 0–2% (ATM)

#### Journey
- Apex L2: ATM path. DTE bucket 91+. dominant_direction = REPEAT_BUY.
- Apex L3: Ceiling applied (no active ladder context).
- Apex L5: Emits REPEAT_BUY with `composite_score_ceiling` in payload.

#### Signal decision
- Emits REPEAT_BUY with ceiling. Payload includes `composite_score_ceiling`.

---

### QA-15 — MID Print Weak Sentiment
**Granularity:** Trade leading to episode

#### Input
- bid/ask classification = MID
- strong_sentiment = False

#### Journey
- Layer 2: MID classification recorded. strong_sentiment = False.
- Apex L3: 0.80× discount applied to composite score.

#### Signal decision
- Composite score reduced by 20%. Weak sentiment path confirmed.

---

### QA-16 — Synthetic Institutional Quality Pass
**Granularity:** Trade

#### Input
- `is_synthetic=True`
- Quote quality at or above institutional-quality floor

#### Journey
- Layer 2: Synthetic flag set but quality is sufficient.
- Apex L1: Synthetic quality check passes (institutional threshold met).
- Continues to Apex L2 with weak-score flag.

#### Signal decision
- Signal proceeds with weak-score path (no full strong_sentiment boost).

---

### QA-17 — Deep OTM 91+ DTE Multiplier
**Granularity:** Episode

#### Input
- OTM > 12%
- DTE 91+

#### Journey
- Apex L2: Deep OTM (1.5× multiplier) applied. DTE bucket 91+.
- Premium floor at 91+ bucket checked against multiplied value.
- Episode qualifies and emits.

#### Signal decision
- Emits after deep OTM multiplier check.

---

### QA-18 — underlying_price Missing Fallback
**Granularity:** Episode

#### Input
- underlying_price not available in registry context

#### Journey
- Apex L2: OTM classification skipped (no underlying_price).
- Standard floor applied without ATM/OTM path.
- Episode qualifies and emits.

#### Signal decision
- Emits using standard floor. No OTM multiplier applied.

---

### QA-19 — Registry Not Ready Fallback Parse
**Granularity:** Trade

#### Input
- Symbol registry `is_ready()` returns False at parse time

#### Journey
- Layer 0: Registry not ready.
- Layer 2: Enrichment fields remain at fallback defaults (no sector, no underlying_price, no avg_volume).
- Parsing continues with fallback values.

#### Signal decision
- Fallback parse path followed. Downstream layers receive fallback enrichment.

---

### QA-20 — Volume > OI Score Boost
**Granularity:** Episode

#### Input
- Episode volume exceeds open interest for the contract

#### Journey
- Apex L2: Episode qualifies.
- Apex L3: volume > OI condition met; score boost applied.

#### Signal decision
- Composite score boosted. Boost verified in `test_composite_signal_engine.py`.

---

### QA-21 — WATCH Alert Level
**Granularity:** Episode

#### Input
- Episode accumulates sufficient premium to cross WATCH threshold but not ALERT

#### Journey
- Apex L2: alert_level = WATCH.
- Apex L3/L5: Broadcasts with WATCH.

#### Signal decision
- Emits WATCH.

---

### QA-22 — ALERT Alert Level
**Granularity:** Episode

#### Input
- Episode crosses ALERT premium/count threshold

#### Journey
- Apex L2: alert_level = ALERT.
- Apex L5: Broadcasts ALERT.

#### Signal decision
- Emits ALERT.

---

### QA-23 — STRONG_SIGNAL Without Bypass
**Granularity:** Episode

#### Input
- Episode reaches STRONG_SIGNAL threshold via regular accumulation (not single-event bypass)
- Minimum 2+ events in episode

#### Journey
- Apex L2: alert_level = STRONG_SIGNAL via multi-event path.
- Apex L5: Broadcasts STRONG_SIGNAL.

#### Signal decision
- Emits STRONG_SIGNAL. Distinct from QA-10 (bypass path).

---

### QA-24 — CONVICTION Alert Level
**Granularity:** Episode

#### Input
- Episode achieves maximum accumulation tier

#### Journey
- Apex L2: alert_level = CONVICTION.
- Apex L3: Full composite scoring.
- Apex L5: Broadcasts CONVICTION.

#### Signal decision
- Emits CONVICTION.

---

### QA-25 — Ladder Positive (Same Expiry, 3+ Strikes)
**Granularity:** Episode set

#### Input
- 3+ distinct strike episodes, same underlying ticker, same expiry date, all bullish direction

#### Journey
- Apex L4: Strike count for (ticker, expiry) reaches 3.
- Ladder detection fires.
- Apex L3: sector_score active, ladder-enhanced composite path (no ceiling applied).

#### Signal decision
- Ladder fires. Payload omits `composite_score_ceiling`.

---

### QA-26 — Ladder Negative (Cross-Expiry Guard)
**Granularity:** Episode set

#### Input
- 3+ episodes on same ticker but across different expiry dates

#### Journey
- Apex L4: Cross-expiry guard groups by (ticker, expiry) independently.
- No single expiry accumulates 3+ strikes.
- Ladder does not fire.

#### Signal decision
- No ladder. Payload includes `composite_score_ceiling` if applicable.

---

### QA-27 — RETAIL Influence Tier
**Granularity:** Episode

#### Input
- Symbol tier = T3 (RETAIL range)

#### Journey
- Apex L3: influence_tier = RETAIL applied in composite scoring.
- Apex L5: Payload includes influence_tier = RETAIL.

#### Signal decision
- Emits with RETAIL influence tier.

---

### QA-28 — WHALE Influence Tier with Active Ladder
**Granularity:** Episode

#### Input
- Symbol tier = T1, very high premium
- Ladder context active

#### Journey
- Apex L3: influence_tier = WHALE.
- Ladder context active → no ceiling applied.
- Apex L5: Payload includes influence_tier = WHALE, no ceiling field.

#### Signal decision
- Emits WHALE without `composite_score_ceiling`.

---

### QA-29 — Dedup TTL Expiry Allows Re-entry
**Granularity:** Trade

#### Input
- Identical tick seen after dedup TTL has expired
- Covered by `test_dedup_clock_c020.py`

#### Journey
- Layer 3: Dedup TTL window expired. Cache entry absent or evicted.
- Event passes dedup as new entry.
- Processing continues normally.

#### Signal decision
- Event accepted. Not a duplicate after TTL.

---

### QA-30 — Dedup Key Collision on Rounded Fill
**Granularity:** Trade

#### Input
- Two ticks with fills that differ by less than rounding precision (e.g. 1.499 vs 1.501 both round to 1.5)
- Same symbol, same size

#### Journey
- Layer 3: Dedup key collision. Second tick dropped.

#### Signal decision
- Second event suppressed. Correct market-center noise reduction behavior.

---

### QA-31 — Persistence Gate Decoupled from Signal
**Granularity:** Trade
*Covered by `test_persist_gate_c002.py`.*

#### Journey
- Signal path: Rejected at Apex L1 (premium below floor).
- Persistence path: Persistence gate evaluates independently and writes.

#### Signal decision
- No signal broadcast. Persistence write fires regardless.

---

### QA-32 — Signal Cooldown Suppresses Re-emission
**Granularity:** Trade / Episode
*Covered by `test_signal_cooldown_c007.py`.*

#### Journey
- First qualifying episode emits successfully.
- Second qualifying episode for same key arrives within cooldown window.
- Cooldown check suppresses broadcast.

#### Signal decision
- No duplicate broadcast within cooldown window.

---

### QA-33 — Concurrent Episode Accumulation Under Lock
**Granularity:** Episode (async)
*Covered by `test_accumulator_concurrency.py`.*

#### Journey
- Multiple coroutines attempt to write to the same episode concurrently.
- Async lock serialises writes.
- No partial state or race corruption.

#### Signal decision
- Episode state is consistent post-concurrent-write.

---

### QA-34 — Persistence Decoupled from Apex Fanout
**Granularity:** Trade
*Covered by `test_persist_decouple_c008.py`.*

#### Journey
- Fan-out fires both persistence path and signal path.
- Signal path failure (e.g. gate reject, exception) does not abort persistence path.

#### Signal decision
- Persistence write completes independently of signal path outcome.

---

## Additional Test Modules Not In Original Spec

The following test modules exist in `backend/tests/` and cover areas outside the original 28-scenario signal pipeline spec. They are catalogued here for completeness.

| Module | Scope |
|---|---|
| `test_4a_oi_pipeline.py` | OI data pipeline ingestion and normalization |
| `test_4a_tier_engine.py` | Tier engine assignment logic (T1/T2/T3 rules) |
| `test_6layer_regression.py` | Full 6-layer end-to-end regression suite |
| `test_activity_log.py` | Activity log write and read paths |
| `test_admin_demo_routes.py` / `test_admin_router.py` / `test_admin_router_coverage.py` | Admin API routes |
| `test_apex_s0_swarm_cleanup.py` | Swarm engine cleanup/lifecycle (Apex L6 — not active in signal runtime) |
| `test_async_bus.py` / `test_async_bus_coverage.py` | Internal async event bus publish/subscribe |
| `test_auth_cors_regression.py` / `test_auth_flow.py` / `test_auth_router.py` / `test_core_auth_security.py` | Auth and CORS regression |
| `test_be3_dict_tick.py` | Dict-format tick ingestion (BE3 schema) |
| `test_chain_store.py` / `test_chain_store_c1_c2.py` | Options chain store CRUD |
| `test_classifier.py` / `test_classifier_coverage.py` | Trade classifier (bid/ask zone) |
| `test_composite_signal_engine.py` / `test_composite_signal_engine_p3.py` / `test_composite_signal_extended.py` | Composite scorer unit and extended paths |
| `test_apex_s6_composite_overhaul.py` | Composite scorer overhaul regression |
| `test_dedup_cache.py` / `test_dedup_cache_coverage.py` / `test_dedup_clock_c020.py` / `test_dedup_coverage.py` / `test_dedup_edge_cases.py` | Dedup cache full coverage including clock-based TTL expiry |
| `test_demo_engine.py` / `test_demo_engine_coverage.py` | Demo/replay engine |
| `test_flow_endpoint.py` / `test_flow_episodes.py` / `test_flow_events.py` / `test_flow_store.py` / `test_flow_and_stats.py` | Flow store read/write, episodes API, events API |
| `test_h1_h3_h4_fixes.py` | Hotfix regression for H1/H3/H4 defects |
| `test_health_stream.py` | Health and stream lifecycle endpoints |
| `test_history_router.py` | Historical signals API |
| `test_ingestion_config.py` / `test_ingestion_config_rc3.py` | Ingestion config loading and validation |
| `test_main_app.py` | FastAPI app startup and route registration |
| `test_midcap_screener.py` | Mid-cap universe screener |
| `test_occ_parser.py` | OCC symbol parser |
| `test_options_flow_parser.py` | Full options flow parser |
| `test_order_side_classifier_coverage.py` | Order side classifier edge cases |
| `test_persist_decouple_c008.py` | Persistence decoupling (QA-34 impl) |
| `test_persist_gate_c002.py` | Persistence gate (QA-31 impl) |
| `test_registry_prewarm.py` | Symbol registry pre-warm |
| `test_repetition_engine.py` | Repetition / dominant direction engine |
| `test_signal_gate_coverage.py` | Signal gate branch coverage |
| `test_signal_store.py` / `test_signal_store_coverage.py` / `test_signal_store_r3.py` | Signal store CRUD and R3 schema |
| `test_simulation_and_ws.py` / `test_simulation_router.py` | Simulation engine and WebSocket |
| `test_smart_signals_router.py` | Smart signals REST API |
| `test_stream_hotpath_fixes.py` | Stream hotpath performance fixes |
| `test_stream_manager.py` / `test_stream_manager_r3.py` | Stream manager lifecycle |
| `test_stream_worker_b008.py` | Stream worker B008 fix regression |
| `test_swarm_engine.py` / `test_swarm_engine_coverage.py` | Swarm engine (Apex L6 stub coverage) |
| `test_sweep_upgrade_c003.py` | Sweep upgrade (QA-04 impl) |
| `test_symbol_registry_coverage.py` / `test_symbol_registry_zero_price_fallback.py` | Symbol registry coverage and zero-price fallback |
| `test_symbols_loader.py` | Symbol universe loader |
| `test_synthetic_quote_handling.py` | Synthetic quote handling |
| `test_tier_engine.py` / `test_tier_engine_c3.py` | Tier engine C3 regression |
| `test_trade_executor.py` | Trade executor |
| `test_tradier_client.py` / `test_tradier_client_coverage.py` / `test_tradier_stream.py` | Tradier API client and stream |
| `test_universe_screener.py` / `test_universe_screener_coverage.py` | Universe screener |
| `test_universe_store.py` / `test_universe_store_coverage.py` / `test_universe_store_rc1_rc2.py` | Universe store CRUD and RC1/RC2 schema |
| `test_ws_lifecycle.py` / `test_ws_router.py` | WebSocket lifecycle and routing |
| `integration/` | Integration test suite (separate directory) |

---

## Change Log

| Date | Change |
|---|---|
| Original | 28-scenario specification created from architecture design |
| 2026-05-01 | **Full reconciliation pass.** Added QA-29 through QA-34 (dedup TTL, rounded fill collision, persistence gate, signal cooldown, concurrent accumulation, persistence decoupled from fanout). Added Apex S1 Threshold Reconciliation branch family and 22 S1 scenarios (QA-S1-01 through QA-S1-22) from `test_apex_s1_threshold_reconciliation.py`. Added Apex S2 Tier Map and Tick Processing branch family and 10 S2 scenarios (QA-S2-01 through QA-S2-10) from `test_apex_s2_tier_coverage.py`. Added `_tier_map_refresh_in_progress` flag to S2 isolation contract. Added runtime model entries for Apex S1 and S2. Added full additional test module catalog. Removed claim that Apex L6 swarm path is "excluded from architecture" — clarified it exists as a cleanup/lifecycle harness only. Updated scenario count from 28 to 34 core + 22 S1 + 10 S2 = 66 total path obligations. |
