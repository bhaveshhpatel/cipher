# Cipher Apex QA Path Coverage Specification

## Purpose
This document is the Lead QA path-exhaustion specification for the Cipher Apex layered architecture. It is designed to do two things: first, define the smallest practical but still exhaustive set of trade and episode scenarios required to cover all materially distinct runtime paths in the current architecture; second, serve as the source document for post-implementation integration tests, scenario replay tests, and end-to-end signal validation.

This is not a unit-test inventory. It is a path-coverage document. The goal is to ensure that every meaningful branch, gate, accumulator outcome, ladder outcome, and composite publication outcome is exercised by at least one deliberately constructed scenario.

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
2. Layer 1 — Stream ingestion entry into `_process_trade()`.
3. Layer 2 — Parser and classifier.
4. Layer 3 — Deduplication and sweep upgrade.
5. Fan-out — persistence path and signal path diverge.
6. Apex L1 — Signal gate.
7. Apex L2 — Dual-window accumulator.
8. Apex L4 — Ladder detector.
9. Apex L3 — Composite scorer.
10. Apex L5 — Broadcast and persistence payload emission.

The blocked future swarm path Apex L6 is excluded from this document because it is not in active runtime scope.

---

## Branch Inventory
The architecture exposes more than 20 distinct paths. The confusion comes from mixing branch outcomes with parameter permutations. When reduced to materially different runtime behavior, the current architecture requires coverage of the following branch families.

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

That produces a path matrix far larger than 20 scenario obligations once deduplicated into actual runtime branches.

---

## Coverage Strategy
A brute-force combinatorial matrix would be useless. The correct QA approach is a deliberate scenario suite where each scenario is chosen because it forces at least one branch outcome not already covered by previous scenarios.

The suite below uses 28 scenarios. That is larger than the first rough estimate of 20 because the earlier estimate undercounted dedup behavior, cross-expiry ladder suppression, all four alert levels, all four influence tiers, and several parser-to-accumulator edge paths.

This 28-scenario set is the recommended minimum practical path-exhaustion suite for the architecture as currently specified.

---

## Scenario Catalog

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

---

## Detailed Scenario Specifications

## QA-01 — Zero Fill Parser Guard
**Granularity:** Single trade

### Input
- `last=None`
- `price=None`
- `bid=0`
- `ask=0`
- `size=25`

### Journey
- Layer 1: Raw tick enters `_process_trade()`.
- Layer 2: Fill resolution computes `0.0`.
- Layer 2: Explicit `fill == 0` guard returns `None`.
- Layer 3 onward: Not reached.
- Persistence path: Not reached.
- Signal path: Not reached.

### Signal decision
- No signal.
- No persistence write.
- Correct outcome is hard parser rejection.

---

## QA-02 — Zero Size Parser Guard
**Granularity:** Single trade

### Input
- Valid non-zero fill
- `size=0`

### Journey
- Layer 1: Tick enters.
- Layer 2: Fill resolves correctly.
- Layer 2: `size == 0` guard rejects event.
- Remaining layers are not reached.

### Signal decision
- No signal.
- No persistence write.

---

## QA-03 — Duplicate Drop
**Granularity:** Single trade, assuming an identical clean event was already seen inside TTL

### Input
- Same `occ_symbol`, `size`, and rounded fill as prior accepted event
- Arrives within dedup TTL

### Journey
- Layer 1: Tick enters.
- Layer 2: Parses normally.
- Layer 3: Dedup cache hit identifies duplicate.
- Layer 3: Event is dropped.
- Persistence path: No second write.
- Signal path: No second evaluation.

### Signal decision
- No new signal.
- Correct behavior is suppression of duplicate market-center reports.

---

## QA-04 — Sweep Upgrade Path
**Granularity:** Single trade as part of exchange fan-out cluster

### Input
- Same execution observed across 3+ exchanges within 8 seconds
- Trade otherwise parses as non-SWEEP on raw event shape

### Journey
- Layer 2: Event parses with valid direction, premium, contract type.
- Layer 3: Dedup recognizes exchange fan-out cluster.
- Layer 3: Trade type upgraded to `SWEEP`.
- Persistence path: Clean event continues.
- Signal path: Event enters Apex as upgraded SWEEP.

### Signal decision
- No signal by itself unless other gates/accumulator thresholds are met.
- Critical expected outcome is trade-type transformation before Apex L1.

---

## QA-05 — Synthetic Quote Rejected at Apex L1
**Granularity:** Single trade

### Input
- Synthetic quote: `bid=0`, `ask=0`
- Premium below institutional-quality expectation for this path
- Example: T1 synthetic SWEEP with $30K premium

### Journey
- Layer 2: Synthetic quote flagged; direction falls back to contract-type sentiment with `strong_sentiment=False`.
- Layer 3: Clean pass.
- Apex L1: Synthetic quote exception logic evaluates premium quality.
- Apex L1: Rejected as synthetic and too weak.
- Persistence path: Event may still persist to `flow_events` via independent fan-out.
- Signal path: Terminates at Apex L1.

### Signal decision
- No signal.
- This scenario proves persistence-path independence from signal rejection.

---

## QA-06 — Spread Gate Rejection
**Granularity:** Single trade

### Input
- Real NBBO quote
- `(ask - bid) / ask > 0.50`
- Otherwise strong premium and valid structure

### Journey
- Layer 2: Parses normally, real quote, strong sentiment path.
- Layer 3: Clean pass.
- Apex L1: Spread gate rejects event.
- Persistence path: Continues independently.
- Signal path: Ends here.

### Signal decision
- No signal.
- Rejection reason must be `spread_too_wide`.

---

## QA-07 — T1 SWEEP Below Premium Floor
**Granularity:** Single trade

### Input
- T1 ticker
- `trade_type=SWEEP`
- Premium below $50K floor, for example $40K

### Journey
- Layer 2: Parses into BUY CALL or similar strong path.
- Layer 3: Clean pass.
- Apex L1: Premium-floor check for T1 SWEEP fails.
- Signal path terminates.

### Signal decision
- No signal.
- Rejection reason must be `premium_below_floor`.

---

## QA-08 — T2/T3 SINGLE Below Premium Floor
**Granularity:** Single trade

### Input
- T2 or T3 ticker
- `trade_type=SINGLE`
- Premium below $150K floor

### Journey
- Layer 2: Parses normally.
- Layer 3: Clean pass.
- Apex L1: T2/T3 SINGLE floor applied.
- Event rejected.

### Signal decision
- No signal.
- This covers the second tier family and the highest gate floor family.

---

## QA-09 — Valid Event Accumulates But Does Not Yet Qualify
**Granularity:** Episode

### Input
- First qualifying event for an otherwise valid episode
- Premium high enough to pass L1
- Does not meet episode repetition requirements and bypass does not apply

### Journey
- Layer 2: Parses normally.
- Layer 3: Clean pass.
- Apex L1: Passes.
- Apex L2: Episode created or updated.
- Apex L2: Not enough trades or sweeps to qualify.
- Apex L4/L3/L5: Not reached for emission.

### Signal decision
- No signal yet.
- Episode remains buffered in accumulator state.

---

## QA-10 — Single-Event Sweep Bypass
**Granularity:** Episode

### Input
- One SWEEP event only
- `len(ep.events) == 1`
- Total premium >= $500K
- Example: AT_BID PUT SWEEP, $600K, 15 DTE, T1

### Journey
- Layer 2: AT_BID + PUT resolves to `order_side=SELL`, `sentiment=BULLISH`, `strong_sentiment=True`.
- Layer 3: Clean pass.
- Apex L1: Passes gates.
- Apex L2: DTE bucket 8–30 selected.
- Apex L2: Sweep bypass fires because single-event episode and premium threshold met.
- Apex L2: dominant_direction resolves to `REPEAT_BUY`.
- Apex L2: Alert level resolves to `STRONG_SIGNAL`.
- Apex L4: No ladder because only one strike episode exists.
- Apex L3: Full-score path, sector inactive, `composite_score_ceiling` applicable.
- Apex L5: Broadcast emits bullish composite and episode payload.

### Signal decision
- Signal emitted.
- Direction must be `REPEAT_BUY`.
- `composite_score_ceiling` must be present.

---

## QA-11 — Sweep Bypass Negative at Two Events
**Granularity:** Episode

### Input
- Two SWEEP events in the same episode
- Premium threshold met
- But `len(ep.events) == 2`

### Journey
- Layers 2–3: Both events parse and pass cleanly.
- Apex L1: Both pass.
- Apex L2: Episode total premium may exceed bypass amount, but bypass condition does not fire because the episode contains 2 events.
- Apex L2: Qualification depends on standard `min_sweeps` or repetition logic.

### Signal decision
- No early bypass-based signal.
- This scenario exists to prove the Issue 7 semantics are implemented exactly.

---

## QA-12 — BUY PUT Bearish BLOCK, Deep OTM
**Granularity:** Episode

### Input
- AT_ASK PUT
- `trade_type=BLOCK`
- Premium $1.5M
- 45 DTE
- Deep OTM at 15%
- T1 ticker

### Journey
- Layer 2: BUY PUT resolves to `order_side=BUY`, `sentiment=BEARISH`, `strong_sentiment=True`.
- Layer 2: Premium and exchange structure classify as BLOCK.
- Layer 2: Golden classification resolves to GOLDEN_BLOCK.
- Layer 3: Clean pass.
- Apex L1: BLOCK premium floor passes.
- Apex L2: DTE bucket 31–90 applied.
- Apex L2: Deep OTM multiplier applied and still passed.
- Apex L2: dominant_direction resolves to `REPEAT_SELL`.
- Apex L2: Alert level at least `STRONG_SIGNAL`, potentially `CONVICTION` depending on total episode premium.
- Apex L4: No ladder unless matched by sibling strikes.
- Apex L3: Full-score path, no weak-sentiment discount.
- Apex L5: Bearish signal persisted and broadcast.

### Signal decision
- Signal emitted.
- Direction must be `REPEAT_SELL`.
- GOLDEN_BLOCK path covered.

---

## QA-13 — SELL CALL Bearish SPLIT, Standard OTM
**Granularity:** Episode

### Input
- AT_BID CALL
- `trade_type=SPLIT`
- Premium $150K
- 20 DTE
- Standard OTM at 5%
- T2 ticker

### Journey
- Layer 2: SELL CALL resolves to `order_side=SELL`, `sentiment=BEARISH`, `strong_sentiment=True`.
- Layer 2: Trade classified as SPLIT.
- Layer 3: Clean pass.
- Apex L1: T2/T3 SPLIT floor applied and passed.
- Apex L2: DTE bucket 8–30 applied.
- Apex L2: Standard OTM path, no multiplier.
- Apex L2: dominant_direction becomes `REPEAT_SELL`.
- Apex L3: Full-score path.

### Signal decision
- Signal emitted as bearish.
- SPLIT path covered.

---

## QA-14 — BUY CALL Bullish LEAPS ATM
**Granularity:** Episode

### Input
- ABOVE_ASK CALL
- SWEEP
- Premium $2.5M
- 120 DTE
- ATM within 2%
- T1 ticker

### Journey
- Layer 2: ABOVE_ASK CALL resolves to BUY, BULLISH, strong.
- Layer 2: Trade type is SWEEP and qualifies as GOLDEN_SWEEP.
- Layer 3: Clean pass.
- Apex L1: T1 SWEEP premium floor passes.
- Apex L2: 91+ DTE bucket selected.
- Apex L2: ATM path uses standard floor, proving LEAPS are not blindly excluded.
- Apex L2: Alert level resolves to CONVICTION.
- Apex L4: No ladder in this single-strike case.
- Apex L3: sector inactive, so pre-ladder ceiling path applies.
- Apex L5: Payload includes `composite_score_ceiling`.

### Signal decision
- Signal emitted.
- Direction `REPEAT_BUY`.
- GOLDEN_SWEEP, ATM, 91+ DTE, and ceiling path covered together.

---

## QA-15 — MID Print Weak Sentiment Path
**Granularity:** Single trade or simple episode

### Input
- MID print
- CALL contract
- Premium high enough to pass gates

### Journey
- Layer 2: MID print produces `order_side=UNKNOWN`, fallback BULLISH sentiment, `strong_sentiment=False`.
- Layer 3: Clean pass.
- Apex L1: Passes if premium and spread permit.
- Apex L2: May accumulate or qualify depending on episode design.
- Apex L3: Flow score multiplied by 0.80 due to weak sentiment.

### Signal decision
- Signal may emit if the episode qualifies.
- Key expected behavior is composite discount, not rejection.

---

## QA-16 — Synthetic but Institutional-Quality Pass
**Granularity:** Single trade or episode

### Input
- Synthetic quote
- Premium high enough to pass exception logic
- Example: BLOCK with $200K premium

### Journey
- Layer 2: Synthetic quote forces weak sentiment.
- Layer 3: Clean pass.
- Apex L1: Synthetic exception passes due to quality/premium.
- Apex L2: Standard accumulation path.
- Apex L3: Weak-sentiment discount path.

### Signal decision
- Signal can emit if episode criteria qualify.
- This covers the synthetic-pass branch, distinct from synthetic reject.

---

## QA-17 — Deep OTM Multiplier Pass at 91+ DTE
**Granularity:** Episode

### Input
- CALL SWEEP
- 180 DTE
- T2 ticker
- Deep OTM at 20%
- Premium above multiplied floor

### Journey
- Apex L1: Passes T2/T3 SWEEP gate.
- Apex L2: 91+ DTE bucket selected.
- Apex L2: Deep OTM path computes 1.5× floor.
- Apex L2: Episode passes multiplied threshold.
- Remaining layers continue normally.

### Signal decision
- Signal emitted if repetition or bypass rules are met.
- This proves the multiplier can pass, not only reject.

---

## QA-18 — Missing underlying_price Fallback
**Granularity:** Episode

### Input
- Valid trade with `underlying_price == 0`
- Registry unable to enrich price in time or not available

### Journey
- Layer 2: Parses normally but price remains zero.
- Apex L1: Gate logic unaffected if premium and spread are sound.
- Apex L2: OTM classification skipped because underlying price is missing.
- Apex L2: Standard floor used.

### Signal decision
- Signal emitted or accumulated under standard floor logic.
- This is distinct from deep OTM or ATM handling.

---

## QA-19 — Registry Not Ready Path
**Granularity:** Single trade

### Input
- Valid raw stream event during cold-start window
- Registry not ready

### Journey
- Layer 2: First-pass direction inference runs using raw stream contract type.
- Layer 2: No enrichment of strike, DTE, OI, underlying price, or daily volume from registry.
- Layer 3 onward: Event continues with fallback fields.
- Apex L2: If `underlying_price` remains zero, standard-floor fallback path applies.

### Signal decision
- Event should still behave deterministically.
- This scenario validates graceful degradation during registry warmup.

---

## QA-20 — volume > OI Boost Path
**Granularity:** Episode

### Input
- High daily/episode volume relative to open interest
- Otherwise valid bullish or bearish structure

### Journey
- Layers 2 and L1: Pass normally.
- Apex L2: Qualifies.
- Apex L3: `volume > OI` contributes as a positive scoring boost.
- No rejection occurs based on this relationship.

### Signal decision
- Signal emitted with elevated composite relative to a control case.
- This is the direct regression guard against the architect’s original “inverted gate” concern.

---

## QA-21 — WATCH Alert Path
**Granularity:** Episode

### Input
- Episode total premium below $100K but still qualifies structurally

### Journey
- Apex L2: Alert level resolves to WATCH.
- Later layers still execute if the architecture emits WATCH-level qualified episodes.

### Signal decision
- WATCH-level decision documented.
- Needed because alert banding is an explicit architecture invariant.

---

## QA-22 — ALERT Alert Path
**Granularity:** Episode

### Input
- Episode premium >= $100K and < $500K

### Journey
- Apex L2: Alert level resolves to ALERT.
- Remaining layers continue normally.

### Signal decision
- ALERT emitted.

---

## QA-23 — STRONG_SIGNAL Without Bypass
**Granularity:** Episode

### Input
- Multi-event episode
- Premium >= $500K
- Qualifies through standard repetition logic, not bypass

### Journey
- Apex L2: No bypass branch.
- Apex L2: Standard qualification reached.
- Alert level STRONG_SIGNAL.

### Signal decision
- STRONG_SIGNAL emitted through the non-bypass path.

---

## QA-24 — CONVICTION Alert Path
**Granularity:** Episode

### Input
- Episode premium >= $2M

### Journey
- Apex L2: Alert level CONVICTION.
- Apex L3: Influence tier may also resolve to WHALE depending on final total.

### Signal decision
- CONVICTION emitted.

---

## QA-25 — Ladder Positive Path
**Granularity:** Episode set

### Input
- Same ticker
- Same expiry
- Three active qualifying episodes across distinct strikes, for example NVDA 580C, 590C, 600C

### Journey
- Each child episode independently reaches Apex L2 qualification.
- Apex L4: Grouped by ticker and expiry.
- Apex L4: Distinct strikes count reaches 3+, ladder fires.
- Apex L3: sector_score becomes active from ladder context.
- Apex L5: Composite payload should no longer use the pre-ladder ceiling field.

### Signal decision
- Ladder-enhanced signal emitted.
- `composite_score_ceiling` removed.

---

## QA-26 — Ladder Negative Cross-Expiry Guard
**Granularity:** Episode set

### Input
- Same ticker
- Three strikes distributed across different expiries

### Journey
- Apex L4: Grouping by `(ticker, expiry)` prevents aggregation into a ladder.
- Apex L4: No ladder fires.
- Apex L3: sector inactive path remains in force.

### Signal decision
- No ladder-enhanced signal.
- This is essential because otherwise multi-expiry flow would create false context.

---

## QA-27 — RETAIL Influence Tier
**Granularity:** Episode

### Input
- Qualified episode with total premium < $100K

### Journey
- Apex L3/L5: `episode_influence_tier()` resolves to RETAIL.

### Signal decision
- Composite payload emits `influence_tier=RETAIL`.

---

## QA-28 — WHALE Influence Tier with Ladder Active
**Granularity:** Episode set

### Input
- Qualified episode or laddered episode set with total premium >= $2M
- sector_score active through ladder context

### Journey
- Apex L4: Ladder fires.
- Apex L3: sector active, no ceiling field.
- Apex L5: `influence_tier=WHALE` emitted.

### Signal decision
- Full highest-conviction, ladder-enhanced, no-ceiling composite path emitted.

---

## Coverage Mapping

| Branch family | Covered by scenarios |
|---|---|
| fill==0 reject | QA-01 |
| size==0 reject | QA-02 |
| duplicate drop | QA-03 |
| sweep upgrade | QA-04 |
| synthetic reject | QA-05 |
| spread reject | QA-06 |
| T1 floor reject | QA-07 |
| T2/T3 floor reject | QA-08 |
| accumulate/no emit | QA-09, QA-11 |
| sweep bypass positive | QA-10 |
| sweep bypass negative | QA-11 |
| BUY PUT bearish path | QA-12 |
| SELL CALL bearish path | QA-13 |
| BUY CALL bullish path | QA-14 |
| weak sentiment MID path | QA-15 |
| synthetic pass path | QA-16 |
| deep OTM multiplier pass | QA-17 |
| missing underlying fallback | QA-18, QA-19 |
| registry-not-ready path | QA-19 |
| volume>OI boost | QA-20 |
| WATCH / ALERT / STRONG / CONVICTION | QA-21 / QA-22 / QA-23 / QA-24 |
| ladder positive | QA-25 |
| ladder negative cross-expiry | QA-26 |
| RETAIL influence tier | QA-27 |
| WHALE influence tier | QA-14, QA-24, QA-28 |
| pre-ladder ceiling present | QA-10, QA-14 |
| ladder-active ceiling removed | QA-25, QA-28 |
| persistence independent of signal rejection | QA-05, QA-06 |

---

## Recommended Post-Implementation Test Conversion
This document should be converted into two deliverables after implementation.

### 1. Scenario Replay Integration Suite
Create a replay harness that injects deterministic synthetic `OptionsFlowEvent`-equivalent raw ticks into the parser entrypoint and records the resulting path decisions. Each QA scenario above becomes one named fixture set.

Recommended structure:
- `tests/integration/test_apex_path_replay.py`
- One test function per QA scenario ID.
- Helper assertions for terminal layer, reject reason, alert level, direction, ladder status, and payload fields.

### 2. Architecture Trace Specification
Create a machine-readable YAML or JSON version of this spec so every scenario includes:
- raw input event list
- expected parser fields
- expected dedup outcome
- expected L1 verdict
- expected L2 episode state
- expected L4 ladder state
- expected L3 scoring modifiers
- expected L5 payload assertions

This will let the team run regression replays after any parser, gate, accumulator, or composite change.

---

## Test Artifact Guidance
The best way to operationalize this suite is:

- One fixture file per scenario family, for example parser rejects, L1 rejects, accumulator edge cases, ladder cases.
- Golden expected-output snapshots for composite payloads.
- A path-trace logger in test mode that records: `entered_layer`, `branch_taken`, `reason`, `payload_delta`.
- CI should fail if any scenario terminates in a different layer or with a different reason than specified here.

---

## Final QA Position
The critical point is this: success-path testing alone will miss the architecture’s real failure modes. The most dangerous regressions in Cipher Apex are semantic reversals, silent gate inversions, and episode-state mistakes that still produce valid-looking output. This is why the scenario suite has to cover reject paths, weak-sentiment paths, fallback paths, accumulator edge paths, and ladder/no-ladder divergence — not just “good bullish sweep” examples.

This 28-scenario document is the recommended baseline for full layered-architecture path coverage in the current Apex design.
