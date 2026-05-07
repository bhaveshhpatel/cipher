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
| ~~0~~ | ~~ING-001~~ | ~~Verify Tradier `order_side` field~~ | — | ✅ CLOSED — resolved pre-sprint |
| 1 | ~~**ING-002**~~ | ~~Hard per-event $10k premium floor at parser~~ | — | ✅ MERGED — 2026-05-03 (PR #58, commit `a38f837`) |
| 2 | ~~**ING-003**~~ | ~~Wire `_DEFAULT_DTE_PREMIUM_TIERS` at accumulator init~~ | — | ✅ MERGED — 2026-05-03 (PR #59, commit `62b159f`) |
| 3 | ~~**ING-004**~~ | ~~Fallback `underlying_price` from registry~~ | — | ✅ MERGED — 2026-05-03 (PR #60, commit `d3c3f31`) |
| 4 | ~~**ING-005**~~ | ~~Align OTM band thresholds registry ↔ accumulator~~ | ING-004 ✅ | ✅ CLOSED — 2026-05-03 (PR #61, commit `252d75f`) |
| 5 | ~~**ING-006**~~ | ~~Directional aggression weighting on premium floor~~ | ING-001 resolved ✅ | ✅ MERGED — 2026-05-04 (PR #62, commit `501b170`) |
| 6 | ~~**ING-009**~~ | ~~Same-session flow episode upsert/merge~~ | ING-002 ✅, ING-003 ✅, ING-006 ✅ | ✅ MERGED — 2026-05-06 (PR #76, commit `9ceee35`) — Issue [#75](https://github.com/bhaveshhpatel/cipher/issues/75) closed |
| 7 | ~~**ING-007**~~ | ~~Multi-day repeat window lookback (DB + cache)~~ | ING-002 ✅, ING-003 ✅, ING-006 ✅, ING-009 ✅ | ✅ MERGED — 2026-05-06 (PR #74, commit `b70d9b0`) — Issue [#70](https://github.com/bhaveshhpatel/cipher/issues/70) closed |
| 8 | ~~**ING-011**~~ | ~~ITM put/call misclassification fix~~ | ING-006 ✅, ING-007 ✅ | ✅ MERGED — 2026-05-07 (PR #81, commit `8d68ed1`) — Issue [#77](https://github.com/bhaveshhpatel/cipher/issues/77) closed |
| 8b | ~~**ING-011b**~~ | ~~`is_aggressive` moneyness-blindness inflates `weighted_premium` for ITM PUT AT_BID episodes~~ | ING-011 ✅ (moneyness band must exist on events) | ✅ MERGED — 2026-05-07 (PR #82, squash) — Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) closed |
| 9 | **ING-008** | Volume vs. OI gate via registry injection | ING-004 ✅, ING-005 ✅, **ING-011 ✅** | ⏳ Deliberation required — ING-011 blocker cleared 2026-05-07 |
| 10 | **ING-010** | Tier-aware min-premium floor + OI-relative bypass gate | ING-002 ✅, ING-003 ✅ | 🔴 Deliberation required — Issue [#78](https://github.com/bhaveshhpatel/cipher/issues/78) — parallel to ING-011/ING-008 |

---

## Post-ING-006-Merge Findings (GitHub Issues Filed)

The following issues were filed during ING-006 deliberation and are tracked separately.

| Issue | Title | Blocking? | Sprint Slot | Status |
|-------|-------|-----------|-------------|--------|
| [#63](https://github.com/bhaveshhpatel/cipher/issues/63) | Migrate `is_directionally_aggressive()` to dedicated aggression module | Not blocking ING-006 merge. Blocking ING-007/008 callers importing from `bid_ask_classifier.py` | SA-F1 post-ING-006 | 🟡 Open |
| [#64](https://github.com/bhaveshhpatel/cipher/issues/64) | SA-F1: Persist `is_aggressive` flag to `flow_events` for ING-007 pattern quality scoring | P1 — required before ING-007 pattern quality scoring is meaningful | ING-007 prerequisite | ✅ **CLOSED 2026-05-06** — superseded by #67/#69; delivered by [`012_catch_up_schema_delta.sql`](https://github.com/bhaveshhpatel/cipher/blob/main/supabase/migrations/012_catch_up_schema_delta.sql) |
| [#65](https://github.com/bhaveshhpatel/cipher/issues/65) | ING-007 story issue — Multi-day repeat window lookback | — | ING-007 | ✅ CLOSED |
| [#66](https://github.com/bhaveshhpatel/cipher/issues/66) | ING-006 SA-F1: Migrate `is_directionally_aggressive()` out of `bid_ask_classifier.py` | Not blocking ING-006. Blocking for ING-007/008 signal-layer import path | SA-F1 post-ING-006 | 🟡 Open |
| [#67](https://github.com/bhaveshhpatel/cipher/issues/67) | ING-007 prereq (SA-F2): Add `is_aggressive` boolean column to `flow_events` | **Blocking for ING-007 and S8 backtest work** | ING-007 S2.5 migration | ✅ **CLOSED 2026-05-06** — delivered by [`012_catch_up_schema_delta.sql`](https://github.com/bhaveshhpatel/cipher/blob/main/supabase/migrations/012_catch_up_schema_delta.sql) |
| [#68](https://github.com/bhaveshhpatel/cipher/issues/68) | SA-F1 shim removal — `is_aggressive()` deprecated shim in `bid_ask_classifier.py` | Coordinate with #63/#66 | Post ING-006 cleanup | 🟡 Open |
| [#69](https://github.com/bhaveshhpatel/cipher/issues/69) | Add `flow_events.is_aggressive` column + `persist_flow_episode` serialisation (S2.5 migration) | **Blocking production deploy** | ING-007 S2.5 | ✅ **CLOSED 2026-05-06** — delivered by [`012_catch_up_schema_delta.sql`](https://github.com/bhaveshhpatel/cipher/blob/main/supabase/migrations/012_catch_up_schema_delta.sql) |
| [#70](https://github.com/bhaveshhpatel/cipher/issues/70) | ING-007: Multi-day repeat window lookback + is_aggressive DB column | — | ING-007 canonical issue | ✅ CLOSED 2026-05-06 |
| [#75](https://github.com/bhaveshhpatel/cipher/issues/75) | ING-009: Same-session flow episode upsert/merge | ✅ MERGED PR #76 2026-05-06 | ING-009 canonical issue | ✅ CLOSED |

---

## Schema Drift Resolution — 2026-05-06

**Migration:** [`supabase/migrations/012_catch_up_schema_delta.sql`](https://github.com/bhaveshhpatel/cipher/blob/main/supabase/migrations/012_catch_up_schema_delta.sql) — committed 2026-05-06 (commit `33aaed2`).

All DDL in `012` was applied directly to the live Supabase project (`cipher-database`, ref: `kpajucxqlrteckfuafvq`) after migration `011_unique_constraints.sql` without a corresponding migration file. The `012` catch-up migration closes the repo schema drift so that fresh environments, branches, and resets reproduce the exact live schema.

**Columns codified in `flow_events`:**

| Column | Type | Default | Origin |
|--------|------|---------|--------|
| `is_aggressive` | `BOOLEAN NOT NULL` | `false` | ING-006 / S2.5 (#67, #69) |
| `is_golden_sweep` | `BOOLEAN NOT NULL` | `false` | ING-005 golden sweep gate |
| `occ_symbol` | `TEXT` | `NULL` | Contract identity string |
| `is_synthetic_quote` | `BOOLEAN NOT NULL` | `false` | Quote provenance |
| `quote_source` | `TEXT NOT NULL` | `'live'` | Quote provenance |
| `miax_normalized_exchange_count` | `INTEGER NOT NULL` | `1` | MIAX dedup normalization |
| `order_side` | `TEXT NOT NULL` | `'UNKNOWN'` | ING-006 order_side_classifier |
| `strong_sentiment` | `BOOLEAN NOT NULL` | `false` | ING-006 high-conviction flag |
| `execution_mechanic` | `TEXT NOT NULL` | `'AMBIGUOUS_LONG'` | ING-006/007 mechanic enum |

**Columns codified in `flow_episodes`:**

| Column | Type | Default | Origin |
|--------|------|---------|--------|
| `is_multi_day_repeat` | `BOOLEAN NOT NULL` | `false` | S12 dual-window engine |
| `is_aggressive` | `BOOLEAN NOT NULL` | `false` | ING-006 episode-level aggression |

**Indexes added:**
- `idx_flow_events_is_aggressive` on `(ticker, is_aggressive, created_at DESC)` — ING-007 lookback stratification
- `idx_flow_events_contract_day` on `(ticker, contract_type, strike, expiry, created_at DESC)` — ING-007 compound lookback
- `idx_flow_events_order_side` on `(ticker, order_side, created_at DESC)` — order-side classifier queries
- `idx_flow_episodes_is_aggressive` on `(ticker, is_aggressive, created_at DESC)` — S8 backtest stratification

**Issues closed by this migration:** [#64](https://github.com/bhaveshhpatel/cipher/issues/64) (superseded), [#67](https://github.com/bhaveshhpatel/cipher/issues/67), [#69](https://github.com/bhaveshhpatel/cipher/issues/69)

---

## Post-ING-009-Merge Findings (GitHub Issues Filed)

| Issue | Title | Blocking? | Sprint Slot |
|-------|-------|-----------|-------------|
| Filed post-merge | `_lookup_open_episode` exception branch has no isolated unit test — E-8 test mocks the entire function; internal `except` block not independently exercised | Not blocking ING-007 | Post ING-009 cleanup |

---

## Post-ING-009 Live Session Findings (2026-05-06)

Identified during live market monitoring on 2026-05-06 after ING-009 merged.

| Issue | Title | Blocking? | Sprint Slot |
|-------|-------|-----------|-------------|
| [#77](https://github.com/bhaveshhpatel/cipher/issues/77) | BUG: Deeply ITM puts misclassified as REPEAT_SELL (bullish) — AT_BID logic ignoring intrinsic value | **Blocked ING-008** — ✅ resolved by ING-011 MERGED 2026-05-07 | ING-011 — ✅ MERGED PR #81 |
| [#78](https://github.com/bhaveshhpatel/cipher/issues/78) | ING-010: Tier-aware min-premium floor + OI-relative bypass gate — small-cap flow (GDYN, PENG) silently dropped at `belowminpremium` | Not blocking ING-011 or ING-008. Parallel track. Deliberation required. | ING-010 |
| [#80](https://github.com/bhaveshhpatel/cipher/issues/80) | ING-011b: `is_aggressive` moneyness-blindness inflates `weighted_premium` for ITM PUT AT_BID episodes | Not blocking ING-011 merge. Follow-on to ING-011. Deliberation COMPLETE 2026-05-06 — Option B selected. | ING-011b — ✅ MERGED PR #82 2026-05-07 — Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) closed |

---

## Post-ING-007-Merge Findings (GitHub Issues Filed)

| Issue | Title | Blocking? | Sprint Slot |
|-------|-------|-----------|-------------|
| SA-F1 (PR #74) | `main.py` comment: `registry.accumulator` may be `None`; hot-path accumulator is module-level in `tradier_stream.py` | Not blocking | Post ING-007 cleanup |
| SA-F2 (PR #74) | `ing007_s2_5_contract_day_index.sql` comment: EXPLAIN ANALYZE results are for `flow_events`; `get_contract_prior_days` queries `flow_episodes` — distinct tables | Not blocking | Post ING-007 cleanup |
| QA-F1 (PR #74) | `test_fetch_from_db_exception_returns_zero_result` — asserts `httpx.RequestError` returns `prior_days_active=0`, does not propagate | Not blocking | Post ING-007 cleanup |
| QA-F2 (PR #74) | `test_multi_day_min_days_boundary_at_exactly_2` — `prior_days_active=1 → False`, `prior_days_active=2 → True` at `min_days=2`; protects against `>` vs `>=` regression | Not blocking | Post ING-007 cleanup |
| PBE-F4 (PR #74) | Apex S6 `Composite`/`CompositeScore`/`build_composite` additions shipped in ING-007 PR to unblock signal-bus wiring — out of ING-007 scope; unblocked by ING-009 merge ✅ | ✅ Unblocked | Signal layer follow-up |

---

## Post-ING-011-Merge Findings (GitHub Issues Filed)

| Issue | Title | Blocking? | Sprint Slot |
|-------|-------|-----------|-------------|
| [#80](https://github.com/bhaveshhpatel/cipher/issues/80) | ING-011b: `is_aggressive` moneyness-blindness inflates `weighted_premium` for ITM PUT AT_BID episodes | Not blocking ING-008. Deliberation COMPLETE 2026-05-06 — Option B selected. | ING-011b — ✅ MERGED PR #82 2026-05-07 — Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) closed |

---

## Post-ING-011b-Merge Findings (GitHub Issues Filed)

| Issue | Title | Blocking? | Sprint Slot |
|-------|-------|-----------|-------------|
| D4 accepted gap | `prior_days_aggressive` in `flow_events.is_aggressive` is stamped at parse time (moneyness-blind). ITM PUT AT_BID buyers are counted as aggressive in the DB column. File a follow-up story if multi-day aggression metrics prove materially skewed after 5+ trading days of live data under ING-007. | Not blocking ING-008 or ING-010 | Post ING-011b monitoring |

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

---

### ING-003 — Wire `_DEFAULT_DTE_PREMIUM_TIERS` at Accumulator Init
**Type:** Bug Fix / Wiring
**Priority:** P0
**GitHub Issue:** [#59](https://github.com/bhaveshhpatel/cipher/issues/59)
**PR:** [#59](https://github.com/bhaveshhpatel/cipher/pull/59) — ✅ **MERGED 2026-05-03** (commit `62b159f`)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-03)

---

### ING-004 — Fallback `underlying_price` from Registry
**Type:** Bug Fix
**Priority:** P0
**GitHub Issue:** [#60](https://github.com/bhaveshhpatel/cipher/issues/60)
**PR:** [#60](https://github.com/bhaveshhpatel/cipher/pull/60) — ✅ **MERGED 2026-05-03** (commit `d3c3f31`)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-03)

---

### ING-005 — Align OTM Band Thresholds Registry ↔ Accumulator
**Type:** Threshold Alignment
**Priority:** P0
**GitHub Issue:** [#61](https://github.com/bhaveshhpatel/cipher/issues/61)
**PR:** [#61](https://github.com/bhaveshhpatel/cipher/pull/61) — ✅ **MERGED 2026-05-03** (commit `252d75f`)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-03)

**Key Decision — `deep_otm_multiplier` 1.5 → 1.0:**
- `deep_otm_multiplier` set to `1.0` — delegates OTM filtering entirely to `SymbolRegistry` pre-filter
- **Dependency documented:** Gate 3 OTM filtering is now fully dependent on `SymbolRegistry.atm_pct` band accuracy. If registry bands are misconfigured or stale, Gate 3 provides no backstop. This dependency is intentional — document explicitly in code.
- `ep.otm_band` wiring deferred to ING-007 (see QA: otm_band wiring below)

---

### ING-006 — Directional Aggression Weighting on Premium Floor
**Type:** Feature / Signal Enhancement
**Priority:** P0
**GitHub Issue:** [#62](https://github.com/bhaveshhpatel/cipher/issues/62)
**PR:** [#62](https://github.com/bhaveshhpatel/cipher/pull/62) — ✅ **MERGED 2026-05-04** (commit `501b170`)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-03)

**Key Decisions:**
- `is_directionally_aggressive(bid_ask_class, contract_type)` replaces `is_aggressive(trade_type)` shim — `AT_BID`/`BELOW_BID` now correctly flags PUT **and** CALL as directional
- `RepetitionEpisode.weighted_premium` property added — Gate 2 and Gate 3 evaluate weighted premium, not total premium
- `_AGGRESSION_DISCOUNT = 0.5` hardcoded; wire through `ingestion_config` deferred to ING-002-CONFIG
- New test file `test_ing006_directional_aggression.py` — F-matrix cases F-1 through F-8 + weighted premium boundary cases

---

### ING-009 — Same-Session Flow Episode Upsert/Merge

**Type:** Bug Fix / Data Model Correctness
**Priority:** P0
**Estimated Effort:** 1 day
**Depends On:** ING-002 ✅, ING-003 ✅, ING-004 ✅, ING-005 ✅, ING-006 ✅
**Blocks:** ING-007 — `get_contract_prior_days()` depends on `flow_episodes` being correctly aggregated
**Files:**
- `backend/services/flow_store.py` — `persist_flow_episode()` upsert logic + new `_stats` counters
- `backend/tests/test_ing009_episode_upsert.py` — NEW: full test matrix
**GitHub Issue:** [#75](https://github.com/bhaveshhpatel/cipher/issues/75) — ✅ CLOSED 2026-05-06
**Branch:** `ing/s9-episode-upsert`
**PR:** [#76](https://github.com/bhaveshhpatel/cipher/pull/76) — ✅ **MERGED 2026-05-06** (squash commit `9ceee35`)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-05)
#### ✅ Pre-Merge Panel Deliberation — COMPLETE (2026-05-06)
**SA verdict:** PASS. **PBE verdict:** PASS. **QA verdict:** PASS — E-1 through E-11 full matrix present and passing.

#### Problem

`flow_episodes` was insert-only from the Signal Gate path, creating one new row per qualifying print instead of one row per logical same-session episode. This made `flow_episodes` a near-duplicate of `flow_events` (26,906 vs 28,373 rows on 2026-05-05) rather than an aggregated episode table.

#### Deliberation Outcomes

**SA — DECIDED: Fix data model semantics, not the gate threshold**
- `flow_events` = every qualifying classified tick
- `flow_episodes` = one aggregated episode per contract per same-session window
- Keep gate order: Signal Gate → `persist_flow_episode()` → SIG-DEBOUNCE.
- Fix belongs in episode upsert/merge semantics inside `persist_flow_episode()` in `flow_store.py`

**PBE — DECIDED: Insert-or-update keyed on contract identity + session window**
- Episode merge key: `(ticker, direction, contract_type, strike, expiry)`
- On match → PATCH: `trade_count += 1`, `total_premium += new_premium`, `signal_ts = new_ts`
- On no match → INSERT (existing path unchanged)

**QA — DECIDED: Full boundary test matrix required**
- Must prove: two qualifying prints for same contract within window → one row, `trade_count = 2`
- Window edge boundary tests required: at `_EPISODE_MERGE_WINDOW_S`, at `_EPISODE_MERGE_WINDOW_S + 1s`

#### Acceptance Criteria

- [x] `flow_episodes` has exactly 1 row per same-session contract episode within the merge window
- [x] A subsequent qualifying print for an open episode updates the existing row — no new row inserted
- [x] `trade_count` increments by 1 on each merge
- [x] `total_premium` accumulates on merge
- [x] `signal_ts` updates to the latest qualifying print timestamp on merge
- [x] `strike` and `expiry` remain correctly populated from the raw signal path (EPISODE-FIX preserved)
- [x] No debounce regression — `persist_flow_episode()` still called before SIG-DEBOUNCE
- [x] ING-007 `get_contract_prior_days()` query is unaffected (separate concern)
- [x] Tests: first print (insert), second print within window (merge), print after window expiry (new episode), next-day (new episode)
- [x] Boundary tests: merge window edge cases
- [x] `_stats["created_episodes"]` and `_stats["merged_episodes"]` initialised at module level
- [x] Both counters visible in `/health/stream` from cold start
- [x] No TODO comments in implementation code
- [x] No DB reads on the hot path — lookup is async, does not block the stream tick

#### QA Test Matrix

| Case | Description | Expected |
|---|---|---|
| E-1 | First qualifying print for contract | INSERT — new episode row, `trade_count=1` |
| E-2 | Second qualifying print, same contract, within window | PATCH — same row, `trade_count=2`, `total_premium` accumulated |
| E-3 | Third qualifying print, same contract, within window | PATCH — same row, `trade_count=3` |
| E-4 | Print for same contract after window expiry | INSERT — new episode row, `trade_count=1` |
| E-5 | Print for different strike, same ticker | INSERT — new episode row (different key) |
| E-6 | Print for different expiry, same strike+ticker | INSERT — new episode row (different key) |
| E-7 | Next-day print for same contract | INSERT — new episode row (ING-007 repeat flag independent) |
| E-8 | `_lookup_open_episode` Supabase error → fallback to INSERT | INSERT — episode not lost on lookup failure |
| E-9 | `strike`/`expiry` correctly populated on both INSERT and PATCH paths | Both fields non-null |
| E-10 | Window boundary — print at exactly `_EPISODE_MERGE_WINDOW_S` | PATCH (inclusive boundary) |
| E-11 | Window boundary — print at `_EPISODE_MERGE_WINDOW_S + 1s` | INSERT (new episode) |

---

### ING-007 — Multi-Day Repeat Window Lookback (DB + Cache)
**Type:** Feature / Signal Enhancement
**Priority:** P0
**Estimated Effort:** 2 days
**Depends On:** ING-002 ✅, ING-003 ✅, ING-006 ✅, ING-009 ✅
**Files:**
- `backend/utils/contract_day_cache.py` — NEW
- `backend/signals/repetition_accumulator.py`
- `backend/services/flow_store.py`
- `backend/services/tradier_stream.py`
- `supabase/migrations/` — S2.5 migration
- `backend/tests/test_ing007_multiday_lookback.py` — NEW
**GitHub Issue:** [#70](https://github.com/bhaveshhpatel/cipher/issues/70) — ✅ CLOSED 2026-05-06
**Branch:** `ing/s7-multiday-repeat`
**PR:** [#74](https://github.com/bhaveshhpatel/cipher/pull/74) — ✅ **MERGED 2026-05-06** (commit `b70d9b0`)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-04)
#### ✅ Pre-Merge Panel Deliberation — COMPLETE (2026-05-06)
**SA verdict:** PASS — SA-F1/SA-F2 comments added. Queue max corrected 500→5000.
**PBE verdict:** PASS — sync threshold reconciled to `accumulator._multi_day_min_days` (PBE-1), `_lookback_result_cache` eviction wired (PBE-3).
**QA verdict:** PASS — G-1 through G-8 passing, TTL expiry test added, p99 < 5ms confirmed, otm_band wiring verified.

#### ING-007 Acceptance Criteria

- [x] S2.5 migration runs cleanly — index, `order_side` column, `is_aggressive` column all present in `flow_events`
- [x] `EXPLAIN ANALYZE` confirms index hit on lookback query
- [x] `contract_day_cache.py` created in `backend/utils/`
- [x] Cache key is 4-tuple `(ticker, contract_type, strike, expiry)`
- [x] `get_lookback()` returns cached result within TTL (300s); re-fetches after expiry
- [x] DB query uses `DATE_TRUNC('day', NOW())` ceiling — today excluded
- [x] DB query passes DTE-tier premium floor as parameter — no hardcoded floor
- [x] `RepetitionEpisode` gains: `prior_days_active`, `prior_days_aggressive`, `is_multi_day_repeat`, `otm_band`
- [x] `RepetitionAccumulator` gains: `require_multi_day: bool = False`, `multi_day_min_days: int = 2`
- [x] `is_multi_day_repeat = prior_days_active >= multi_day_min_days`
- [x] Background `asyncio.Queue` worker — `maxsize=5000`
- [x] `_stats["lookback_queue_overflow"]` initialised at module level; never propagates as unhandled exception
- [x] All 8 fixture cases G-1 through G-8 pass
- [x] TTL expiry test passes
- [x] Latency benchmark: p99 < 5ms
- [x] `otm_band` wiring verified
- [x] No new dependencies added unless `cachetools` already present

---

### ING-011 — ITM Put/Call Misclassification Fix

**Type:** Bug Fix / Signal Correctness
**Priority:** P0
**Estimated Effort:** 1 day
**Depends On:** ING-006 ✅, ING-007 ✅ (`otm_band` field established on `RepetitionEpisode`)
**Blocks:** ING-008 — OI gate logic depends on correct directional classification
**Does NOT block:** ING-010 (parallel track)
**Files:**
- `backend/signals/repetition_accumulator.py` — `_classify_moneyness_band()` + `dominant_direction` ITM override
- `backend/tests/test_ing011_itm_classification.py` — NEW: 34 tests across 3 classes
**GitHub Issue:** [#77](https://github.com/bhaveshhpatel/cipher/issues/77) — ✅ CLOSED 2026-05-07
**Branch:** `ing/s11-itm-classification`
**PR:** [#81](https://github.com/bhaveshhpatel/cipher/pull/81) — ✅ **MERGED 2026-05-07** (commit `8d68ed1`)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-06)
#### ✅ Pre-Merge Panel Deliberation — COMPLETE (2026-05-06)
**SA verdict:** PASS — SA-F1 (`_majority_itm_band()` UNKNOWN-tick suppression) resolved with test I-12 added inline.
**PBE verdict:** PASS.
**QA verdict:** PASS — QA-F1 (test I-12 added), QA-F3 (I-8 docstring clarified). All 34 tests passing.

#### Deliberation Outcomes

**D1 — ITM threshold — DECIDED: Reuse ING-005 ATM band exactly**
- `_ITM_THRESHOLD = 0.02` — symmetric with ING-005 ATM ±2% band
- `_DEEP_ITM_THRESHOLD = 0.10`
- Thresholds apply symmetrically to puts and calls (PUT: strike > underlying × (1 + threshold); CALL: strike < underlying × (1 − threshold))

**D2 — Override scope — DECIDED: PUT-only override; applies to ALL ITM (not just DEEP_ITM)**
- Override applies to `otm_band in ('ITM', 'DEEP_ITM')` for PUT contracts
- ITM CALL AT_BID is unchanged — call seller at bid = bearish, already correct
- Rationale: mildly ITM put AT_BID buyers are economically meaningful enough to override; DEEP_ITM-only cutoff misses legitimate institutional hedges in the 2–10% ITM band

**D3 — `otm_band` extension — DECIDED: Extend in-place; no DB migration required**
- `otm_band` TEXT column extended to cover `DEEP_ITM | ITM | ATM | OTM | DEEP_OTM | UNKNOWN`
- No DB migration required — column is TEXT, no enum constraint
- `_classify_otm()` replaced by `_classify_moneyness_band()` — full spectrum classification

#### Problem (recorded for posterity)

Deeply ITM puts filling `AT_BID` were classified as `REPEAT_SELL` (put selling = bullish) when the correct economic read is bearish put buying. The bid/ask classification logic was correct for OTM puts but broke for ITM contracts where the fill price reflects intrinsic value, not directional seller intent.

**Live example — TMDX 2026-05-06 13:43:50 UTC:**
- PUT $105 · May 15 · underlying price $75.69 · size 1,263 · fill $27.68 · bid $26.70 · ask $29.50
- `bid_ask_class = AT_BID` → system classified as `REPEAT_SELL` → `direction = REPEAT_SELL` (bullish)
- Actual: strike ($105) is ~39% above underlying ($75.69) — deeply ITM put buyer, economically bearish
- Episode emitted as `CONVICTION` bullish signal — **incorrect; fixed by this PR**

#### Acceptance Criteria

- [x] D1, D2, D3 deliberations resolved and documented inline (SA + PBE + QA sign-off)
- [x] `_classify_moneyness_band()` replaces `_classify_otm()` — full spectrum: `DEEP_ITM | ITM | ATM | OTM | DEEP_OTM | UNKNOWN`
- [x] `_ITM_THRESHOLD = 0.02` and `_DEEP_ITM_THRESHOLD = 0.10` defined at module level
- [x] `dominant_direction` for ITM/DEEP_ITM puts with `bid_side_prem > ask_side_prem` resolves to `REPEAT_BUY` (bearish)
- [x] ITM CALL AT_BID behaviour unchanged
- [x] TMDX $105P scenario produces correct `BEARISH` direction
- [x] Existing OTM put `AT_BID` → `REPEAT_SELL` (bullish) behaviour unchanged
- [x] `underlying_price == 0` fallback: `UNKNOWN` band, no classification attempted
- [x] 34 tests across `TestClassifyMoneynessBand`, `TestITMDirectionOverride`, `TestThresholdConstants` — all passing
- [x] No DB migration required — `otm_band` TEXT column extended in-place

#### QA Test Matrix

| Case | Contract | `bid_ask_class` | `otm/itm_band` | Expected direction | Expected sentiment |
|---|---|---|---|---|---|
| I-1 | OTM PUT | AT_BID | OTM | REPEAT_SELL | BULLISH (put seller — unchanged) |
| I-2 | ITM PUT | AT_BID | ITM | REPEAT_BUY | BEARISH (buyer paying intrinsic) |
| I-3 | DEEP_ITM PUT | AT_BID | DEEP_ITM | REPEAT_BUY | BEARISH (institutional hedge) |
| I-4 | ITM PUT | AT_ASK | ITM | REPEAT_BUY | BEARISH (aggressive buyer — already correct) |
| I-5 | ITM CALL | AT_ASK | ITM | REPEAT_BUY | BULLISH (already correct) |
| I-6 | ITM CALL | AT_BID | ITM | REPEAT_SELL | BEARISH (call seller — unchanged) |
| I-7 | ATM PUT | AT_BID | ATM | REPEAT_SELL | BULLISH (ATM selling — unchanged) |
| I-8 | `underlying_price == 0` | any | UNKNOWN | no classification attempted | existing fallback preserved |

---

### ING-011b — `is_aggressive` Moneyness-Blindness Inflates `weighted_premium` for ITM PUT AT_BID Episodes

**Type:** Bug Fix / Signal Correctness
**Priority:** P1
**Estimated Effort:** 0.5–1 day
**Depends On:** ING-011 ✅ (moneyness band classification must exist on events before aggression fix is meaningful)
**Does NOT block:** ING-008, ING-010 (parallel tracks)
**Filed:** 2026-05-06 — annotation commit [`a82f3967`](https://github.com/bhaveshhpatel/cipher/commit/a82f3967fb37a07af180caadefbbcb50e041aae2)
**Files:**
- `backend/signals/repetition_accumulator.py` — `get_weighted_premium()` + promoted `_classify_moneyness_band()` to module-level function
- `backend/tests/test_ing011b_itm_aggression_weight.py` — NEW: full test matrix (W-1 through W-12)
**GitHub Issue:** [#80](https://github.com/bhaveshhpatel/cipher/issues/80) — ✅ CLOSED 2026-05-07
**Branch:** `ing/s11b-itm-aggression-weight`
**PR:** [#82](https://github.com/bhaveshhpatel/cipher/pull/82) — ✅ **MERGED 2026-05-07** (squash)

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-06)
#### ✅ Pre-Merge Panel Deliberation — COMPLETE (2026-05-06)
**SA verdict:** PASS — SA-1 (non-blocking comment, no code change required).
**PBE verdict:** PASS — PBE-1 (non-blocking), PBE-2 (non-blocking).
**QA verdict:** PASS — QA-1 resolved inline (W-4b boundary test added), QA-2 typo fixed (commit `2bb1487`). All 30 assertions across 12 test classes (W-1 through W-12) passing.

#### Deliberation Outcomes

**D1 — Option A, B, or C — DECIDED: Option B**
- **Option B selected:** Apply ITM-buyer discount per-event inside `get_weighted_premium()`, using a promoted module-level `_classify_moneyness_band()` function.
- Option A rejected: `is_directionally_aggressive()` is called at parse time in `tradier_stream.py`. `_classify_moneyness_band()` lives in the signal layer (`RepetitionAccumulator`). Pulling signal-layer output back into a parser-layer function inverts the dependency graph — worse than Option B's episode coupling.
- Option C rejected: permanently skews `prior_days_aggressive` multi-day metrics (ING-007). An ITM PUT AT_BID buyer is not a conviction writer; including them in aggressive counts degrades S8 backtest stratification quality over time.

**D2 — `self.otm_band` vs. per-event classification — DECIDED: Per-event**
- Use `_classify_moneyness_band(e)` called per-event inside the `get_weighted_premium()` loop.
- `self.otm_band` is last-tick only (SA-6 Phase 1 accepted limitation). Using it for per-event discount decisions would apply the wrong band to all events except the last.
- Per-event call is identical to the approach used by `_majority_itm_band()` (inline re-implementation of the same threshold math). Promoting `_classify_moneyness_band()` to module level eliminates that duplication.

**D3 — `_classify_moneyness_band()` location — DECIDED: Promote to module-level function**
- The function has zero `self` state dependencies — it is pure arithmetic over an event's fields.
- `RepetitionEpisode._majority_itm_band()` already re-implements the same threshold math inline (SA-F1 fix, ING-011). Promoting `_classify_moneyness_band()` eliminates the duplication and makes it callable from both `RepetitionAccumulator.ingest_tick()` and `RepetitionEpisode.get_weighted_premium()` without circular dependencies.
- Promotion is in-scope for this PR — not a separate story.

**D4 — `prior_days_aggressive` impact — DECIDED: Accepted known gap**
- `prior_days_aggressive` is populated by `get_contract_prior_days()` querying `flow_events.is_aggressive` from the DB — a column stamped at parse time by `is_directionally_aggressive()`, which Option B does not touch.
- DB `is_aggressive` remains parse-time aggression (moneyness-blind). This is the known limitation of Option B vs. Option A.
- Accepted: file a follow-up story if multi-day aggression metrics prove materially skewed after 5+ trading days of live data under ING-007.

**D5 — Cold-start / `underlying_price == 0` fallback — DECIDED: No discount applied**
- `_classify_moneyness_band()` returns `'UNKNOWN'` when `underlying_price == 0`.
- `'UNKNOWN'` is not in `_ITM_BANDS` (`{'ITM', 'DEEP_ITM'}`), so no discount fires — event receives full weight (×1.0).
- This is the correct safe default: when moneyness cannot be determined, do not discount.
- Identical safe-default logic to `_majority_itm_band()` (UNKNOWN events contribute 0 weight to both sides).

#### Implementation Spec (PBE — Option B)

Promote `_classify_moneyness_band()` to module-level, then update `get_weighted_premium()`:

```python
def get_weighted_premium(self, discount: float) -> float:
    total = 0.0
    for e in self.events:
        prem = getattr(e, "premium", 0.0)
        if getattr(e, "is_aggressive", False):
            # ING-011b: re-check moneyness per-event.
            # is_aggressive is moneyness-blind (ING-006). ITM PUT AT_BID
            # fills are buyers, not aggressive writers — apply discount.
            bac   = getattr(e, "bid_ask_class", "UNKNOWN")
            ctype = str(getattr(e, "contract_type", "") or "").upper()
            if (
                ctype == "PUT"
                and bac in ("AT_BID", "BELOW_BID")
                and _classify_moneyness_band(e) in _ITM_BANDS
            ):
                total += prem * discount  # ITM put buyer — discount applies
            else:
                total += prem             # Genuine aggressive writer — full weight
        else:
            total += prem * discount      # Passive fill — unchanged
    return total
```

#### Blast Radius

| Component | Impact |
|---|---|
| `Gate 2` in `ingest_tick()` | `ep_weighted` correctly reduced for ITM PUT AT_BID buyer episodes — false Gate-2 clears suppressed |
| `deep_otm_multiplier` gate | Same — `ep_weighted` no longer overstated for DEEP_OTM puts with AT_BID fills |
| `prior_days_aggressive` (ING-007) | Not affected — DB column stamped at parse time (D4 accepted gap) |
| `order_side_classifier.py` | Not affected — directional logic unchanged |
| `dominant_direction` | Not affected — ING-011 handles direction correctly |

#### Acceptance Criteria

- [x] D1–D5 deliberations recorded inline in this document and in Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80)
- [x] `_classify_moneyness_band()` promoted to module-level function in `repetition_accumulator.py`
- [x] `_majority_itm_band()` updated to call module-level `_classify_moneyness_band()` — eliminates inline duplication
- [x] `get_weighted_premium()` updated per Option B implementation spec above
- [x] `weighted_premium` property correctly delegates to updated `get_weighted_premium()`
- [x] A 3-event ITM PUT AT_BID episode at $200k total premium is discounted to $100k `weighted_premium` (all 3 events at 0.5×)
- [x] A 3-event OTM PUT AT_BID episode at $200k total premium retains $200k `weighted_premium` (full weight unchanged — W-1 regression)
- [x] No regression on `test_ing006_directional_aggression.py`
- [x] No regression on `test_ing007_multiday_lookback.py`
- [x] No regression on `test_ing011_itm_classification.py`
- [x] All QA matrix cases W-1 through W-12 pass

#### QA Test Matrix

| Case | Contract | `bid_ask_class` | `otm_band` | `is_aggressive` (input) | Expected `weighted_premium` weight |
|---|---|---|---|---|---|
| W-1 | OTM PUT | AT_BID | OTM | True | ×1.0 — OTM put writer, correct aggression (ING-006 regression) |
| W-2 | ITM PUT | AT_BID | ITM | True | ×0.5 — ITM buyer, discount applies |
| W-3 | DEEP_ITM PUT | AT_BID | DEEP_ITM | True | ×0.5 — DEEP_ITM buyer, discount applies |
| W-4 | ITM PUT | AT_ASK | ITM | True | ×1.0 — aggressive AT_ASK buyer, full weight always |
| W-4b | ITM PUT | AT_BID | ITM | True, premium boundary ($10k floor) | ×0.5 — boundary test: floor-value event still discounted |
| W-5 | ITM CALL | AT_BID | ITM | True | ×1.0 — call writer, correct aggression, no change |
| W-6 | OTM PUT | MID | OTM | False | ×0.5 — passive mid-fill, unchanged |
| W-7 | ITM PUT | AT_BID | UNKNOWN | True | ×1.0 — band unknown, no discount applied (safe fallback) |
| W-8 | 3-event episode: 2× ITM PUT AT_BID + 1× OTM PUT AT_BID | mixed | ITM/OTM | True | Only the 2 ITM events discounted; OTM event full weight |
| W-9 | ITM PUT AT_BID, `underlying_price=0` | AT_BID | UNKNOWN | True | ×1.0 — UNKNOWN band → no discount (D5 fallback) |
| W-10 | `weighted_premium` property delegates to updated `get_weighted_premium()` | — | — | — | Same result as direct call |
| W-11 | ING-006 regression: OTM PUT AT_BID writer | AT_BID | OTM | True | ×1.0 — explicit regression label (same as W-1) |
| W-12 | `prior_days_aggressive` not affected — DB `is_aggressive` column unchanged | — | — | — | Integration note: confirm in ING-007 lookback test suite |

---

### ING-010 — Tier-Aware Min-Premium Floor + OI-Relative Bypass Gate

**Type:** Feature / Signal Quality
**Priority:** P1
**Estimated Effort:** 1.5 days
**Depends On:** ING-002 ✅, ING-003 ✅
**Does NOT block:** ING-011, ING-008 (parallel track)
**Must resolve before:** Any small-cap catalyst monitoring is considered reliable
**Related:** ING-008 (OI gate — OI-relative bypass logic in ING-010 must be designed consistently with ING-008 OI significance logic)
**Files:**
- `backend/parsers/options_flow_parser.py` — tier-aware floor lookup + OI-relative bypass gate
- `backend/services/options_universe.py` (or equivalent) — expose `tier`, `open_interest`, `average_volume` per symbol to parser
- `backend/tests/test_ing010_tier_floor.py` — NEW: full test matrix
**GitHub Issue:** [#78](https://github.com/bhaveshhpatel/cipher/issues/78)
**Branch:** `ing/s10-tier-floor` *(not yet created)*

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION (SA · PBE · QA)

#### Problem

The flat DTE-adjusted min-premium floor silently drops **all** options prints for low-price and low-OI tickers before they reach the accumulator. These tickers are `stream_eligible = true` in `options_universe_symbols` and are actively subscribed on the Tradier stream, but zero events are persisted or gated — they vanish at the `belowminpremium` funnel counter with no per-ticker logging.

**Observed instances (2026-05-06):**

| Ticker | last_price | open_interest | avg_volume | tier | Flow today |
|--------|-----------|---------------|------------|------|------------|
| GDYN   | $6.10     | 380           | 1,613,139  | 3    | 0 events   |
| PENG   | $36.45    | 1,422         | 0 (bug)    | 3    | 0 events   |

**Logging gap:** `belowminpremium` is an opaque aggregate counter. There is no per-ticker logging at this drop stage.

#### Proposed Options

**Option A — Tier-Based Floor Override** *(lowest risk — ship first)*
```python
TIER_FLOORS = {
    1: 50_000,   # large-cap
    2: 25_000,   # mid-cap
    3: 5_000,    # small-cap — GDYN, PENG land here
}
min_floor = TIER_FLOORS.get(symbol_tier, DEFAULT_FLOOR)
```

**Option B — OI-Relative Bypass Gate** *(most signal-accurate — medium-term)*
```python
OI_SIGNIFICANCE_THRESHOLD = 0.05  # 5% of total OI
if open_interest > 0 and (size / open_interest) >= OI_SIGNIFICANCE_THRESHOLD:
    bypass_premium_floor = True
```

**Option C — Relative Premium Floor** *(most generalizable)*
```python
min_premium = max(BASE_FLOOR, underlying_price * size * RELATIVE_MULTIPLIER)
```

**Option D — Per-Ticker Debug Logging at Drop Stage** *(prerequisite for all)*
```python
if premium < min_premium_floor:
    logger.debug(f"belowminpremium {ticker} {contract_type} {strike} dte{dte} prem{premium} floor{min_premium_floor}")
```

**Option E — Fix `average_volume = 0` Bug for PENG**
PENG's `average_volume = 0` in `options_universe_symbols` is a universe refresh failure.

#### Deliberations Required (3-Way: SA · PBE · QA)

**D1 — Which options to implement, and in what order?**
**D2 — Tier 3 floor value:** `$5,000` proposed. Needs validation against historical Tier 3 tape data.
**D3 — `average_volume = 0` handling:** Sentinel/fallback or block `stream_eligible` until fixed?

#### Acceptance Criteria

- [ ] D1, D2, D3 deliberations resolved and documented inline (SA + PBE + QA sign-off)
- [ ] Option D (per-ticker debug logging at `belowminpremium`) shipped as prerequisite
- [ ] At least one of Options A/B/C implemented per D1 resolution
- [ ] GDYN and PENG flow events begin appearing in `flow_events` after fix (manual verification on a live session)
- [ ] `average_volume = 0` sentinel handling added to universe refresh and/or floor calculation
- [ ] Existing Tier 1/2 signal quality unaffected — no new noise on NVDA, AMD, AAPL, SPY
- [ ] Unit tests: Tier 3 print below flat floor but above tier floor; OI bypass gate at 5% threshold; `average_volume = 0` fallback
- [ ] All new `_stats` keys initialised at module level — cold-start safe

---

### ING-008 — Volume vs. OI Gate via Registry Injection

**Type:** Feature / Gate Addition
**Priority:** P0
**Estimated Effort:** TBD — pending deliberation
**Depends On:** ING-004 ✅, ING-005 ✅, **ING-011 ✅**
**Files:** TBD — pending deliberation
**GitHub Issue:** TBD
**Branch:** `ing/s8-vol-oi-gate` *(not yet created)*

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION

> **ING-011 merged 2026-05-07 (PR #81 commit `8d68ed1`). ING-011b merged 2026-05-07 (PR #82). ING-008 deliberation is now unblocked.** Read the full ING-011 and ING-011b deliberation outcomes above (D1–D5 decisions, panel verdicts) before beginning ING-008 deliberation — OI gate thresholds must be calibrated against correctly-classified and correctly-weighted directional data. Do not begin implementation until deliberation is complete and all three roles have signed off.

---

*Last updated: 2026-05-07 (ING-011b MERGED — PR #82 squash 2026-05-07; Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) closed; Sprint Order row 8b marked ✅ MERGED; Post-ING-011-Merge Findings table updated; Post-ING-011b-Merge Findings section added; ING-011b story section: AC all checked ✅, pre-merge panel verdicts recorded SA/PBE/QA PASS, W-4b boundary test row added to QA matrix; ING-008 note updated to reflect ING-011b merged) | Sprint: WSJ Ingestion Alignment (P0) | Owner: Dhruv Patel*
