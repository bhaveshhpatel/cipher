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
| 1 | ~~**ING-002**~~ | ~~Hard per-event $10k premium floor at parser~~ | — | ✅ MERGED — 2026-05-03 (PR #58) |
| 2 | ~~**ING-003**~~ | ~~Wire `_DEFAULT_DTE_PREMIUM_TIERS` at accumulator init~~ | — | ✅ MERGED — 2026-05-03 (PR #59) |
| 3 | ~~**ING-004**~~ | ~~Fallback `underlying_price` from registry~~ | — | ✅ MERGED — 2026-05-03 (PR #60) |
| 4 | **ING-005** | Align OTM band thresholds registry ↔ accumulator | ING-004 | 🔄 IN PROGRESS — branch `ing/s5-otm-threshold-align` |
| 5 | **ING-006** | Directional aggression weighting on premium floor | ~~ING-001~~ resolved | ✅ UNBLOCKED — deliberation required |
| 6 | **ING-007** | Multi-day repeat window lookback (DB + cache) | ING-002, ING-003 | ✅ UNBLOCKED — deliberation required |
| 7 | **ING-008** | Volume vs. OI gate via registry injection | ING-004, ING-005 | After ING-005 merges + deliberation |

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
**PR:** [#58](https://github.com/bhaveshhpatel/cipher/pull/58) — ✅ **MERGED 2026-05-03** (commit `a38f837`)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-03)
**All three roles signed off. Story cleared for implementation.**

#### Deliberation Outcomes

**SA-Q1: Hardcoded vs. DB-driven floor — DECIDED: Hardcoded now, admin-configurable later**
- `_MIN_EVENT_PREMIUM = 10_000` defined at module level in `options_flow_parser.py`
- Floor is active at import time — no DB dependency, no cold-start gap
- Future path: when admin config page is built, wire through `ingestion_config` key `"min_event_premium"` with `10_000` as hardcoded cold-start fallback
- Follow-up story filed: **ING-002-CONFIG** (see below)
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

**QA-Q3: `"below_min_premium": 0` must be in `_stats` init block at module level**
- Key must exist before first tick arrives — no `KeyError` from `/health/stream` on cold start

#### Acceptance Criteria
- [x] `_MIN_EVENT_PREMIUM = 10_000` defined at module level in `options_flow_parser.py`
- [x] `parse_tradier_trade()` returns `"below_premium"` for `premium < 10_000`
- [x] Gate fires after `size == 0` guard, after `premium = fill * size * 100`, before OCC parsing and `OptionsFlowEvent` construction
- [x] Return type annotation updated to `Union[OptionsFlowEvent, Literal["below_premium"], None]`
- [x] `_stats["below_min_premium"]` initialised to `0` in module-level `_stats` dict
- [x] `_process_trade()` checks `result == "below_premium"` BEFORE `if not ev` / `parse_failed` branch
- [x] `_stats["below_min_premium"]` increments on sentinel — does NOT increment `parse_failed`
- [x] `"below_min_premium"` counter visible in `/health/stream` from first request
- [x] All 6 QA boundary test cases pass
- [x] All existing callers of `parse_tradier_trade()` in tests audited
- [x] All existing parse tests pass without modification
- [x] No regression in `_stats["parse_failed"]` behaviour for genuine parse errors

---

### ING-002-CONFIG — DTE Premium Tier Presets: Admin-Configurable via Named Presets
**Type:** Feature / Admin Configuration
**Priority:** P2 — quality of life; not blocking signal quality
**Estimated Effort:** 2.5 days
**Depends On:** ING-002 (merged ✅), ING-003 (DTE tiers wired ✅ before this story is needed)
**Files:**
- `backend/signals/repetition_accumulator.py` — add preset dicts + `_DEFAULT_PRESET` alias
- `backend/services/ingestion_config.py` — add `DTE_TIER_PRESET` + 8 custom floor keys to `_DEFAULTS` + `_EXPECTED_DB_KEYS`; add `get_dte_premium_tiers()` loader
- `backend/routers/admin.py` — 2 new endpoints: `GET/POST /api/admin/ingestion/dte-tiers`
- `backend/services/tradier_stream.py` — call `get_dte_premium_tiers()` at accumulator init + live reload path
- Supabase migration — insert 9 new rows into `ingestion_config` table
- Frontend: new admin panel card (separate frontend story; backend ships first)

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION
(See full question set in original spec — deliberation not yet started.)

---

### ING-003 — Wire `_DEFAULT_DTE_PREMIUM_TIERS` at Accumulator Instantiation
**Type:** Bug Fix / Configuration
**Priority:** P0
**Estimated Effort:** 0.25 day
**Depends On:** Nothing
**Files:** `backend/services/tradier_stream.py`
**PR:** [#59](https://github.com/bhaveshhpatel/cipher/pull/59) — ✅ **MERGED 2026-05-03** (commit `62b159f`)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-03)

**SA-Q1:** T1-default stands. Unknown tickers default to T1 until registry warmup. Safe direction is too strict, not too permissive.
**SA-Q2:** T3-default rejected — would pass everything during cold-start, defeating DTE tiers for 30 min.
**PBE-Q1:** `_DEFAULT_DTE_PREMIUM_TIERS` import safety confirmed — module-level dict constant, no side effects.
**PBE-Q2:** `set_dte_premium_tiers()` post-warmup override confirmed clean — atomic replace under lock, no merging.
**QA-Q1:** Cold-start test D-11/D-12: DTE=5 unknown ticker (T1 default $50k floor). $30k → None. $60k → episode.
**QA-Q2:** Post-warmup transition test D-13: after `set_tier_map({"TESTTICKER": 2})`, DTE=5, $30k → passes (T2=$25k).

#### Acceptance Criteria
- [x] Accumulator instantiated with `dte_premium_tiers=_DEFAULT_DTE_PREMIUM_TIERS`
- [x] Unit test: DTE=5, T1 ticker, premium=$30k pre-warmup → Gate 6 drops
- [x] Unit test: DTE=5, T1 ticker, premium=$60k pre-warmup → Gate 6 passes
- [x] Post-warmup `set_dte_premium_tiers()` still overrides correctly
- [x] No regression in existing accumulator tests

---

### ING-004 — Fallback `underlying_price` From Registry When Tick Has Zero
**Type:** Bug Fix
**Priority:** P0
**Estimated Effort:** 0.25 day
**Depends On:** Nothing
**Files:** `backend/parsers/options_flow_parser.py`, `backend/tests/test_ing004_underlying_price.py`
**Branch:** `ing/s4-underlying-price-fallback` (commit `327300d`)
**PR:** [#60](https://github.com/bhaveshhpatel/cipher/pull/60) — ✅ **MERGED 2026-05-03** (commit `d3c3f31`)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-03)
**All three roles signed off. Story merged.**

#### Deliberation Outcomes

**SA-Q1: Parser-layer coupling — DECIDED: Non-issue — add to existing enrichment block**
**SA-Q2: Cold-start log visibility — DECIDED: Single startup INFO log only; no per-tick warnings**
**SA-Q3: `stock_price()` vs `ContractMeta.underlying_price` — DECIDED: `reg.stock_price(ev.ticker)`**
**PBE-Q1: Hot-path safety — DECIDED: Safe — O(1) dict read, no IO, no lock, no await**
**PBE-Q2: Write-lock during concurrent `build()` — DECIDED: No additional locking needed**
**PBE-Q3: Exact placement — DECIDED: After meta block, inside same try/except**
**PBE-Q4: Counter initialisation — DECIDED: Module-level in `_stats` init block**
**QA-Q1:** Full test matrix D-1 through D-5 — all 5 cases required and passing.
**QA-Q2:** `/health/stream` visibility — automatic via existing `get_parser_stats()` wiring.
**QA-Q3:** Guard `if sp > 0` ensures zero-mutation for unknowns.
**QA-Q4:** Cold-start INFO log — no test required (infrastructure observability).

#### Acceptance Criteria
- [x] All criteria met — PR #60 merged.

---

### ING-005 — Align OTM Band Thresholds: Registry ↔ Accumulator
**Type:** Bug Fix / Consistency
**Priority:** P1
**Estimated Effort:** 1 day
**Depends On:** ING-004 ✅
**Files:** `backend/signals/repetition_accumulator.py`, `backend/tests/test_ing005_otm_thresholds.py`
**Branch:** `ing/s5-otm-threshold-align`

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-03)
**All three roles signed off. Story cleared for implementation.**

#### Deliberation Outcomes

**SA-Q1: Option chosen — Option A: Retire deep OTM multiplier as a default**

The registry's per-tier OTM filter is the correct place for OTM qualification. Post-ING-004, `underlying_price` is reliably populated and the registry OTM filter works correctly. The 1.5× accumulator penalty was compensating for the ING-004 bug. That bug is fixed. Applying a second penalty with a hardcoded 12% threshold that is inconsistent with the registry's per-tier `atm_pct` bands (up to ~20% for T1) is double-gating on the same axis with contradictory policy.

- **Option A:** Change `deep_otm_multiplier` default from `1.5` → `1.0`. Keep the param and the Gate 3 logic block for backward-compat — callers that explicitly pass `deep_otm_multiplier > 1.0` (e.g. backtesting) still work. Keep `_classify_otm()` static method — still used for episode enrichment and downstream signal metadata.
- **Option B** (pass tier `atm_pct` into accumulator) — **REJECTED**: layer inversion; accumulator would need to know registry tier internals.
- **Option C** (bump `0.12` → `0.20`) — **REJECTED**: lazy patch; still wrong for T2/T3; doesn't fix root issue.

**SA-Q2: Option B layer violation — CONFIRMED REJECTED**
Passing tier `atm_pct` from registry into accumulator creates signal layer → registry coupling. Not acceptable.

**PBE-Q1: Code change scope — default only, no structural changes**
- `deep_otm_multiplier: float = 1.5` → `deep_otm_multiplier: float = 1.0` in `__init__`
- Gate 3 block in `ingest_tick()` unchanged structurally — `> 1.0` guard is never true at the new default
- `_classify_otm()` retained as-is
- Scan for direct external calls to `_classify_otm()` — if none found outside accumulator, no further changes

**PBE-Q2: No `OptionsFlowEvent` or `_DictEventWrapper` changes**
Option B rejected. No tier data needs to flow through event objects.

**QA-Q1: Required regression test matrix — 3 cases, all must pass**

| Case | Setup | Expected |
|---|---|---|
| E-1: T1 at 18% OTM | T1 ticker, DTE=5, strike 18% OTM, total_premium=$60k | Passes — no penalty. 60k ≥ T1 DTE≤7 floor $50k |
| E-2: T2 at 14% OTM | T2 ticker, DTE=15, strike 14% OTM, total_premium=$110k | Passes — no penalty. 110k ≥ T2 DTE≤30 floor $100k |
| E-3: T3 at 9% OTM | T3 ticker, DTE=60, strike 9% OTM, total_premium=$510k | Passes — no penalty. 510k ≥ T3 DTE≤90 floor $500k |

**QA-Q2: `test_classify_otm` tests — keep, update any asserting default penalty**
- Static method tests stay green (method unchanged)
- Tests using default `RepetitionAccumulator()` and asserting deep OTM penalty must be updated — the default no longer applies a penalty
- Tests explicitly passing `deep_otm_multiplier=1.5` continue to work as-is

**QA-Q3: No new `/health/stream` counter required**
`_classify_otm()` still classifies; `otm_band` available on episode for downstream. No new stat counter in scope for this story.

#### Acceptance Criteria
- [x] `deep_otm_multiplier` default changed `1.5` → `1.0` in `RepetitionAccumulator.__init__`
- [x] All docstrings updated with ING-005 rationale (module, class, `_classify_otm`, `ingest_tick` Gate 3 comment)
- [ ] `backend/tests/test_ing005_otm_thresholds.py` created with E-1, E-2, E-3 cases
- [ ] All existing `test_classify_otm` tests green
- [ ] Tests asserting default deep OTM penalty updated to use explicit `deep_otm_multiplier=1.5`
- [ ] No regression in existing accumulator or stream tests
- [ ] PR opened targeting `main`

---

### ING-006 — Directional Aggression Weighting on Premium Floor
**Type:** Feature / Gate Enhancement
**Priority:** P1
**Estimated Effort:** 1 day
**Depends On:** ING-001 resolved ✅

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION

---

### ING-007 — Multi-Day Repeat Window Lookback (DB + Cache)
**Type:** Feature
**Priority:** P1
**Estimated Effort:** 2 days
**Depends On:** ING-002 ✅, ING-003 ✅

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION

---

### ING-008 — Volume vs. OI Gate via Registry Injection
**Type:** Feature / Gate Addition
**Priority:** P1
**Estimated Effort:** 1 day
**Depends On:** ING-004 ✅, ING-005 (after merge)

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION
