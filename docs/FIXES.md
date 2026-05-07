# Cipher — Bug Fix Log

Chronological record of all bugs found and fixed. Each entry includes root cause, symptom, and the exact change made.

---

## ING-011b — ITM PUT AT_BID Buyer Episodes Were Overcounted as Full-Weight Aggressive Premium

**Date:** 2026-05-07
**Severity:** P1 — signal correctness; ITM/DEEP_ITM PUT fills at `AT_BID`/`BELOW_BID` were counted at full `weighted_premium` because `is_aggressive` is moneyness-blind, inflating Gate 2 clears and downstream conviction on bearish put-buyer episodes
**PR:** [#82](https://github.com/bhaveshhpatel/cipher/pull/82) — squash merged 2026-05-07 (commit `5e9dd22`)
**Branch:** `ing/s11b-itm-aggression-weight`
**Files:** `backend/signals/repetition_accumulator.py`, `backend/tests/test_ing011b_itm_aggression_weight.py`
**Issue:** [#80](https://github.com/bhaveshhpatel/cipher/issues/80) — closed 2026-05-07

### Root Cause

ING-006 introduced `weighted_premium` using `event.is_aggressive` to decide whether a fill should receive full weight (`×1.0`) or discounted weight (`×0.5`). That works for genuine aggressive writer/buyer cases, but `is_aggressive` is computed at parse time from `bid_ask_class + contract_type` only — it does **not** know the contract's moneyness.

That blind spot became incorrect after ING-011 fixed direction for ITM puts. An ITM or DEEP_ITM PUT filling `AT_BID` is often a bearish buyer paying near intrinsic value, not a bullish put writer. Direction was corrected by ING-011, but `weighted_premium` still treated the same event as aggressive and granted full weight, overstating episode conviction.

**Failure mode:**
- `contract_type = PUT`
- `bid_ask_class in {AT_BID, BELOW_BID}`
- `is_aggressive = True` (from ING-006 parse-time logic)
- `otm_band in {ITM, DEEP_ITM}` (from ING-011 moneyness classification)
- Result before fix: full-weight premium on an ITM put buyer episode that should have been discounted

### Deliberation Decisions (3-way panel, 2026-05-06)

**D1 — Option B chosen: fix weighting in `get_weighted_premium()`**
- Apply `_AGGRESSION_DISCOUNT` to ITM/DEEP_ITM PUT `AT_BID`/`BELOW_BID` fills per-event inside `get_weighted_premium()`.
- Option A rejected — patching `is_directionally_aggressive()` would pull signal-layer moneyness logic back into the parser path and invert the dependency graph.
- Option C rejected — changing stored DB `is_aggressive` semantics would skew ING-007 `prior_days_aggressive` history and blur the meaning of the parse-time column.

**D2 — Per-event classification chosen over episode-level `self.otm_band`**
- `self.otm_band` is effectively last-tick state and is not safe for applying discounts to all prior events in an episode.
- The fix must classify each event individually during `get_weighted_premium()` iteration.

**D3 — `_classify_moneyness_band()` promoted to module level**
- The function is pure arithmetic over event fields and has no `self` dependency.
- Promotion removes duplication and lets both `ingest_tick()` and `get_weighted_premium()` use the same classifier.
- A backward-compat shim was retained on `RepetitionAccumulator` for pre-D3 callers/tests.

**D4 — `prior_days_aggressive` accepted as a known gap**
- `flow_events.is_aggressive` remains parse-time and moneyness-blind.
- ING-011b intentionally fixes **episode weighting**, not historical DB aggression counts.
- A follow-up story is only needed if multi-day aggression metrics show material skew after several live sessions.

**D5 — `UNKNOWN` band fallback: no discount**
- If `underlying_price == 0`, `_classify_moneyness_band()` returns `UNKNOWN`.
- `UNKNOWN` is not part of `_ITM_BANDS`, so the event receives full weight.
- Safe default: when moneyness cannot be proven, do not discount.

### Fix

**`backend/signals/repetition_accumulator.py`:**
- Promoted `_classify_moneyness_band()` to module-level function
- Added `_ITM_BANDS = frozenset({"ITM", "DEEP_ITM"})`
- Updated `_majority_itm_band()` to delegate to the shared module-level classifier
- Updated `get_weighted_premium()` so ITM/DEEP_ITM PUT `AT_BID`/`BELOW_BID` fills receive `_AGGRESSION_DISCOUNT` even when `is_aggressive=True`
- Kept all other aggression paths unchanged: OTM PUT writers, ITM CALL writers, `AT_ASK` buyers, and passive MID fills preserve prior behaviour

### Panel Findings (resolved inline before merge)

- **SA-1:** Non-blocking architecture note — module-level classifier promotion is the right boundary; resolved inline
- **PBE-1:** Non-blocking implementation note — preserve backward-compat shim while promoting classifier; resolved inline
- **PBE-2:** Non-blocking regression note — `_majority_itm_band()` must call the shared classifier to avoid duplicated threshold logic; resolved inline
- **QA-1:** Boundary coverage around W-4b required — added inline before merge
- **QA-2:** Test typo fix (`ite_006` → `ing_006` in W-6 method name) committed inline before merge

### Tests Added (`backend/tests/test_ing011b_itm_aggression_weight.py`)

30 assertions across W-1 through W-12 covering OTM writer regressions, ITM/DEEP_ITM PUT buyer discounting, ITM CALL no-change behaviour, passive MID-fill retention, mixed-event episodes, `UNKNOWN` fallback, and `weighted_premium` property delegation.

**Regression guards explicitly preserved:**
- W-1 / W-11: OTM PUT `AT_BID` writer keeps full weight (`×1.0`)
- W-5: ITM CALL `AT_BID` writer unchanged (`×1.0`)
- W-6: OTM PUT `MID` passive fill remains discounted (`×0.5`)

### Acceptance Criteria
- [x] D1–D5 deliberations resolved and documented
- [x] `_classify_moneyness_band()` promoted to module-level function
- [x] `_majority_itm_band()` delegates to shared module-level classifier
- [x] `get_weighted_premium()` updated per Option B
- [x] `weighted_premium` property delegates to updated weighting logic
- [x] ITM/DEEP_ITM PUT `AT_BID`/`BELOW_BID` events are discounted to `×0.5`
- [x] OTM PUT writer full-weight behaviour unchanged
- [x] ITM CALL `AT_BID` writer behaviour unchanged
- [x] `underlying_price == 0` / `UNKNOWN` fallback receives full weight
- [x] 30 assertions across W-1 through W-12 passing
- [x] No regression on ING-006, ING-007, or ING-011 behaviour

---

## ING-011 — ITM Put/Call Moneyness Classification + Direction Override

**Date:** 2026-05-07
**Severity:** P0 — signal correctness; deeply ITM puts filling AT_BID were classified as REPEAT_SELL (bullish/put-writing) when the correct read is bearish put buying
**PR:** [#81](https://github.com/bhaveshhpatel/cipher/pull/81) — squash merged 2026-05-07 (commit `8d68ed1`)
**Branch:** `ing/s11-itm-classification`
**Files:** `backend/signals/repetition_accumulator.py`, `backend/tests/test_ing011_itm_classification.py`
**Issue:** [#77](https://github.com/bhaveshhpatel/cipher/issues/77) — closed 2026-05-07

### Root Cause

`dominant_direction` in `RepetitionAccumulator` mapped `AT_BID PUT fill → REPEAT_SELL` (put writing = bullish). This is correct for OTM puts where AT_BID means a seller initiating. For ITM puts, AT_BID simply reflects a buyer paying near-intrinsic value in a wide spread — not a put writer. The existing `_classify_otm()` method only classified OTM/ATM bands; ITM contracts fell to `UNKNOWN` with no override logic, allowing the incorrect AT_BID=seller assumption to propagate.

**Live example — TMDX 2026-05-06 13:43:50 UTC:**
- PUT $105 · May 15 · underlying $75.69 · size 1,263 · fill $27.68 · bid $26.70 · ask $29.50
- `bid_ask_class = AT_BID` → system: `REPEAT_SELL` (bullish) — **incorrect**
- Actual: strike ~39% above underlying = deeply ITM put buyer = bearish

### Deliberation Decisions (3-way panel, 2026-05-06)

**D1 — ITM threshold: DECIDED — reuse ING-005 ATM band (±2%) exactly**
- `_ITM_THRESHOLD = 0.02` — symmetric with ING-005 ATM ±2% band
- `_DEEP_ITM_THRESHOLD = 0.10` — symmetric with existing `DEEP_OTM` boundary
- Rationale: reusing the ING-005 threshold ensures consistent moneyness classification across the full band spectrum. No new magic numbers introduced.
- Options B (tighter 1%) and C (wider 5%) both rejected — no empirical basis to deviate from the established ±2% regime.

**D2 — Override scope: DECIDED — ALL ITM (not just DEEP_ITM), PUT-only**
- Override applies to both `ITM` and `DEEP_ITM` bands for PUTs
- ITM CALL AT_BID unchanged — call seller writing at bid is correctly bearish already
- Mildly ITM puts (2–10% ITM) can legitimately represent put writing in slow markets, but the deliberation concluded the signal correctness gain on institutional ITM put buyers outweighs the edge-case risk on mild ITM sellers
- Option A (DEEP_ITM only, >10%) rejected — misses the 2–10% ITM band where most institutional hedges and synthetic short positions land

**D3 — Schema: DECIDED — extend `otm_band` enum in-place, no DB migration**
- `ITM` and `DEEP_ITM` added to existing `otm_band` TEXT column values on `RepetitionEpisode`
- `_classify_otm()` replaced by `_classify_moneyness_band()` — full spectrum: `DEEP_ITM | ITM | ATM | OTM | DEEP_OTM | UNKNOWN`
- No DB migration required — `otm_band` column is TEXT, not a Postgres enum type
- Option B (separate `itm_band` field) rejected — adds schema surface with no benefit; the existing `otm_band` field already semantically covers the full moneyness spectrum

### Fix

**`backend/signals/repetition_accumulator.py`:**
- `_classify_otm()` replaced by `_classify_moneyness_band()` — full spectrum classification
- `_ITM_THRESHOLD = 0.02` and `_DEEP_ITM_THRESHOLD = 0.10` added at module level
- `dominant_direction` override: `contract_type == PUT AND otm_band in (ITM, DEEP_ITM) AND bid_side_prem > ask_side_prem` → force `REPEAT_BUY` (bearish)

### Panel Findings (resolved inline before merge)

- **SA-F1:** `_majority_itm_band()` helper needed UNKNOWN-tick suppression — resolved with test I-12 added
- **QA-F1:** Test I-12 (UNKNOWN-band suppression) added to test matrix — resolved
- **QA-F3:** I-8 docstring clarified (`underlying_price == 0` fallback) — resolved

### Tests Added (`backend/tests/test_ing011_itm_classification.py`)

34 tests across 3 classes: `TestClassifyMoneynessBand` (unit tests for `_classify_moneyness_band()` directly), `TestITMDirectionOverride` (full episode integration tests for QA matrix cases I-1 through I-11), `TestThresholdConstants` (sanity checks on `_ITM_THRESHOLD == 0.02` and `_DEEP_ITM_THRESHOLD == 0.10`).

### Acceptance Criteria
- [x] D1, D2, D3 deliberations resolved and documented
- [x] `_classify_moneyness_band()` replaces `_classify_otm()` — full band spectrum
- [x] `dominant_direction` for ITM/DEEP_ITM puts resolves to `REPEAT_BUY` (bearish) regardless of `bid_ask_class`
- [x] TMDX $105P scenario re-run produces correct `BEARISH` direction
- [x] Existing OTM put `AT_BID` → `REPEAT_SELL` (bullish) behaviour unchanged
- [x] `underlying_price == 0` fallback: no ITM classification attempted, `UNKNOWN` preserved
- [x] 34 tests added, all passing
- [x] No regression on ING-006 or ING-007 test suites

---

## ING-009 — Same-Session Flow Episode Upsert/Merge

**Date:** 2026-05-06
**Severity:** P0 — data model correctness; `flow_episodes` was insert-only, producing near-duplicate rows per qualifying print instead of one aggregated episode per session
**PR:** [#76](https://github.com/bhaveshhpatel/cipher/pull/76) — squash merged 2026-05-06 (commit `9ceee35`)
**Branch:** `ing/s9-episode-upsert`
**Files:** `backend/services/flow_store.py`, `backend/tests/test_ing009_episode_upsert.py`

### Root Cause

`persist_flow_episode()` in `flow_store.py` had no upsert/merge path. Every call to the function unconditionally ran `_insert_rows("flow_episodes", [row])`, creating one new `flow_episodes` row per Signal Gate crossing regardless of whether an open episode for that contract already existed in the current session.

The EPISODE-FIX (2026-04-30) correctly moved episode persistence before SIG-DEBOUNCE to preserve `strike`/`expiry`. In doing so it exposed this pre-existing insert-only behaviour: with the SIG-DEBOUNCE gate removed from the write path, every qualifying print now reached `persist_flow_episode()` directly, producing one row per print rather than one row per session episode.

On 2026-05-05 the symptom was confirmed in Supabase: `flow_episodes` had 26,906 rows vs. `flow_events` 28,373 rows — near-1:1 ratio instead of the expected aggregated ratio. `ING-007`'s `get_contract_prior_days()` query depends on `flow_episodes` being correctly aggregated, making ING-009 a hard prerequisite.

### Option Decision — PBE Deliberation (2026-05-05)

**Option A (chosen): Insert-or-PATCH upsert in `persist_flow_episode()` keyed on contract identity + session window**
- Lookup open episode via Supabase REST: `flow_episodes WHERE (ticker, direction, contract_type, strike, expiry) AND signal_ts >= now() - 1800s ORDER BY signal_ts DESC LIMIT 1`
- **Match found →** PATCH: `trade_count += 1`, `total_premium += new_premium`, `signal_ts = new_ts`; increment `_stats["merged_episodes"]`
- **No match →** INSERT (existing path); increment `_stats["created_episodes"]`
- Merge logic entirely in `flow_store.py` — not in `tradier_stream.py`, not in the accumulator

**Option B (rejected): Tie episode write back to SIG-DEBOUNCE gate**
- Rejected: loses `strike`/`expiry` on non-debounce-qualifying episodes (reverts the EPISODE-FIX). Hides data rather than models it correctly.

**Option C (rejected): Add a `min_trade_count > N` drop gate before DB write**
- Rejected: hides data, same root objection as Option B. A 3-print episode is genuinely different from a 30-print episode — suppressing the former destroys information the signal layer needs.

### Fix

**`backend/services/flow_store.py`:**
```python
_EPISODE_MERGE_WINDOW_S: int = 1800  # 30 min — module-level constant

async def _lookup_open_episode(key_fields: dict, window_s: int) -> Optional[dict]:
    # Query flow_episodes for open episode matching merge key within window
    # Returns episode row dict if found, else None

async def persist_flow_episode(signal_data: dict) -> None:
    # Build merge key: (ticker, direction, contract_type, strike, expiry)
    # existing = await _lookup_open_episode(key_fields, _EPISODE_MERGE_WINDOW_S)
    # if existing:
    #     PATCH id: trade_count += 1, total_premium += new, signal_ts = new_ts
    #     _stats["merged_episodes"] += 1
    # else:
    #     INSERT (existing path)
    #     _stats["created_episodes"] += 1
```

**New `_stats` counters (module-level init):**
```python
"created_episodes": 0,   # INSERT path — new session episode
"merged_episodes":  0,   # PATCH path — existing episode updated
```

Both counters exposed in `/health/stream` from cold start.

### Tests Added (`backend/tests/test_ing009_episode_upsert.py`)

11 test cases covering the full matrix: E-1 (first insert), E-2/E-3 (merge within window, `trade_count` accumulation), E-4 (new episode after window expiry), E-5/E-6 (different strike/expiry → separate episode), E-7 (next-day → new episode), E-8 (`_lookup_open_episode` error → fallback INSERT), E-9 (`strike`/`expiry` fields on both paths), E-10/E-11 (window boundary inclusive/exclusive).

### Acceptance Criteria
- [x] `flow_episodes` has exactly 1 row per same-session contract episode within the merge window
- [x] Subsequent qualifying print for open episode → PATCH (no new row); `trade_count` increments; `total_premium` accumulates; `signal_ts` updates
- [x] Print after window expiry → INSERT (new episode row)
- [x] `strike` and `expiry` correctly populated on both INSERT and PATCH paths
- [x] No debounce regression — `persist_flow_episode()` still called before SIG-DEBOUNCE
- [x] `_stats["created_episodes"]` and `_stats["merged_episodes"]` at module-level init; both in `/health/stream`
- [x] E-1 through E-11 full test matrix passing
- [x] No TODO comments in implementation code
- [x] No DB reads on the hot path — lookup is async, non-blocking


---


**Date:** 2026-05-04
**Severity:** P1 — every composite signal insert was rejected by Postgres when upstream emitted lowercase/mixed-case or aliased sentiment values (e.g. `"bullish"`, `"STRONG_BULLISH"`)
**PR:** [#72](https://github.com/bhaveshhpatel/cipher/pull/72) — squash merged 2026-05-04 (commit `8ad8ebc`)
**Branch:** `hotfix/signal-store-23514-sentiment-normalisation`
**Files:** `backend/services/signal_store.py`, `backend/tests/test_signal_store.py`

### Root Cause

`_build_row()` passed `sig["sentiment"]` raw to the `signal_history` row dict with no case normalisation and no constraint validation:

```python
# BEFORE (broken)
if sig.get("sentiment"):
    sentiment = sig["sentiment"]   # raw passthrough — no normalisation
```

The live DB CHECK constraint `signal_feed_log_sentiment_check` requires exactly one of:
```
BULLISH | BEARISH | NEUTRAL
```

If upstream emits `"bullish"` (lowercase), `"Bearish"` (mixed-case), `"STRONG_BULLISH"` (composite), or any other variant, Postgres rejects the row with `23514`. The same latent risk existed on `alert_level`: if `sig["alert_level"]` carried `"NORMAL"` (the column's pre-migration default value) or any string outside `CONVICTION | STRONG_SIGNAL | ALERT | WATCH`, the `signal_feed_log_alert_level_check` constraint would also reject the row.

**Note:** PR #71 (`fix/signal-store-constraint-defaults`) was opened as a first attempt to fix this. It was closed pre-merge during panel deliberation after schema verification confirmed it targeted 4 non-existent columns (`order_side`, `execution_mechanic`, `quote_source`, `strong_sentiment`) — consistent with `docs/ORDER_SIDE_RESOLUTION.md` (ING-001 ADR) which explicitly states `order_side` is not added to the schema. The real cause was identified via live schema query against `cipher-database`.

### Schema Verification (2026-05-04, `cipher-database / kpajucxqlrteckfuafvq`)

All 5 CHECK constraints on `signal_history`:

| Constraint | Allowed Values |
|---|---|
| `signal_feed_log_sentiment_check` | `BULLISH \| BEARISH \| NEUTRAL` |
| `signal_feed_log_alert_level_check` | `CONVICTION \| STRONG_SIGNAL \| ALERT \| WATCH` |
| `signal_feed_log_direction_check` | `BUY \| SELL \| HOLD` |
| `signal_feed_log_trade_type_check` | `SWEEP \| BLOCK \| SPLIT \| SINGLE` |
| `signal_feed_log_influence_tier_check` | `WHALE \| INSTITUTIONAL \| LARGE \| RETAIL` |

`direction`, `trade_type`, and `influence_tier` were already routed through normalisation helpers — safe. Only `sentiment` and `alert_level` had raw passthrough.

### Fix

**Two new normalisation helpers added:**

```python
def _normalise_sentiment(raw: str) -> str:
    """Routes raw upstream sentiment to a constraint-safe value."""
    if not raw:
        return "NEUTRAL"
    upper = raw.upper()
    if upper in _VALID_SENTIMENTS:          # BULLISH / BEARISH / NEUTRAL
        return upper
    if upper in ("BULL", "BULLISH_STRONG", "STRONG_BULLISH", "BUY"):
        return "BULLISH"
    if upper in ("BEAR", "BEARISH_STRONG", "STRONG_BEARISH", "SELL"):
        return "BEARISH"
    log.warning("[signal_store] unknown sentiment value %r -- defaulting to NEUTRAL", raw)
    return "NEUTRAL"

def _normalise_alert_level(raw: str) -> str:
    """Validates alert_level against live CHECK constraint; falls back to WATCH."""
    if not raw:
        return "WATCH"
    upper = raw.upper()
    if upper in _VALID_ALERT_LEVELS:        # CONVICTION / STRONG_SIGNAL / ALERT / WATCH
        return upper
    log.warning("[signal_store] unknown alert_level value %r -- defaulting to WATCH", raw)
    return "WATCH"
```

**`_build_row()` updated:**

```python
# AFTER (fixed)
if sig.get("sentiment"):
    sentiment = _normalise_sentiment(sig["sentiment"])   # normalised — no more raw passthrough

if sig.get("alert_level"):
    alert_level = _normalise_alert_level(sig["alert_level"])   # validated
```

**Module-level sets document the exact constraint values:**

```python
_VALID_SENTIMENTS   = {"BULLISH", "BEARISH", "NEUTRAL"}
_VALID_ALERT_LEVELS = {"CONVICTION", "STRONG_SIGNAL", "ALERT", "WATCH"}
```

### Tests Added (`backend/tests/test_signal_store.py`)

17 new test functions covering:
- `_normalise_sentiment`: valid uppercase passthrough, lowercase, mixed-case, bullish aliases (BULL, STRONG_BULLISH, BUY), bearish aliases (BEAR, STRONG_BEARISH, SELL), unknown → NEUTRAL, empty/None → NEUTRAL
- `_normalise_alert_level`: valid passthrough, lowercase, `"NORMAL"` → WATCH, unknown → WATCH, empty/None → WATCH
- `_build_row` integration: lowercase sentiment produces uppercase DB value, STRONG_BULLISH alias resolves to BULLISH, `alert_level="NORMAL"` remapped to WATCH, smoke test — all 5 constrained fields always within CHECK sets

### Acceptance Criteria
- [x] `_normalise_sentiment(raw)` → `BULLISH | BEARISH | NEUTRAL` added
- [x] `_normalise_alert_level(raw)` → `CONVICTION | STRONG_SIGNAL | ALERT | WATCH` added
- [x] `_build_row()` routes `sentiment` through `_normalise_sentiment()` — no raw passthrough
- [x] `_build_row()` routes `sig["alert_level"]` through `_normalise_alert_level()` — validated
- [x] `_VALID_SENTIMENTS` and `_VALID_ALERT_LEVELS` at module level — mirror live DB constraints
- [x] 17 QA test functions added, all passing
- [x] No schema changes, no new columns added to row dict
- [x] `docs/ORDER_SIDE_RESOLUTION.md` ING-001 ADR respected — `order_side` not touched

---
## ING-005 — Deep OTM Multiplier Default Changed to 1.0 (Registry Pre-Filter is Authoritative)

**Date:** 2026-05-03
**Severity:** P1 — silent misalignment; legitimate T1 prints at 12–20% OTM were incorrectly penalised with a 1.5× floor multiplier inconsistent with registry tier bands
**Branch:** `ing/s5-otm-threshold-align`
**Files:** `backend/signals/repetition_accumulator.py`

### Root Cause

`RepetitionAccumulator.__init__` defaulted `deep_otm_multiplier=1.5`. Gate 3 in `ingest_tick()` applied this multiplier whenever `_classify_otm()` returned `DEEP_OTM` (OTM% > 12%):

```python
# BEFORE — 1.5× penalty applied at accumulator for any OTM% > 12%
deep_otm_multiplier: float = 1.5
```

The registry's per-tier `atm_pct` bands allow up to ~20% OTM for T1. A T1 contract at 18% OTM legitimately passes the registry's T1 OTM filter — but the accumulator's hardcoded 12% threshold then applied a 1.5× penalty, requiring 1.5× more premium to qualify. Two problems:

1. **Inconsistent thresholds:** registry uses per-tier `atm_pct` (up to ~20% for T1); accumulator used a single hardcoded 12% cutoff for all tiers.
2. **Double-gating on the same axis:** the registry pre-filter is the correct OTM qualification layer. The accumulator penalty was compensating for the `underlying_price = 0` bug fixed by ING-004 — once ING-004 landed, the registry OTM filter works correctly and the accumulator penalty became redundant and incorrect.

### Deliberation Decisions (3-way panel, 2026-05-03)

**SA-Q1 — Option A chosen: retire deep OTM multiplier as a default**
- Option B (pass tier `atm_pct` into accumulator) rejected — layer inversion; accumulator would need to know registry tier internals.
- Option C (bump hardcoded `0.12` → `0.20`) rejected — lazy patch; still wrong for T2/T3 and doesn't fix the root issue.
- Option A is the correct architectural decision: the registry OTM pre-filter is the authoritative moneyness gate post-ING-004.

**SA-Q2 — Option B violates layer boundaries: confirmed rejected**

**PBE-Q1 — Code change scope: default only**
- Change `deep_otm_multiplier: float = 1.5` → `deep_otm_multiplier: float = 1.0`.
- Gate 3 logic block in `ingest_tick()` unchanged structurally — the `> 1.0` guard is never true at the new default, so it naturally reduces to the `else` branch (standard floor check) on every production tick.
- `_classify_otm()` static method retained — still used for episode enrichment and downstream signal metadata (ING-007 pattern scoring).
- Backward-compat: callers passing explicit `deep_otm_multiplier=1.5` still work exactly as before.

**PBE-Q2 — No changes to `OptionsFlowEvent` or `_DictEventWrapper`**
Option B rejected; no tier data needs to flow through event objects.

**QA-Q1 — Required regression test matrix (3 cases)**

| Case | Setup | Expected |
|---|---|---|
| E-1: T1 at 18% OTM | T1 ticker, DTE=5, strike 18% above underlying, total_premium=$60k (3 events × $20k) | Passes Gate 3 — no penalty applied. 60k ≥ T1 DTE≤7 floor $50k. |
| E-2: T2 at 14% OTM | T2 ticker, DTE=15, strike 14% OTM, total_premium=$110k | Passes Gate 3 — no penalty. 110k ≥ T2 DTE≤30 floor $100k. |
| E-3: T3 at 9% OTM | T3 ticker, DTE=60, strike 9% OTM, total_premium=$510k | Passes Gate 3 — no penalty. 510k ≥ T3 DTE≤90 floor $500k. |

All three cases were previously dropped by the 1.5× penalty. Must pass after this change.

**QA-Q2 — `test_classify_otm` tests: keep, update any asserting default penalty**
- `_classify_otm()` static method stays — its tests stay green unchanged.
- Tests using `RepetitionAccumulator()` with default args that assert deep OTM penalty behaviour must be updated: the default no longer applies a penalty. Tests explicitly passing `deep_otm_multiplier=1.5` continue to work.

**QA-Q3 — No new `/health/stream` counter required for this story**
- `_classify_otm()` still classifies; `otm_band` is still available on the episode for downstream use. No new stat counter in scope.

### Fix

```python
# AFTER — no penalty by default; registry pre-filter is authoritative
deep_otm_multiplier: float = 1.0   # ING-005: changed from 1.5 — see docstring
```

Gate 3 logic in `ingest_tick()` is structurally unchanged. With `deep_otm_multiplier=1.0`, the `> 1.0` guard is never true in production — Gate 3 effectively reduces to the standard DTE floor check on every tick.

### Acceptance Criteria
- [x] `deep_otm_multiplier` default changed from `1.5` to `1.0` in `RepetitionAccumulator.__init__`
- [x] Docstring updated: documents the ING-005 rationale and the explicit `> 1.0` opt-in path
- [x] Module-level docstring updated: ING-005 note added to S4 additions list
- [x] `_classify_otm()` docstring updated: ING-005 note clarifying retention for enrichment
- [x] `ingest_tick()` Gate 3 inline comment updated: documents the `> 1.0` no-op at new default
- [x] Test E-1, E-2, E-3 regression cases added/updated and passing
- [x] Existing `test_classify_otm` tests green (static method unchanged)
- [x] Tests asserting default deep OTM penalty updated to use explicit `deep_otm_multiplier=1.5`

---

## ING-003 — Cold-Start DTE Floor Bypass at Accumulator Instantiation

**Date:** 2026-05-03
**Severity:** P0 — data quality; low-quality short-DTE lottery tickets passed Gate 6 during cold-start window (~30 min)
**PR:** [#59](https://github.com/bhaveshhpatel/cipher/pull/59) — squash merged commit `62b159f`
**Files:** `backend/services/tradier_stream.py`, `backend/tests/test_ing003_dte_floors.py`

### Root Cause

`RepetitionAccumulator` was instantiated in `tradier_stream.py` with `dte_premium_tiers=None`:

```python
# BEFORE (broken)
accumulator = RepetitionAccumulator(
    window_minutes=30,
    min_trades=1,
    min_premium=10_000,
)
```

With `dte_premium_tiers=None`, `_get_episode_min_premium()` fell back to the flat `min_premium=$10,000` floor for all DTE buckets. This bypassed all DTE-stratified tier floors until registry warmup (~30 min) called `set_dte_premium_tiers()`. During cold-start:
- A `$12k 2-DTE` lottery ticket cleared the same floor as a `$500k 45-DTE` institutional print
- All unknown tickers defaulted to the flat $10k floor regardless of DTE

### Fix

Pass `_DEFAULT_DTE_PREMIUM_TIERS` at instantiation so DTE-stratified floors are active from tick 1:

```python
# AFTER (fixed)
from signals.repetition_accumulator import RepetitionAccumulator, _DEFAULT_DTE_PREMIUM_TIERS

accumulator = RepetitionAccumulator(
    window_minutes=30,
    min_trades=1,
    min_premium=10_000,
    dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS,
)
```

### Deliberation Decisions (3-way panel, 2026-05-03)

**SA-Q1 — Cold-start default tier: T1 (strictest)**
Decision: T1-default stands. Unknown tickers default to T1 until registry warmup confirms their tier. Safe direction is too strict, not too permissive. A $30k DTE=7 print dropped at cold-start is the borderline noise ING-003 is designed to eliminate — the suppression is doing work.

**SA-Q2 — T3-default rejected**
T3-default would pass everything during cold-start, defeating DTE tiers for the first 30 minutes.

**PBE-Q1 — Import safety confirmed**
`_DEFAULT_DTE_PREMIUM_TIERS` is a module-level dict constant — instantiated at import time. No function call, no class instantiation, no side effects.

**PBE-Q2 — `set_dte_premium_tiers()` override confirmed clean**
Post-warmup `set_dte_premium_tiers()` replaces `self.dte_premium_tiers` entirely under lock. No merging, no double-application. Clean atomic replace.

**QA-Q1 — Cold-start accumulator test (D-11, D-12)**
DTE=5, unknown ticker (T1 default, floor=$50k): $30k → None (D-11), $60k → RepetitionEpisode (D-12).

**QA-Q2 — Post-warmup tier override test (D-13)**
After `set_tier_map({"TESTTICKER": 2})`, DTE=5, $30k → RepetitionEpisode (T2 floor=$25k).

### No option choice required
This story had no Option A/B/C decision — it was a single unambiguous fix: pass the existing constant at instantiation.

---

## ALERT-LEVEL — `flow_episodes.alert_level` Always Written as `WATCH`

**Date:** 2026-04-28
**Severity:** High — every `flow_episodes` row was stored with `alert_level = WATCH` regardless of actual episode premium
**Files:** `backend/services/flow_store.py`, `backend/services/tradier_stream.py`

### Root Cause

`_bus_signal_listener()` in `flow_store.py` was building the `persist_flow_episode()` call with:

```python
"alert_level": sig.get("recommendation"),
```

`sig` is the `signal` dict inside the `composite_signal` bus message. `recommendation` is the composite engine field that returns `BUY`, `SELL`, or `HOLD` — completely unrelated to alert level. The `flow_episodes` table schema expects one of `CONVICTION`, `STRONG_SIGNAL`, `ALERT`, or `WATCH` (derived from cumulative episode premium).

Since `recommendation` does not match the alert level enum, Postgres silently accepted the value and stored it literally. Every episode row was persisted with `alert_level = WATCH`.

### Symptom

- `flow_episodes.alert_level` always `WATCH` in Supabase regardless of premium size
- Dashboard alert level badges all showed `WATCH`
- Filtering by `alert_level = CONVICTION` returned 0 rows

### Fix Applied

**`backend/services/tradier_stream.py`** — Added `alert_level` to the `composite_signal` `signal` sub-dict before bus publish:

```python
alert_level = accumulator.get_alert_level(sig_ep)

composite_msg = {
    "type": "composite_signal",
    "data": {
        "signal": {
            "ticker":         composite.ticker,
            "recommendation": composite.recommendation,
            ...
            "alert_level":    alert_level,   # ← injected here
            "reasoning":      composite.reasoning,
        },
        ...
    },
}
```

**`backend/services/flow_store.py`** — `_bus_signal_listener()` reads the correct field:

```python
# BEFORE (wrong — reads BUY/SELL/HOLD)
"alert_level": sig.get("recommendation"),

# AFTER (correct — reads CONVICTION/STRONG_SIGNAL/ALERT/WATCH)
"alert_level": sig.get("alert_level"),
```

### Alert Level Source of Truth

`accumulator.get_alert_level(ep)` in `signals/repetition_accumulator.py`:

| Premium | Level |
|---|---|
| ≥ $1,000,000 | `CONVICTION` |
| ≥ $500,000 | `STRONG_SIGNAL` |
| ≥ $200,000 | `ALERT` |
| < $200,000 | `WATCH` |

---

## DEDUP-KWARGS — `DedupCache.is_duplicate()` Raised `TypeError` on Every Tick

**Date:** 2026-04-28
**Severity:** High — deduplication completely broken in production; Layer 4 silently a no-op again
**Files:** `backend/services/tradier_stream.py`

### Root Cause

`DedupCache.is_duplicate()` in `utils/dedup.py` defines its first parameter as `event_or_occ_symbol` (positional). The call in `_process_trade()` passed it as a keyword argument:

```python
flow_dedup.is_duplicate(
    occ_symbol=occ_symbol,   # ← raises TypeError
    size=ev.size,
    ...
)
```

Python raised `TypeError: got an unexpected keyword argument 'occ_symbol'` on every tick. The outer try/except caught and dropped it silently — `_stats["deduped"]` stayed at 0 and all exchange copies of every trade were written to DB (same symptom as pre-C-019).

### Fix Applied

**`backend/services/tradier_stream.py`** — Pass `occ_symbol` positionally:

```python
# BEFORE
if flow_dedup.is_duplicate(occ_symbol=occ_symbol, size=ev.size, ...):

# AFTER
if flow_dedup.is_duplicate(occ_symbol, size=ev.size, ...):
```

---

## H4 — `_sweep_upgrade_dispatched` Set Never Evicted (Unbounded Memory Leak)

**Date:** 2026-04-28
**Severity:** Medium — memory grew unboundedly over a full trading day; also caused missed sweep upgrades for contracts reprinting after 30 min
**Files:** `backend/services/tradier_stream.py`

### Root Cause

`_sweep_upgrade_dispatched` was `Set[str]`. Keys were added via `.add()` and **never removed**. Over a full day with thousands of unique `occ|size|fill` keys, the set accumulated indefinitely.

Secondary correctness bug: if a contract reprinted after its 30-min episode window (a valid new episode), the stale key in the set would block the retroactive sweep upgrade from being dispatched.

### Fix Applied

**`backend/services/tradier_stream.py`**

Changed to `dict[str, float]` (key → wall-clock timestamp) with TTL eviction before each check:

```python
# BEFORE
_sweep_upgrade_dispatched: set = set()

# AFTER
_sweep_upgrade_dispatched: dict[str, float] = {}
_SWEEP_DISPATCH_TTL_S = 1800.0  # 30 min

# Before membership check:
now = _time.time()
stale = [k for k, ts in _sweep_upgrade_dispatched.items() if now - ts > _SWEEP_DISPATCH_TTL_S]
for k in stale:
    del _sweep_upgrade_dispatched[k]

if dispatch_key not in _sweep_upgrade_dispatched:
    _sweep_upgrade_dispatched[dispatch_key] = now
    asyncio.create_task(upgrade_to_sweep_in_db(...))
```

| Behaviour | Before H4 | After H4 |
|---|---|---|
| Memory over full trading day | Unbounded | Bounded to ~30 min of keys |
| Sweep re-dispatch after 30 min | Silently skipped | Correctly re-dispatched |

---

## Gate 2 — Accumulator Re-Emission Spam on Active QQQ/SPY Episodes

**Date:** 2026-04-28
**Severity:** Medium — high-volume tickers emitted a new `signal_history` and `flow_episodes` row on every tick after Gate 1 was crossed
**Files:** `backend/signals/repetition_accumulator.py`

### Root Cause

`ingest_tick()` returned the episode on every tick after Gate 1 (`trade_count >= 3` OR `total_premium >= $10k`) was crossed. For SPY/QQQ with heavy flow, this meant the downstream signal pipeline received a new emission at ~10–100 ticks/sec, writing a new row to `signal_history` and `flow_episodes` on every single tick.

### Fix Applied

**`backend/signals/repetition_accumulator.py`** — Added `last_signaled_premium: float = 0.0` to `RepetitionEpisode`.

Gate 2 added inside `ingest_tick()` after Gate 1:

```python
# Gate 2: re-emit only on first crossing or after >= $50k new premium
delta = ep.total_premium - ep.last_signaled_premium
if ep.last_signaled_premium == 0 or delta >= self.retrigger:
    ep.last_signaled_premium = ep.total_premium
    return ep
return None
```

Default `SIGNAL_RETRIGGER_THRESHOLD = $50,000`.

| Gate | Condition | Result |
|---|---|---|
| Gate 1 not crossed | `count < 3` AND `prem < $10k` | `None` — dropped |
| First Gate 1 crossing | Either threshold met, `last_signaled_premium == 0` | Episode returned |
| Re-emission | Δ `total_premium >= $50k` | Episode returned |
| Blocked re-emission | Δ < $50k | `None` — no new row |

---

## FLOW-DEBUG — Every Tick Drop Gate Silent in Railway Logs

**Date:** 2026-04-28
**Severity:** Observability — impossible to diagnose stream throughput; a dead stream looked identical to a healthy one
**Files:** `backend/services/tradier_stream.py`

### Root Cause

All drop gates in `_process_trade()` logged at `DEBUG` or were silent. Railway does not surface `DEBUG` by default. With no visible logging, a stream parsing 0 trades/sec and a healthy stream at 500 trades/sec were indistinguishable in the Railway log panel.

### Fix Applied

- `parse_tradier_trade() → None`: upgraded to `INFO` with symbol, size, bid, ask, last
- `accumulator.ingest_tick() → None`: upgraded to `INFO` with ticker, contract, premium, threshold
- Dedup hits: `DEBUG → INFO` with running count, occ_symbol, size, fill, exchange
- First 5 ticks individually at `INFO` (any type) — confirms WebSocket data arriving
- Non-timesale event types at `INFO` for first 10 distinct types seen, then `DEBUG`
- Periodic funnel summary every 100 ticks at `INFO`:
  ```
  [flow-funnel] ticks=100 parsed=87 parse_failed=13 deduped=42 classified=45 accumulator_gated=12 persisted=33 signals=8
  ```
- `_stats` extended: `parsed_count`, `accumulator_gated`, `parse_failed` — exposed on `/health/stream`

---

## U-1 — `options_universe_symbols` Duplicate Rows on Every Restart

**Date:** 2026-04-28
**Severity:** Medium — Railway restart inserted a fresh snapshot with duplicate OCC rows each time
**Files:** `backend/services/universe_store.py`
**Migration:** `backend/migrations/013_*.sql`

### Root Cause

`_sync_save_snapshot()` always generated a new `snapshot_id` (UUID) and inserted fresh rows on startup. No uniqueness constraint existed on `options_universe_symbols`, so Postgres accepted all duplicates. After N restarts the table contained N copies of every symbol.

### Fix Applied

**`backend/services/universe_store.py`** — Snapshot reuse logic:

```python
# Reuse if snapshot is < 20h old AND symbol count within ±10%
existing = _find_recent_snapshot(max_age_hours=20, symbol_count=len(symbols), tolerance=0.10)
if existing:
    snapshot_id = existing["id"]
else:
    snapshot_id = str(uuid4())
    _insert_new_snapshot(snapshot_id, len(symbols))
```

**`backend/migrations/013_*.sql`**:

```sql
ALTER TABLE options_universe_symbols
  ADD CONSTRAINT uq_universe_snapshot_symbol UNIQUE (snapshot_id, symbol);
```

Same constraint added to `chain_store` table.

---

## D-001 — Duplicate `build()` Call: Two Independent `SymbolRegistry` Instances at Startup

**Date:** 2026-04-28
**Severity:** High — doubled Tradier chain API calls; two registries with no shared state; doubled cold-start time
**Files:** `backend/services/tradier_stream.py`, `backend/main.py`

### Root Cause

`main.py` lifespan called `init_registry()` + `registry.build()`. Then `stream_options_flow()` also called `init_registry()` + `build()` internally — creating a second independent registry. Two full Tradier chain fetches (~31,920 symbols each), two `refresh_loop()` tasks running simultaneously, workers using the wrong registry.

### Fix Applied

**`backend/services/tradier_stream.py`** — `stream_options_flow()` accepts `registry=` from lifespan:

```python
async def stream_options_flow(symbols: list[str], registry=None):
```

When `registry` is provided: poll `registry.is_ready()` at 500ms intervals (30-min timeout max) — no `build()` call.
When `registry` is `None` (standalone/test): original inline build + `refresh_loop()` spawn.

**D-002 companion:** `refresh_loop()` only spawned by lifespan in production path — not inside `stream_options_flow()`.

---

## D-003 — Stream Worker Count Hard-Coded to 32 (Coverage Gaps for Large Universe)

**Date:** 2026-04-28
**Severity:** Medium — ~half the OCC symbol universe unstreamed with a 32-worker cap
**Files:** `backend/services/stream_manager.py`

### Root Cause

`StreamManager` spawned exactly 32 workers regardless of `registry.size()`. With ~31,920 OCC symbols at 500 symbols/worker, 64 workers are needed for full coverage. Hard-coding 32 left ~16,000 symbols unstreamed with no log warning.

### Fix Applied

**`backend/services/stream_manager.py`**:

```python
import math
worker_count = math.ceil(registry.size() / _CHUNK_SIZE)
# For 31,920 symbols at _CHUNK_SIZE=500 → 64 workers
```

Worker count logged at `INFO` on startup.

---

## B-008 — Stream Health Endpoint: `errors` / `reconnects` / `last_reconnect_at` Never Written

**Date:** 2026-04-25
**Severity:** Observability — `/health/stream` always returned `errors=0, reconnects=0, last_reconnect_at=null`
**Files:** `backend/services/stream_worker.py`, `backend/tests/test_stream_worker_b008.py`

### Root Cause

`_stats["errors"]`, `_stats["reconnects"]`, and `_stats["last_reconnect_at"]` in `tradier_stream.py` were declared at module level but never written. `StreamWorker` maintained local `self._errors` and `self._reconnects` counters that never propagated to the shared `_stats` dict. `get_stats()` read only its own `_stats`.

### Fix Applied

**`backend/services/stream_worker.py`** — Added two helpers with lazy import (avoids circular dependency):

```python
def _inc_global_error(self) -> None:
    try: _global_stats()["errors"] += 1
    except Exception: pass

def _inc_global_reconnect(self) -> None:
    try:
        s = _global_stats()
        s["reconnects"] += 1
        s["last_reconnect_at"] = _time.time()
    except Exception: pass
```

Wired at every error and reconnect site in `run()`. Tests: `test_stream_worker_b008.py` (5 tests, SW-01–05).

---

## B-023 — Unhandled 429 in `get_session_token()` Caused Crash-Loop

**Date:** 2026-04-25
**Severity:** Reliability — rate-limited token requests entered a crash loop, burning API budget
**Files:** `backend/utils/tradier_client.py`

### Root Cause

`get_session_token()` called `resp.raise_for_status()` without first checking for 429. Under load, Tradier returned 429s which became unhandled `httpx.HTTPStatusError`. Workers caught them as generic exceptions, slept `_backoff()`, then immediately re-requested tokens — re-triggering the 429.

### Fix Applied

Explicit 429 check before `raise_for_status()`. Reads `Retry-After` header (defaults to `_DEFAULT_RETRY_AFTER_S = 10.0`). Sleeps the correct window, then retries.

---

## B-022 — 32 Concurrent Session Token Requests at Startup

**Date:** 2026-04-25
**Severity:** Reliability — burst of simultaneous token POSTs triggered Tradier rate-limiter; workers failed to connect
**Files:** `backend/utils/tradier_client.py`

### Root Cause

All workers called `get_session_token()` at startup simultaneously. ~32 concurrent POSTs to `/v1/markets/events/session` exceeded Tradier's rate limit. Without B-023 handling, the 429s became crash loops.

### Fix Applied

`_SESSION_SEM = asyncio.Semaphore(3)` module-level. All `get_session_token()` calls serialized to max 3 concurrent. 32 workers batch through in `⌈32/3⌉ = 11` batches × ~400ms = **~4.4s total** one-time startup cost.

---

## B-021 — All Workers Started Simultaneously (Zero Stagger)

**Date:** 2026-04-25
**Severity:** Reliability — amplified B-022 by causing simultaneous semaphore contention at t=0
**Files:** `backend/services/stream_manager.py`, `backend/services/stream_worker.py`

### Fix Applied

Each worker receives `startup_delay_s = idx * 0.200`. Worker 0 starts at 0s, worker 31 at 6.2s. Reconnects do not re-apply this delay. `startup_delay_s` exposed in `worker.stats`. Tests: `TestB021StaggeredStartup` in `test_stream_manager.py` (7 tests).

---

## T-001 — Unit Test Suite: OCC Parser, Bid/Ask Classifier, Repetition Engine

**Date:** 2026-04-25
**Type:** Test coverage
**Files:** `tests/test_occ_parser.py` (40), `tests/test_classifier.py` (24), `tests/test_repetition_engine.py` (22)

Covers: full OCC parse paths, all 4 influence tiers, synthetic quote detection, golden sweep, DTE calc, `bid_ask_class` edge cases, all 4 trade type detections, Gate 1 / Gate 2 accumulator logic, all 4 alert levels, episode isolation across contracts.

**Grand total after T-001: ~250 tests.**

---

## C-020 — Tier Engine + Universe Tier Assignment

**Date:** 2026-04-25
**Severity:** Enhancement — no tier metadata; `backtest_score` OI factor defaulted to 0.5 for all symbols
**Files:** `backend/services/tier_engine.py` *(new)*, `backend/services/universe_store.py`, `backend/main.py`
**Migrations:** `010_add_tier_and_oi_to_universe.sql`, `011_add_tier_thresholds.sql`

New `TierEngine` class: loads thresholds from DB, assigns T1/T2/T3 per symbol volume, upserts back. Admin whitelist (SPY, QQQ, AAPL, TSLA, NVDA, MSFT, AMZN, META, GOOGL, AMD, PLTR, COIN) always forces T1. `set_tier_map()` builds in-memory lookup for signal pipeline. `load_tier_map()` added to `universe_store`.

| Tier | Label | Min Avg Volume |
|---|---|---|
| T1 | Liquid large-cap | ≥ 20M |
| T2 | Mid-cap | ≥ 2M |
| T3 | Standard (default) | ≥ 500K |

---

## C-019 — Layer 4 Dedup Inert + TTL Too Tight + Sweep Never Firing (5 Bugs)

**Date:** 2026-04-24
**Severity:** High — all dedup was a production no-op; sweeps never detected; premium inflated 2–4× in accumulator
**Files:** `backend/utils/dedup.py`, `backend/services/tradier_stream.py`

**Bug 1** — TTL 2s too tight for MIAX/PHLX lag (real lag: 500ms–5s). Changed to 5s.
**Bug 2** — `int(ts // 2)` time-bucket created gap at 2s boundary: straddle prints both passed as canonical. Replaced with pure `first_seen_ts` + TTL comparison.
**Bug 3** — Fill key `fill:.2f` too tight (exchange rounding). Changed to `fill:.1f`.
**Bug 4** — `flow_dedup` singleton never imported into `tradier_stream.py`. Layer 4 was a no-op from initial implementation.
**Bug 5** — `exchange` was never passed to `is_duplicate()` (defaulted to `""`). `set(["","",""])` length = 1 — sweep threshold (`>= 3`) never reached. Zero sweeps ever detected in production.

---

## C-018 — Synthetic Quotes Polluting `bid_ask_class` / `is_aggressive` Metrics

**Date:** 2026-04-24
**Severity:** Medium — data quality; backtesting aggression metrics skewed
**Fix:** Added `is_synthetic_quote: bool` to `OptionsFlowEvent`. Set `True` when `bid == 0 AND ask == 0`. Persisted to `flow_events` via migration `009_flow_events_synthetic_quote.sql`. Backtest queries should filter `WHERE is_synthetic_quote = false`.

---

## C-017 — Duplicate `flow_episodes` Rows Per Signal Episode

**Date:** 2026-04-24
**Severity:** Medium — 2× rows per episode
**Fix:** `_bus_signal_listener` in `flow_store.py` now writes `flow_episodes` ONLY on `composite_signal` events. Raw `signal` events are WebSocket-only, no DB write.

---

## C-016 — `UnboundLocalError` in `persist_flow_event()` After 100-Row Buffer

**Date:** 2026-04-24
**Severity:** High — every flow event write crashed when buffer hit 100 rows
**Fix:** Added `global _flow_event_buffer` declaration inside `persist_flow_event()`.

---

## C-015 — Stream Filter `trade` Delivering Equity Events Instead of Option Events

**Date:** 2026-04-23
**Severity:** Critical — all strike/expiry/bid/ask values were 0 in DB
**Fix:** Switched Tradier stream `filter` from `trade` to `timesale`.

---

## C-014 — Over-Aggressive Null Guards Silently Dropping Valid Trades

**Date:** 2026-04-23
**Severity:** Medium — trades with fill=0 or unknown contract type silently dropped
**Fix:** Removed `if fill == 0: return None`. Unknown `ctype` defaults to PUT instead of returning `None`.

---

## C-013 — Tradier Stream Envelope Not Unwrapped Before Parse

**Date:** 2026-04-23
**Severity:** High — `parse_tradier_trade()` received outer envelope dict, not inner trade payload
**Fix:** `_process_trade()` unwraps `raw[event_type]` before passing to parser.

---

## C-010 — `flow_episodes` Inserts Failing: 401 / RLS Policy Violation

**Date:** 2026-04-23
**Severity:** Critical — no flow episodes ever persisted to DB
**Root Cause:** `flow_store.py` fell back to `SUPABASE_KEY` (anon) when `SUPABASE_SERVICE_ROLE_KEY` was unset. Anon key is subject to RLS. All inserts rejected with `42501`. The fallback was silent — no error at startup.
**Fix:** Removed the anon key fallback. `_SUPABASE_KEY` reads only `SUPABASE_SERVICE_ROLE_KEY`. Startup warning made explicit if missing.

| Key | Env Var | RLS | Use |
|---|---|---|---|
| Anon / Public | `SUPABASE_KEY` | Enforced | Client-side reads only |
| Service Role | `SUPABASE_SERVICE_ROLE_KEY` | Bypassed | All server-side DB writes |

---

## C-009 — `universe_screener.py` OI Screener Replaced by Batch Quotes

**Date:** 2026-04-20
**Fix:** Replaced `screen_universe()` with `_fetch_batch_quotes()` in Step 3 of universe pipeline. `universe_screener.py` kept for reference, marked deprecated.

---

## C-008 — `stream_eligible` Column Missing from DB Migration

**Date:** 2026-04-20
**Severity:** High — `options_universe_symbols` upsert failing on missing column
**Fix:** Added `stream_eligible`, `last_price`, `volume` columns + index in `002_universe_symbols_quotes.sql`.

---

## C-007 — `config.py` Missing `priority_symbols` Property

**Date:** 2026-04-18
**Fix:** Added `@property priority_symbols` to `config.py` that splits the `UNIVERSE_PRIORITY_SYMBOLS` env var into `list[str]`.

---

## C-006 — `options_universe_snapshots.provider` NOT NULL with No Default

**Date:** 2026-04-18
**Severity:** High — every snapshot insert failed
**Fix:** Always pass `"tradier"` explicitly on insert.

---

## C-005 — supabase-py v2: `.select()` Not Available After `.insert()`

**Date:** 2026-04-17
**Severity:** High — snapshot ID could not be read back after insert
**Fix:** Generate `snapshot_id` via `uuid4()` in Python before insert; use directly without reading back from DB response.
