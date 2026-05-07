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
| 8b | **ING-011b** | `is_aggressive` moneyness-blindness inflates `weighted_premium` for ITM PUT AT_BID episodes | ING-011 (moneyness band must exist on events) | ⏳ Deliberation COMPLETE (2026-05-06) — **Option B selected** — branch `ing/s11b-itm-aggression-weight` not yet created — Issue [#80](https://github.com/bhaveshhpatel/cipher/issues/80) |
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

All DDL in `012` was applied directly to the live Supabase project (`cipher-database`, ref: `kpajucxqlrteckfuafvq`) after migration `011_unique_constraints.sql` without a corresponding migration file. The `012` catch-up migration closes the repo schema drift so that fresh environments, branches, and resets reproduce