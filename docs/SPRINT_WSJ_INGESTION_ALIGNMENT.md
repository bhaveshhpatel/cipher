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
| 7 | **ING-007** | Multi-day repeat window lookback (DB + cache) | ING-002 ✅, ING-003 ✅, ING-006 ✅, **ING-009 ✅** | ✅ UNBLOCKED — ING-009 merged 2026-05-06. Deliberation ✅ COMPLETE 2026-05-04. Issue [#70](https://github.com/bhaveshhpatel/cipher/issues/70) — **ready to implement** |
| 8 | **ING-008** | Volume vs. OI gate via registry injection | ING-004 ✅, ING-005 ✅ | 🔴 UNBLOCKED — deliberation required before implementation |

---

## Post-ING-006-Merge Findings (GitHub Issues Filed)

The following issues were filed during ING-006 deliberation and are tracked separately. None block ING-007 implementation start (deliberation is what blocks ING-007).

| Issue | Title | Blocking? | Sprint Slot |
|-------|-------|-----------|-------------|
| [#63](https://github.com/bhaveshhpatel/cipher/issues/63) | Migrate `is_directionally_aggressive()` to dedicated aggression module | Not blocking ING-006 merge. Blocking ING-007/008 callers importing from `bid_ask_classifier.py` | SA-F1 post-ING-006 |
| [#64](https://github.com/bhaveshhpatel/cipher/issues/64) | SA-F1: Persist `is_aggressive` flag to `flow_events` for ING-007 pattern quality scoring | P1 — required before ING-007 pattern quality scoring is meaningful | ING-007 prerequisite |
| [#65](https://github.com/bhaveshhpatel/cipher/issues/65) | ING-007 story issue — Multi-day repeat window lookback | — | ING-007 |
| [#66](https://github.com/bhaveshhpatel/cipher/issues/66) | ING-006 SA-F1: Migrate `is_directionally_aggressive()` out of `bid_ask_classifier.py` | Not blocking ING-006. Blocking for ING-007/008 signal-layer import path | SA-F1 post-ING-006 |
| [#67](https://github.com/bhaveshhpatel/cipher/issues/67) | ING-007 prereq (SA-F2): Add `is_aggressive` boolean column to `flow_events` | **Blocking for ING-007 and S8 backtest work** | ING-007 S2.5 migration |
| [#68](https://github.com/bhaveshhpatel/cipher/issues/68) | SA-F1 shim removal — `is_aggressive()` deprecated shim in `bid_ask_classifier.py` | Coordinate with #63/#66 | Post ING-006 cleanup |
| [#69](https://github.com/bhaveshhpatel/cipher/issues/69) | Add `flow_events.is_aggressive` column + `persist_flow_episode` serialisation (S2.5 migration) | **Blocking production deploy** | ING-007 S2.5 |
| [#70](https://github.com/bhaveshhpatel/cipher/issues/70) | ING-007: Multi-day repeat window lookback + is_aggressive DB column | — | ING-007 canonical issue |
| [#75](https://github.com/bhaveshhpatel/cipher/issues/75) | ING-009: Same-session flow episode upsert/merge | ~~**Blocking ING-007**~~ ✅ MERGED PR #76 2026-05-06 | ING-009 canonical issue — CLOSED |

---

## Post-ING-009-Merge Findings (GitHub Issues Filed)

The following observation was noted during the ING-009 pre-merge deliberation (2026-05-06). It does not block ING-007 implementation.

| Issue | Title | Blocking? | Sprint Slot |
|-------|-------|-----------|-------------|
| Filed post-merge | `_lookup_open_episode` exception branch has no isolated unit test — the E-8 test mocks the entire function; the internal `except` block in the implementation is not independently exercised | Not blocking ING-007 | Post ING-009 cleanup |

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
**All three roles signed off. Story cleared for implementation.**

#### ✅ Pre-Merge Panel Deliberation — COMPLETE (2026-05-06)
**SA verdict:** PASS — gate order preserved, layer boundaries respected, ING-007 PATCH path untouched, deliberation alignment confirmed. Post-merge observation filed: `_lookup_open_episode` float strike in URL not URL-encoded (consistent with existing pattern in `_update_episode_multiday`; non-blocking).
**PBE verdict:** PASS — upsert logic correct, `or 1` / `or 0.0` guards sound, PATCH early-return path correct, PATCH failure does not silently increment counter, `_episode_stats` init at module level cold-start safe, `httpx.AsyncClient` 5s timeout async non-blocking.
**QA verdict:** PASS — E-1 through E-11 full matrix present and passing, counter invariants verified, cold-start safety confirmed, `get_episode_stats()` accessor present for `/health/stream`.

#### Problem

`flow_episodes` was insert-only from the Signal Gate path, creating one new row per qualifying print instead of one row per logical same-session episode. This made `flow_episodes` a near-duplicate of `flow_events` (26,906 vs 28,373 rows on 2026-05-05) rather than an aggregated episode table.

The EPISODE-FIX (2026-04-30) correctly moved episode persistence before SIG-DEBOUNCE to preserve `strike`/`expiry`, but in doing so exposed that `persist_flow_episode()` had no merge/upsert path — every Signal Gate crossing unconditionally inserted a new row.

**Root cause:** ING-007's `get_contract_prior_days()` would query fragmented single-print episode rows and produce unreliable repeat-day counts. ING-009 must ship before ING-007 is implemented.

#### Deliberation Outcomes

**SA — DECIDED: Fix data model semantics, not the gate threshold**
- `flow_events` = every qualifying classified tick
- `flow_episodes` = one aggregated episode per contract per same-session window
- Reintroducing a drop gate (e.g. `min_trade > N`) at the DB-write level hides data rather than models it correctly
- Keep gate order: Signal Gate → `persist_flow_episode()` → SIG-DEBOUNCE. Do not re-tie to debounce.
- Fix belongs in episode upsert/merge semantics inside `persist_flow_episode()` in `flow_store.py`

**PBE — DECIDED: Insert-or-update in `persist_flow_episode()` keyed on contract identity + session window**
- Add episode lookup via Supabase REST: query `flow_episodes` for open episode matching key with `signal_ts >= now() - _EPISODE_MERGE_WINDOW_S`
- Episode merge key: `(ticker, direction, contract_type, strike, expiry)`
- On match → PATCH: `trade_count += 1`, `total_premium += new_premium`, `signal_ts = new_ts`
- On no match → INSERT (existing path unchanged)
- Merge logic lives entirely in `flow_store.py` — not in `tradier_stream.py`, not in the accumulator
- ING-007's multi-day repeat detection is a separate query layer on top of correctly merged episodes

**QA — DECIDED: Full boundary test matrix required**
- Must prove: two qualifying prints for same contract within window → one row, `trade_count = 2`
- Must prove: print after window expiry → new episode row
- Must prove: next-day repeat → new episode row (ING-007 repeat flag applies independently)
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

#### Implementation Steps

1. Define `_EPISODE_MERGE_WINDOW_S: int = 1800` (30 min) as module-level constant in `flow_store.py`
2. Define episode merge key: `(ticker, direction, contract_type, strike, expiry)`
3. Add `_lookup_open_episode(key_fields: dict, window_s: int) -> Optional[dict]` in `flow_store.py`
   - Query `flow_episodes` via Supabase REST: `ticker=eq.X&direction=eq.X&contract_type=eq.X&strike=eq.X&expiry=eq.X&signal_ts=gte.{cutoff}&order=signal_ts.desc&limit=1`
   - Return episode row dict if found, else `None`
4. Refactor `persist_flow_episode(signal_data: dict)`:
   - Call `_lookup_open_episode()` with merge key and window
   - **No match →** INSERT (existing `_insert_rows("flow_episodes", [row])` path); increment `_stats["created_episodes"]`
   - **Match found →** PATCH existing row id: `trade_count += 1`, `total_premium += new`, `signal_ts = new`; increment `_stats["merged_episodes"]`
5. Add `"created_episodes": 0` and `"merged_episodes": 0` to module-level `_stats` init block
6. Write `backend/tests/test_ing009_episode_upsert.py` covering full test matrix below

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
**Depends On:** ING-002 ✅, ING-003 ✅, ING-006 ✅, **ING-009 ✅ MERGED 2026-05-06**
**Files:**
- `backend/utils/contract_day_cache.py` — NEW: async TTL cache + DB fetch logic
- `backend/signals/repetition_accumulator.py` — wire `prior_days_active`, `prior_days_aggressive`, `is_multi_day_repeat`, `otm_band` onto `RepetitionEpisode`; constructor params `require_multi_day`, `multi_day_min_days`
- `backend/services/flow_store.py` — background `asyncio.Queue` worker; `_stats["lookback_queue_overflow"]` counter
- `backend/services/tradier_stream.py` — wire `_lookback_queue.put_nowait()` in `_process_trade()`
- `supabase/migrations/` — S2.5 migration: index + `order_side` column + `is_aggressive` column
- `backend/tests/test_ing007_multiday_lookback.py` — NEW: G-1 through G-8 fixture cases + TTL expiry + latency benchmark + otm_band wiring
**GitHub Issue:** [#70](https://github.com/bhaveshhpatel/cipher/issues/70)
**Branch:** `ing/s7-multiday-repeat`

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-04)
**All three roles signed off. Story cleared for implementation. ✅ UNBLOCKED — ING-009 merged 2026-05-06.**

---

#### Senior Architect (SA) — Decisions

**SA-Q1: Hard gate vs. soft enrichment flag — DECIDED: Flag**

`require_multi_day: bool = False` constructor param on `RepetitionAccumulator`. A hard gate would silently drop episodes for new listings, newly-tracked symbols, or any ticker that hasn't repeated in 5 days — signal quality degradation disguised as a feature. WSJ uses multi-day recurrence as a conviction amplifier, not a binary qualifier. The flag pattern (`ep.is_multi_day_repeat = True/False`) lets the signal layer weight it without destroying otherwise-valid single-day sweeps. Promotion to hard gate deferred until backtest data from ING-007 confirms false-positive reduction justifies the drop rate.

**SA-Q2: Lookback window — DECIDED: 5 calendar days**

Calendar days are deterministic with no market calendar dependency. `pandas_market_calendars` / `trading-calendars` adds a dependency and failure mode (stale calendar, holiday edge cases) that is unnecessary at this stage. Combined with SA-Q3's qualifying-flow-only counting, weekends and holidays auto-drop out because there is no flow on those days. No extra logic required.

**SA-Q3: `prior_days_active` counting method — DECIDED: Days-with-qualifying-flow only**

`prior_days_active = COUNT(DISTINCT DATE(created_at))` on qualifying flow for that contract over the lookback window. "Qualifying" = same `(ticker, contract_type, strike, expiry)` tuple, `premium >= DTE-tier floor`. Counting calendar days inflates the metric on quiet days (e.g., SPY swept Monday, nothing Tuesday–Friday → `prior_days_active = 1`, not 4). The DB query returns distinct-day count; the cache stores it.

**SA-F2-Q1: Two counters — DECIDED: Both `prior_days_active` + `prior_days_aggressive`**

Collapsing to one counter loses the most important distinguishing signal for WSJ-style flow reading. `prior_days_active = 3` where all 3 prior days were passive fills (MID pricing) is categorically weaker than `prior_days_active = 3` where all 3 were aggressive (AT_ASK, ABOVE_ASK). The `is_aggressive` column from S2.5 migration makes the second counter free — it is a second `COUNT(DISTINCT DATE(created_at)) WHERE is_aggressive = TRUE` in the same query.

- `prior_days_active: int` — all qualifying flows, distinct calendar days
- `prior_days_aggressive: int` — aggressively-filled qualifying flows only, distinct calendar days
- Both stored on `RepetitionEpisode` dataclass
- Both exposed in episode serialisation / signal metadata
- `is_multi_day_repeat = prior_days_active >= 2` (threshold below)

**SA: Multi-day threshold — DECIDED: >= 2, configurable**

`>= 2` means it repeated on at least one prior day — the minimal bar that catches early accumulation. WSJ commentary targets recurrence, not dominance. Wire threshold as `multi_day_min_days: int = 2` constructor param on `RepetitionAccumulator`. Do NOT hardcode `2` inline — configurable from day 1 for future backtest tuning.

`is_multi_day_repeat = prior_days_active >= multi_day_min_days`

**SA: S2.5 Migration — CRITICAL ORDER**

S2.5 migration must land before any Python. Three parts:

```sql
-- Part 1: Index
CREATE INDEX IF NOT EXISTS idx_flow_events_contract_day
ON flow_events (ticker, contract_type, strike, expiry, created_at DESC);

-- Part 2: Columns
ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS order_side TEXT DEFAULT 'UNKNOWN';
ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS is_aggressive BOOLEAN DEFAULT FALSE;

-- Part 3: Backfill gap (DOCUMENT — do not attempt backfill)
-- Existing rows: is_aggressive = FALSE (default). prior_days_aggressive will be 0
-- for all historical data until enough newly-flagged rows accumulate (~5 trading days).
-- Backfilling is_aggressive from bid_ask_class string on existing rows is error-prone.
-- Document this cold-start characteristic explicitly in the PR. Do not attempt backfill.
```

Run `EXPLAIN ANALYZE` and confirm index hit before writing any Python for the lookback query (Rule 6 of STORY-STEPS_ING.md).

---

#### Principal Backend Engineer (PBE) — Decisions

**PBE-Q1: Cache library — DECIDED: Check requirements first; manual dict fallback; no new deps**

Check `requirements.txt` before assuming. If `cachetools` is present: use `TTLCache(maxsize=500, ttl=300)`. If absent: use manual `{key: (value, expires_at)}` dict pattern with expiry-on-read. Do NOT add `cachetools` as a new dependency solely for this story. If `redis` or another caching lib is already present, evaluate that first. The TTL cache is not complex enough to justify a new dependency.

**PBE-Q2: Background queue eventual consistency — DECIDED: Acceptable for flag pattern**

The background `asyncio.Queue` pattern means lookback results are populated asynchronously after the first episode for a contract arrives. The first episode in a session may briefly see `prior_days_active = 0` and `is_multi_day_repeat = False` until the queue worker completes the DB fetch and populates the cache. Acceptable — signal layer treats `is_multi_day_repeat` as enrichment, not a gate. Document the "first-episode cold-cache" behaviour in `flow_store.py` module docstring.

**PBE-Q3: Queue overflow — DECIDED: `maxsize=5000`, overflow counter, no exception propagation**

```python
try:
    _lookback_queue.put_nowait(contract_key)
except asyncio.QueueFull:
    _stats["lookback_queue_overflow"] += 1
```

`_stats["lookback_queue_overflow"]` exposed in `/health/stream`. Do NOT let `asyncio.QueueFull` propagate as an unhandled exception that kills the hot path. Overflow means lookback enrichment is silently skipped for that episode; the episode still produces and emits correctly.

**PBE: Cache key — DECIDED: 4-tuple `(ticker, contract_type, strike, expiry)`**

```python
ContractKey = Tuple[str, str, float, str]  # (ticker, contract_type, strike, expiry)
```

Using `(ticker, strike)` is incorrect — same strike different expiry is a different contract. Cache key must match the DB query predicate exactly.

**PBE: Lookback DB query — DECIDED: 6-param with DATE_TRUNC ceiling + premium floor param**

```sql
SELECT
    COUNT(DISTINCT DATE(created_at))                                          AS prior_days_active,
    COUNT(DISTINCT DATE(created_at)) FILTER (WHERE is_aggressive = TRUE)      AS prior_days_aggressive
FROM flow_events
WHERE ticker        = $1
  AND contract_type = $2
  AND strike        = $3
  AND expiry        = $4
  AND created_at   >= NOW() - INTERVAL '5 days'
  AND created_at   <  DATE_TRUNC('day', NOW())  -- exclude today
  AND premium      >= $5;  -- DTE-appropriate floor passed from accumulator context
```

`AND created_at < DATE_TRUNC('day', NOW())` is critical — today's flow must NOT count toward `prior_days_active`. Today's episode is what is being evaluated; prior days are the lookback. Without this clause, the first episode of the day sets `prior_days_active = 1` (counting itself), which is wrong.

Premium floor `$5` is passed as a parameter from accumulator context — no hardcoding in the query.

**PBE: Module structure — DECIDED: `contract_day_cache.py` in `backend/utils/`**

```python
# contract_day_cache.py
# Async TTL cache for per-contract multi-day lookback results.
# Cache entries: ContractKey -> LookbackResult
# TTL: 5 minutes (300s) — stale reads acceptable; hard gate not in use

LookbackResult = NamedTuple:
    prior_days_active:     int
    prior_days_aggressive: int
    fetched_at:            float  # time.monotonic()

_cache: Dict[ContractKey, LookbackResult] = {}
_CACHE_TTL_S = 300

async def get_lookback(key: ContractKey, min_premium: float) -> LookbackResult: ...
async def _fetch_from_db(key: ContractKey, min_premium: float) -> LookbackResult: ...
```

`flow_store.py` handles the write path — it is NOT the right home for cache + DB fetch logic. `contract_day_cache.py` is read-only, separate concern.

---

#### Lead QA (QA) — Decisions

**QA-Q1: Integration test fixture cases — ALL 8 REQUIRED**

| Case | Seeded Data | Expected `prior_days_active` | Expected `prior_days_aggressive` |
|------|-------------|------------------------------|----------------------------------|
| G-1 | 3 qualifying rows on 3 distinct prior days, all aggressive | 3 | 3 |
| G-2 | 5 rows on 3 days, 2 days aggressive only | 3 | 2 |
| G-3 | 3 rows all on same prior day | 1 | depends on `is_aggressive` seed |
| G-4 | No prior qualifying rows | 0 | 0 |
| G-5 | Rows exist but all today (excluded by ceiling clause) | 0 | 0 |
| G-6 | Rows outside 5-day window (6 days ago) | 0 | 0 |
| G-7 | Mix: 2 qualifying days prior + 1 today | 2 | per `is_aggressive` seed |
| G-8 | Premium below DTE floor on all prior rows | 0 | 0 |

G-5 and G-6 are the most important regression guards — they confirm the `DATE_TRUNC` ceiling clause and the 5-day window are both applied correctly.

**QA-Q2: Cache TTL expiry test**

Mock `time.monotonic()` to advance 301 seconds after initial fetch. Next call to `get_lookback()` must re-fetch from DB — assert `_fetch_from_db` call count == 2 (mock/patch the function and assert call count). Stale cached value must NOT be returned after TTL expiry.

**QA-Q3: `_process_trade()` latency benchmark**

The sprint doc requires "no measurable latency increase." Before implementation, run:

```bash
python -m pytest backend/tests/test_latency_baseline.py -v --tb=short
```

If no latency baseline test exists, create one in this PR: time 1,000 `_process_trade()` calls with the background queue worker running, assert p99 < 5ms. Post-implementation, same test must still pass. The async queue pattern means hot-path latency should be near-zero (just a `put_nowait`), but this must be confirmed empirically.

**QA: `otm_band` wiring — REQUIRED (deferred from ING-005)**

`ep.otm_band` wiring was explicitly deferred from ING-005. It is required in ING-007. Not optional.

```python
# In repetition_accumulator.py ingest_tick(), after _classify_otm() call:
ep.otm_band = self._classify_otm(ev.strike, ev.underlying_price)

# On RepetitionEpisode dataclass:
otm_band: str = "UNKNOWN"
```

Dedicated test required: seed a known `strike`/`underlying_price` pair, assert `ep.otm_band` is set correctly on the returned episode. Two-line test — explicitly verify, do not assume.

---

#### ING-007 Acceptance Criteria

- [ ] S2.5 migration runs cleanly — index, `order_side` column, `is_aggressive` column all present in `flow_events`
- [ ] `EXPLAIN ANALYZE` confirms index hit on lookback query before Python implementation begins
- [ ] `contract_day_cache.py` created in `backend/utils/` — owns all cache + DB fetch logic
- [ ] Cache key is 4-tuple `(ticker, contract_type, strike, expiry)` — no collisions on same strike different expiry
- [ ] `get_lookback()` returns cached result within TTL (300s); re-fetches from DB after expiry
- [ ] DB query uses 6-param form with `DATE_TRUNC('day', NOW())` ceiling — today excluded from `prior_days_active`
- [ ] DB query passes DTE-tier premium floor as `$5` parameter — no hardcoded floor in query
- [ ] `RepetitionEpisode` dataclass gains: `prior_days_active: int`, `prior_days_aggressive: int`, `is_multi_day_repeat: bool`, `otm_band: str = "UNKNOWN"`
- [ ] `RepetitionAccumulator` constructor gains: `require_multi_day: bool = False`, `multi_day_min_days: int = 2`
- [ ] `is_multi_day_repeat = prior_days_active >= multi_day_min_days` — threshold not hardcoded inline
- [ ] Background `asyncio.Queue` worker in `flow_store.py` — `maxsize=5000`
- [ ] `_stats["lookback_queue_overflow"]` initialised at module level; increments on `asyncio.QueueFull`; never propagates as unhandled exception
- [ ] `_stats["lookback_queue_overflow"]` visible in `/health/stream` from cold start
- [ ] "first-episode cold-cache" behaviour documented in `flow_store.py` module docstring
- [ ] `is_aggressive` cold-start lag documented in PR description — no backfill attempted
- [ ] All 8 fixture cases G-1 through G-8 pass
- [ ] TTL expiry test passes — `_fetch_from_db` call count asserted
- [ ] Latency benchmark: p99 < 5ms for 1,000 `_process_trade()` calls with queue worker running
- [ ] `otm_band` wiring: `ep.otm_band` set correctly; dedicated test with known strike/underlying pair passes
- [ ] Both `prior_days_active` and `prior_days_aggressive` exposed in episode serialisation / signal metadata
- [ ] No new dependencies added unless `cachetools` already present in `requirements.txt`

---

*Last updated: 2026-05-06 (ING-009 ✅ MERGED PR #76 commit `9ceee35`; Issue #75 closed; ING-007 ✅ UNBLOCKED — ready to implement) | Sprint: WSJ Ingestion Alignment (P0) | Owner: Dhruv Patel*
