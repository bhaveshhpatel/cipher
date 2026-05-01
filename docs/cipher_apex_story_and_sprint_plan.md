# Cipher Apex Signal Pipeline — Story and Sprint Plan

## Purpose
This document is the single source of truth for the refined story set and sprint execution plan for the Cipher Apex signal pipeline. It incorporates the full architecture and engineering review, including parser direction inference, alert-level reconciliation, accumulator redesign, composite-score corrections, test coverage policy, and deferred swarm scope.

## Planning Principles
- New files must have 100% line and branch coverage.
- Modified files must cover every new or changed branch.
- All existing tests must pass before any PR merges.
- Directional invariants are CI gate tests and cannot regress.
- No swarm code exists outside the new Apex-scoped implementation.
- Fake backtest scoring must not influence production composite scores.

## Sprint Order
1. S0 — Swarm cleanup.
2. S1 — Alert level threshold reconciliation and emit-cache flush.
3. S2 — Parser and detector layer fixes.
4. S2.5 — Supabase migration for order direction fields.
5. S3 — Apex L1 signal gate.
6. S4 — Apex L2 dual-window accumulator.
7. S5 — Apex L4 ladder detection.
8. S6 — Apex L3 composite overhaul and hot-path corrections.
9. S7 — Tiered swarm and circuit breaker, only after stream worker review.
10. S8 — Real backtest score, future sprint.

---

## S0 — Swarm Cleanup
**Type:** Prerequisite housekeeping  
**Status:** Must land first

### Scope
- Remove `build_composite_async()` entirely from `composite_signal_engine.py`.
- Remove `run_ensemble` imports, aliasing, and patch-compatibility wiring.
- Keep `build_composite()` as the only active composite path.
- Mark `simulation/ensemble_runner.py` deprecated until all references are removed.
- Confirm no tests still mock the old swarm path before deleting the old file.

### Acceptance Criteria
- `build_composite_async()` no longer exists.
- No `run_ensemble` import exists in `composite_signal_engine.py`.
- Existing composite tests pass unchanged or with only expected cleanup edits.
- No import errors remain anywhere in the test suite.

### Test Coverage
- Assert the async composite path no longer exists.
- Assert old ensemble import wiring is removed.
- Full regression suite passes.

---

## S1 — Alert Level Threshold Reconciliation + Emit Cache Flush
**Type:** Bug fix  
**Depends on:** S0

### Scope
- Reconcile `get_alert_level()` thresholds in `RepetitionAccumulator` to the Apex definitions.
- Flush `_signal_last_emit` on startup so threshold changes do not cause false de-escalation or stale debounce behavior.
- Document the cutover line: historical rows keep old labels, new rows use the corrected labels.
- No historical backfill.

### Acceptance Criteria
- Threshold boundaries match the approved Apex alert bands.
- Startup path clears `_signal_last_emit` before processing live traffic.
- No unexpected alert churn appears immediately after deploy.

### Test Coverage
- Threshold boundary tests for all alert levels.
- Startup-flush test for `_signal_last_emit`.
- Regression tests for debounce logic still pass.

---

## S2 — Parser + Detector Layer Fixes
**Type:** Feature + bug fix  
**Depends on:** S0

### Goals
- Replace naive contract-type sentiment with direction-aware inference.
- Preserve the SELL PUT = BULLISH invariant across parser, accumulator, persistence, and signal publishing.
- Fix parser enrichment so registry metadata helps without overwriting direction logic.
- Improve whale/shark classification fidelity.

### New File
#### `backend/parsers/order_side_classifier.py`
Responsibilities:
- Infer `order_side` from `bid_ask_class` and `contract_type`.
- Infer `sentiment` from execution behavior, not contract type alone.
- Expose `strong_sentiment` when direction is unambiguous.
- Convert `(order_side, contract_type)` into `REPEAT_BUY` / `REPEAT_SELL` direction.

### Core Rules
- BUY CALL = BULLISH (strong).
- BUY PUT = BEARISH (strong).
- SELL CALL = BEARISH (strong).
- SELL PUT = BULLISH (strong).
- MID and synthetic quotes fall back to contract-type-based sentiment, but with `strong_sentiment=False`.

### Dataclass Changes
Add to `OptionsFlowEvent`:
- `order_side: str = "UNKNOWN"`
- `strong_sentiment: bool = False`
- `daily_volume: int = 0`

### Parser Changes
- Add sell-side aggression recognition.
- Compute `is_directionally_aggressive = buy_aggressive or sell_aggressive`.
- Pass directionally aggressive status into golden-sweep logic.
- Call `classify_order_direction()` after bid/ask classification.
- Remove the registry block's naive sentiment overwrite.
- Re-run direction classification after registry-corrected contract metadata is applied.
- Populate `underlying_price` from `reg.stock_price(ticker)` when missing.
- Populate `daily_volume` from registry quotes.
- Add `fill == 0` guard.

### Detector Changes
- Extend BLOCK detection to include high-premium, low-exchange multi-fill whales.
- Extend golden-sweep logic to include golden BLOCK at >= $1M.
- Rename the golden-sweep parameter to directionally aggressive terminology.

### Symbol Registry Changes
- Persist raw quote payloads in memory.
- Add `get_daily_volume(ticker)`.
- Ensure parser can enrich `daily_volume` without new API fetches.

### Repetition Accumulator Changes
Add `dominant_direction` as a premium-weighted episode property. This must preserve the invariant that an episode dominated by SELL PUT premium resolves to `REPEAT_BUY`.

### Acceptance Criteria
- SELL PUT flows are classified as `order_side=SELL`, `sentiment=BULLISH`, `strong_sentiment=True` when quote placement supports it.
- Registry enrichment never overwrites a correctly inferred sentiment with naive contract-type logic.
- Premium-based BLOCK detection catches single-venue whale executions.
- Golden BLOCK classification works at approved thresholds.
- `underlying_price` and `daily_volume` enrich correctly when registry is ready.

### Test Coverage

#### CI Gate Invariants — All Four Quadrants Required
> **Updated April 30 2026 (Issue 8 resolution — Architect + Principal Engineer deliberation):**
> All four direction quadrants are now CI gate requirements. The original list covered only
> SELL side. BUY CALL = BULLISH and BUY PUT = BEARISH are equally regressionable. A parser
> refactor breaking BUY PUT direction would not be caught by the old invariant set.

A dedicated file must enforce:

**SELL-side (original):**
- `AT_BID + PUT => sentiment=BULLISH`
- `BELOW_BID + PUT => sentiment=BULLISH`
- `AT_BID + PUT => order_side=SELL`
- `AT_BID + PUT => strong_sentiment=True`
- `SELL + PUT => REPEAT_BUY`
- `SELL + CALL => REPEAT_SELL`

**BUY-side (added — Issue 8):**
- `AT_ASK + CALL => sentiment=BULLISH`
- `AT_ASK + CALL => order_side=BUY`
- `AT_ASK + CALL => strong_sentiment=True`
- `AT_ASK + PUT => sentiment=BEARISH`
- `AT_ASK + PUT => order_side=BUY`
- `AT_ASK + PUT => strong_sentiment=True`
- `BUY + CALL => REPEAT_BUY`
- `BUY + PUT => REPEAT_SELL`

#### Required Test Files
- `tests/test_order_side_classifier.py`
- `tests/test_direction_invariants.py`  ← must cover all 14 invariants above
- `tests/test_bid_ask_classifier.py`
- `tests/test_trade_type_detector.py`
- `tests/test_options_flow_parser.py`
- `tests/test_repetition_accumulator.py`

#### Coverage Standard
- New module: 100% branch and line coverage.
- Every changed parser branch covered, including registry fallback and synthetic quote paths.

---

## S2.5 — Supabase Migration: `order_side` + `strong_sentiment`
**Type:** DB migration  
**Depends on:** S2

### Scope
- Add `order_side` column with `BUY | SELL | UNKNOWN` check constraint.
- Add `strong_sentiment` boolean column.
- Index `order_side` for analytics queries.
- Backfill historical rows conservatively and mark them as not strongly directional.

### Acceptance Criteria
- Live writes with `order_side` and `strong_sentiment` succeed.
- Existing readers do not break.
- Historical rows remain queryable.

### Test Coverage
- Integration test verifies new fields persist successfully.
- Migration test verifies repeated application is safe.

---

## S3 — Apex L1: `signal_gate.py`
**Type:** New module  
**Depends on:** S2

### Scope
- Create a first-layer gate that filters obvious noise before accumulation.
- Gate by trade type, tier, premium floor, spread quality, and aggression quality.
- Use direction-aware aggression, not just buy-side aggression.
- Return a structured verdict with failure reason.

### Acceptance Criteria
- Weak, wide-spread, low-premium noise is rejected before entering the signal path.
- Spread gate threshold is 50% of ask price, applied uniformly across all tiers.
- High-quality SELL PUT flow can pass when it meets premium and quote-quality gates.
- Tier-aware premium floors differ for T1 versus T2/T3 names.

### Test Coverage
- 100% branch coverage on gate outcomes.
- Explicit tests for SELL PUT acceptance path.

---

## S4 — Apex L2: Dual-Window Accumulator
**Type:** Refactor + feature  
**Depends on:** S3

### Scope
- Replace the static DTE cap with DTE-adjusted premium floors.
- Expand OTM eligibility to include ATM and selected deeper OTM activity.
- Add deep-OTM premium multiplier.
- Add whale-conviction bypass for a single huge sweep episode event.
- Preserve low-threshold persistence in the DB path while raising the live signal bar.

### ATM Band Definition
> **Deliberation note (Architect + Principal Engineer, April 30 2026 — Issue 6 resolution):**
> ATM is defined as a percentage of underlying price, not an absolute dollar amount.
> ±2% was selected as the working threshold: tight enough to exclude clear OTM, wide
> enough to capture ATM prints on high-underlying-price names (e.g., NVDA at $900+ where
> a ±$5 strike gap is < 1%). Contracts with `underlying_price == 0` fall back to standard
> floor and are not classified as OTM or deep OTM.

ATM condition: `abs(strike - underlying_price) / underlying_price <= 0.02`

### Sweep Bypass — `trade_count` Semantics
> **Deliberation note (Architect + Principal Engineer, April 30 2026 — Issue 7 resolution):**
> `trade_count` (or equivalently `len(ep.events)`) is the number of `OptionsFlowEvent`
> objects that have accumulated in the episode. It is NOT the `fill_count` field within
> a single stream tick. A single-event episode means exactly one qualifying event entered
> the accumulator for this (ticker, strike, expiry) key. The bypass fires when that one
> event is a SWEEP with premium >= $500K — at that size and structure, the min_sweeps
> repetition requirement adds no information.

Bypass condition: `len(ep.events) == 1 AND trade_type == "SWEEP" AND premium >= 500K`

### Default DTE Premium Tiers
- 0–7 DTE: T1 = $50K, T2/T3 = $25K
- 8–30 DTE: T1 = $500K, T2/T3 = $100K
- 31–90 DTE: T1 = $1M, T2/T3 = $500K
- 91+ DTE: T1 = $2M, T2/T3 = $1M

### OTM Classification
- ATM (0–2% OTM): standard premium floor
- Standard OTM (2–12%): standard premium floor
- Deep OTM (> 12%): 1.5× premium floor multiplier
- No underlying_price: standard floor, no OTM classification attempted

### Acceptance Criteria
- LEAPS are no longer blindly excluded.
- ATM is defined as `abs(strike - underlying_price) / underlying_price <= 0.02`; contracts in this band use standard floors.
- Deep OTM (> 12%) requires 1.5× premium floor.
- Single-event episodes (`len(ep.events) == 1`) of type SWEEP at >= $500K bypass `min_sweeps`.
- Events with `underlying_price == 0` fall back to standard floor, not OTM classification.

### Test Coverage
- 100% new-branch coverage for DTE tiers, OTM checks, deep-OTM multiplier, and sweep bypass.
- ATM boundary test: contract at exactly 2.0% OTM → standard floor; contract at 2.01% → standard OTM floor.
- Sweep bypass: `len(ep.events) == 1` with SWEEP and premium >= 500K → passes without meeting min_sweeps.
- Sweep bypass negative: `len(ep.events) == 2` same SWEEP and premium → bypass does not fire; must meet min_sweeps.
- Zero underlying_price: no OTM computation, standard floor applied.

---

## S5 — Apex L4: Cross-Contract Ladder Detection
**Type:** New module  
**Depends on:** S4

### Scope
- Detect coordinated same-ticker, same-expiry multi-strike activity.
- Surface ladder structures as higher-context conviction signals.
- Feed ladder output into the `sector_score` input of the Apex L3 composite scorer.
- Ladder detector runs **before** composite scoring in the hot path.

### Acceptance Criteria
- Ladder only fires when multiple related strikes align within the active window.
- Unrelated expiries do not false-trigger.
- Expired ladder state is evicted correctly.
- Ladder output is passed as context into L3 composite scoring (wires `sector_score`).
- When S5 lands and ladder context is wired, `composite_score_ceiling` field is removed from the composite bus payload.

### Test Coverage
- 100% branch coverage.
- Positive and negative ladder scenarios.

---

## S6 — Apex L3: Composite Formula Overhaul + Hot Path Corrections
**Type:** Refactor + bug fix  
**Depends on:** S2, S2.5, S4, S5

### Composite Formula Changes
- Remove fake backtest influence from production scoring.
- Use episode-level influence tier instead of per-tick tier.
- Discount flow confidence when sentiment is weak or unknown.
- Keep sector/context weight reserved until ladder/context data is available from S5.

### Score Ceiling — Pre-S5 Behavior
> **Deliberation note (Architect + Principal Engineer, April 30 2026 — Issue 5 resolution):**
> With `sector_score = 0.0` and `backtest_score = 0.0`, the active weights sum to 0.90,
> capping composite_score at 0.90 until S5 ladder data activates sector_score. The team
> decided NOT to redistribute the 0.10 weight — doing so would shift scoring baselines
> and invalidate threshold calibration. Instead, the ceiling is made explicit in the
> composite bus payload via a `composite_score_ceiling` field (value: 0.90). This field
> is removed from the payload when S5 wires in real ladder context and sector_score
> receives a non-zero value.

### Approved Formula
- `flow_score * 0.55`
- `volume_weighted_premium_factor * 0.20`
- `premium_tier_score * 0.15`
- `sector_score * 0.10` — activates in S5; `0.0` until then
- `backtest_score * 0.00` until S8 is implemented

### Hot Path Changes in `tradier_stream.py`
- Replace naive `contract_type => direction` logic with episode `dominant_direction`.
- Ensure SELL PUT episodes publish and persist as `REPEAT_BUY`.
- Add `order_side` and `strong_sentiment` to persistence payloads and composite signal payloads.
- Publish episode influence tier using total episode premium, not latest tick premium.
- Update demo-mode direction generation so it does not reinforce the naive CALL=BUY / PUT=SELL assumption.
- Add `composite_score_ceiling: 0.90` to composite bus payload (remove when S5 activates sector_score).

### Acceptance Criteria
- Composite scoring no longer depends on pseudorandom backtest values.
- SELL PUT campaigns persist as bullish direction end to end.
- Composite payloads include order-side information for frontend interpretation.
- Episode influence tier reflects episode premium, not single-tick premium.
- `composite_score_ceiling: 0.90` is present in bus payload while sector_score is inactive.
- `composite_score_ceiling` is removed from payload when S5 ladder context is wired.

### Test Coverage
- `episode_influence_tier()` boundaries.
- `strong_sentiment=False` discount path.
- `backtest_score == 0.0` in production composite output.
- SELL PUT signal path publishes `REPEAT_BUY` in persistence and bus messages.
- `composite_score_ceiling` field is present in payload before S5; absent after.

---

## S7 — Tiered Swarm + Circuit Breaker
**Type:** New feature  
**Depends on:** S6  
**Status:** Blocked pending stream worker review

### Scope
- Only Apex-layer swarm is allowed.
- No reuse of old dead swarm infrastructure.
- L1 and L2 signals can optionally call new async model logic with a hard timeout.
- Circuit breaker downgrades to deterministic scoring when repeated timeouts occur.

### Precondition
Review `stream_worker.py` to confirm whether `_process_trade()` runs sequentially or via task scheduling. If sequential, scope must change to avoid event-loop stalls.

### Acceptance Criteria
- Swarm path is isolated, timeout-bounded, and circuit-breaker protected.
- Deterministic fallback always exists.

### Test Coverage
- Timeout path.
- Circuit breaker open/close behavior.
- Deterministic fallback path.

---

## S8 — Real Backtest Score from `flow_events`
**Type:** Future feature  
**Depends on:** S6  
**Status:** Future sprint only

### Scope
- Replace the fake seeded backtest score with a real 90-day historical win-rate query.
- Use `(ticker, contract_type, dte_bucket)` buckets.
- Cache results to avoid per-tick database hits.
- Reintroduce non-zero backtest weight only after the real implementation is complete.
- When S8 lands, revisit composite weight distribution (backtest_score weight to be determined based on validated results).

### Acceptance Criteria
- Backtest score is computed from real historical behavior.
- Query cost is controlled through caching.
- No production fallback to pseudorandom score.

### Test Coverage
- Query bucket selection.
- Cache hit/miss behavior.
- No production fallback to pseudorandom score.

---

## Cross-Cutting Quality Bar

### Coverage Policy
- New files: 100% line and branch coverage.
- Modified code: every changed branch covered.
- Existing regression suite: all green before merge.
- Any new import fallbacks must use the same tested/fallback-safe import pattern already present in the parser.

### Directional Invariants
> **Updated April 30 2026 (Issue 8 resolution):** All four quadrants are now non-negotiable
> CI gate requirements. The original list only covered SELL-side invariants. BUY CALL and
> BUY PUT symmetry is equally regressionable and must be enforced.

These are non-negotiable and must be enforced by dedicated tests:

**SELL-side (original):**
- SELL PUT is bullish.
- SELL CALL is bearish.

**BUY-side (added):**
- BUY CALL is bullish.
- BUY PUT is bearish.

**General:**
- Unknown direction remains weak-confidence, not strong-confidence.
- Registry enrichment must never overwrite direction inference with naive contract-type sentiment.

### Data Integrity Rules
- Open interest is options-chain OI, not stock OI.
- Swarm is Apex-only and nowhere else.
- Production composite scoring cannot use fake backtest values.
- Episode-level fields must use episode context, not latest-tick shortcuts, where that changes semantics.
- `composite_score_ceiling` must be present in bus payloads while sector_score is inactive (pre-S5 wiring).

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

## Implementation Notes
- S2 and S2.5 should be treated as a single release train because persistence schema must exist before hot-path writes start using the new fields.
- S6 should not begin until S4 and S5 have stabilized, because it consumes accumulator and episode semantics.
- S7 is explicitly blocked pending stream-worker concurrency review.
- S8 is intentionally separated so the team does not quietly keep fake backtest weight in production.
- S6 introduces `composite_score_ceiling = 0.90` in the bus payload. This field must be removed from the payload — and from this note — when S5 ladder data is wired into `sector_score`.
