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
**Finding:** Tradier’s timesale WebSocket stream does **not** include `order_side`, `side`, or `aggressor_side` in the tick payload. This is a platform-level limitation — Tradier’s documented timesale fields are: `type`, `symbol`, `exchange`, `bid`, `ask`, `last`, `size`, `date`, `open`, `high`, `low`, `close`, `prevclose`. No aggressor-side field exists.

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
| 10 | **ING-010** | Tier-aware configurable ingestion gate system + hot-reload admin control | ING-002 ✅, ING-003 ✅, ING-004 ✅, ING-006 ✅ | 🔶 **IN PROGRESS** — branch [`ing/s10-tiered-gate-control-plane`](https://github.com/bhaveshhpatel/cipher/tree/ing/s10-tiered-gate-control-plane) — Issue [#84](https://github.com/bhaveshhpatel/cipher/issues/84) |

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
| [#78](https://github.com/bhaveshhpatel/cipher/issues/78) | Superseded by ING-010 revamp scope — original tier-aware min-premium floor + OI-relative bypass gate framing | No longer canonical | Historical |
| [#80](https://github.com/bhaveshhpatel/cipher/issues/80) | ING-011b: `is_aggressive` moneyness-blindness inflates `weighted_premium` for ITM PUT AT_BID episodes | Not blocking ING-011 merge. Follow-on to ING-011. Deliberation COMPLETE 2026-05-06 — Option B selected. | ING-011b — ✅ MERGED PR #82 2026-05-07 — Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) closed |
| [#84](https://github.com/bhaveshhpatel/cipher/issues/84) | ING-010: Tier-aware configurable ingestion gate system + hot-reload admin control | **Canonical ING-010 issue**. Replaces the narrow floor-only framing with a full gate control-plane revamp. | ING-010 |

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

## ING-010 Implementation Log — 2026-05-08

**Branch:** [`ing/s10-tiered-gate-control-plane`](https://github.com/bhaveshhpatel/cipher/tree/ing/s10-tiered-gate-control-plane)
**Status:** 🔶 In Progress — pre-merge checklist items being resolved
**Canonical Issue:** [#84](https://github.com/bhaveshhpatel/cipher/issues/84)

### Deliberation Findings Resolved on Branch (2026-05-08)

| Tag | Finding | Commit |
|-----|---------|--------|
| SA-3 | `signal_min_premium` + `exclude_indices` rows missing from `019_gate_configs.sql` seed — admin PATCH against them would silently no-op | [`0255bec`](https://github.com/bhaveshhpatel/cipher/commit/0255bec1d998a74398e9ddf00d6f3e4829454494) |
| SA-4 | Tombstone migration stubs (`20260507_create_gate_configs.sql`, `20260507_ing010_gate_configs_and_audit.sql`, `20260507_seed_gate_configs.sql`) deleted — unnumbered files skipped by `run_migrations.py` and created false schema impression | [`3f0046c`](https://github.com/bhaveshhpatel/cipher/commit/3f0046ce1abb5a50f5baff02b9d6e2fe8eede29a) / [`3f3fa2b`](https://github.com/bhaveshhpatel/cipher/commit/3f3fa2be3f1d6034fafc642590fa22da9c6550bd) |
| SA-5 | `gate_config_store.update()` was inserting `previous_value` key — column does not exist in DDL (uses `old_value`/`new_value`). Removed; audit insert now uses `old_value` consistently | [`b88d882`](https://github.com/bhaveshhpatel/cipher/commit/b88d8826daf62c0c40e3764560f1e9bf761be414) |
| SA-6 | `main.py` startup sequence comment mis-numbered after ING-010 inserted Step 0 (`gate_config_store.load()`). Steps renumbered to match true execution order | [`b88d882`](https://github.com/bhaveshhpatel/cipher/commit/b88d8826daf62c0c40e3764560f1e9bf761be414) |
| PBE-1 / TGC-5 | `GateConfigStore.get()` returned `None` for unknown gate names — test contract requires `0.0`. Fixed; return type annotation tightened from `Optional[float]` → `float` | [`d9abdb1`](https://github.com/bhaveshhpatel/cipher/commit/d9abdb1c7ef0d4f0c4a55c9b5df37dd8cc0e349e) |
| QA-2 / QA-4 | `_VALID_TIERS` and `_BOUNDS` missing as module-level exports (caused `ImportError` on every test run). Added. `exclude_indices` gate coverage added (`TestExcludeIndicesGate`) | [`5b02dd6`](https://github.com/bhaveshhpatel/cipher/commit/5b02dd6558cdff37b56e354f7012e52a294f14db) |
| Contract collision (5-way) | `_DEFAULTS` / `_FALLBACK` / `_BOUNDS` shapes were inconsistent across 4 test files. Resolved: `_DEFAULTS` = nested `{gate: {tier: value}}`, `_FALLBACK` = flat `{(gate, tier): value}`, `_BOUNDS` = `{gate: (lo, hi, cast)}` 3-tuple | [`04f817b`](https://github.com/bhaveshhpatel/cipher/commit/04f817b94f920983c5644d9c27b014d3b5ddfa33) |
| datetime import collision | `from datetime import datetime` shadowed the module name; tests patching `services.gate_config_store.datetime` hit `AttributeError`. Refactored to `import datetime as dt_module` | [`89a997a`](https://github.com/bhaveshhpatel/cipher/commit/89a997a503b62ecccf1516456e2250d98461ee7b) |
| load() credential resolution | `load()` read credentials exclusively from `config.settings`; `update()` already did instance-attr-first. Tests inject `_supabase_url`/`_supabase_key` directly — `load()` was falling through to no-DB branch silently. Aligned both methods | [`c25ea6a`](https://github.com/bhaveshhpatel/cipher/commit/c25ea6af613f5e272b9bd445e398ff120f6b73ba) |
| Blocker 1 (migration 021) | `021` INSERT referenced wrong columns (missing `value_type`, `description`; duplicate rows already in `020`). Rewritten to be idempotent with correct schema | [`c0b4788`](https://github.com/bhaveshhpatel/cipher/commit/c0b478811cd6863eb623338f2a44992f89d54065) |
| Medium 3 | `signal_min_premium` `_DEFAULTS` drifted from DB seed (T1/T2/T3 values mismatched). Aligned to T1=75k, T2=50k, T3=25k | [`c0b4788`](https://github.com/bhaveshhpatel/cipher/commit/c0b478811cd6863eb623338f2a44992f89d54065) |
| Medium 4 | `signal_min_premium` missing from `_VALID_GATE_NAMES` and `_ALL_GATES` in `admin.py` — PATCH and GET matrix excluded it silently. Added | [`c0b4788`](https://github.com/bhaveshhpatel/cipher/commit/c0b478811cd6863eb623338f2a44992f89d54065) |
| EPISODE-FIX regression | `persist_flow_episode` was called **after** the `signal_min_premium` gate check — episodes with `total_premium` below T1 floor (e.g. SPY PUT $60k) were never persisted even when accumulator crossed Gate-2. Hoisted `asyncio.create_task(persist_flow_episode(...))` to before the signal gate | [`71643f7`](https://github.com/bhaveshhpatel/cipher/commit/71643f746920a31c05805b3788183f2271d69d6b) |
| AsyncMock patch regression | `persist_flow_episode` patches missing `new_callable=AsyncMock` — `asyncio.create_task()` received a non-coroutine MagicMock, raised `TypeError` silently, leaving `persisted_episodes` empty in 3 episode-direction tests | [`022a6e2`](https://github.com/bhaveshhpatel/cipher/commit/022a6e26971961d1e60b8c141b61596a81b1c91a) |
| EI-1 through EI-10 | Stream-side `exclude_indices` gate tests added: `_resolve_exclude_indices` + `_process_trade` filter (10 cases covering ON/OFF state, non-index passthrough, safe fallback on exception, `_stats[index_filtered]` counter) | [`e84f4cd`](https://github.com/bhaveshhpatel/cipher/commit/e84f4cd2d14afccebe876f97759435a876772d74) |

### ING-010 Open Items as of 2026-05-08

- Pre-merge checklist items above are all resolved on branch
- Branch is **not yet merged** — pending final CI green + PR review
- ING-008 deliberation still required (unblocked since 2026-05-07)

---

## Post-ING-010 Findings — Admin UI Surface (2026-05-08)

Filed during ING-010 branch work. Frontend surface for the gate control plane is a separate parallel-track story.

| Issue | Title | Blocking? | Sprint Slot | Status |
|-------|-------|-----------|-------------|--------|
| [#87](https://github.com/bhaveshhpatel/cipher/issues/87) | ADMIN-UI-001: Gate Control Panel — frontend surface for ING-010 gate control plane | Not blocking ING-010 merge. Parallel frontend track. | `admin/ing-gate-control-panel` branch | 🟡 In Progress — Chunks 1–4 complete |

### ING-010 Post-Merge Admin UI Checklist

> To be completed once ING-010 merges to `main`.

- ☐ Merge `admin/ing-gate-control-panel` → `main` after ING-010 is on `main`
- ☐ Verify `GET /api/admin/gate-config` and `PATCH /api/admin/gate-config` endpoints reachable from Vercel prod
- ☐ Smoke-test: load Gate Control Panel in prod admin UI, confirm all 5 gates × 3 tiers render with live DB values
- ☐ `docs/ARCHITECTURE.md` — document `GateConfigStore` singleton, `gate_configs` DB table, 5-gate × 3-tier threshold matrix
- ☐ Close Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87) after merge

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
- New test file: `test_directional_aggression.py` — covers all 8 `(bid_ask_class × contract_type)` combinations

---

*Last updated: 2026-05-08 — ING-010 branch active; all pre-merge deliberation findings resolved; ADMIN-UI-001 (Issue [#87](https://github.com/bhaveshhpatel/cipher/issues/87)) filed as parallel frontend track for ING-010 gate control plane UI — Chunks 1–4 complete on `admin/ing-gate-control-panel`.*
