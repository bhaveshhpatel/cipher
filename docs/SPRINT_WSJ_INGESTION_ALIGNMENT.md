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
| 3 | **ING-004** | Fallback `underlying_price` from registry | — | 🟡 IN PROGRESS — branch `ing/s4-underlying-price-fallback` |
| 4 | **ING-005** | Align OTM band thresholds registry ↔ accumulator | ING-004 | After ING-004 merges |
| 5 | **ING-006** | Directional aggression weighting on premium floor | ~~ING-001~~ resolved | ✅ UNBLOCKED — deliberation required |
| 6 | **ING-007** | Multi-day repeat window lookback (DB + cache) | ING-002, ING-003 | ✅ UNBLOCKED — deliberation required |
| 7 | **ING-008** | Volume vs. OI gate via registry injection | ING-004, ING-005 | After ING-004+005 |

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

#### Context
`_DEFAULT_DTE_PREMIUM_TIERS` is hardcoded in `repetition_accumulator.py` as a module-level constant. There is no mechanism to change floors without a code deploy. This is low risk while the system is early, but as Cipher matures there are legitimate use cases for an operator to dial signal sensitivity up or down — e.g., switching to a permissive preset during low-volatility periods to capture smaller institutional positioning.

The `_MIN_EVENT_PREMIUM` scalar from ING-002 also belongs in this story's scope — wire it together rather than adding a second partial config story later.

**Why named presets, not raw field editing:**
Eight interdependent DTE floors with no validation create a footgun. A misconfigured floor silently changes which episodes qualify, changing signal volume, which is hard to attribute without strong observability. Named presets constrain the decision surface: the operator chooses a validated methodology, not raw numbers. Custom mode exists for deliberate expert use with full awareness.

#### Preset Definitions

**WSJ-Strict (default — current hardcoded values):**
```python
_PRESET_WSJ_STRICT: Dict[int, Tuple[float, float]] = {
    7:    (50_000,    25_000),
    30:   (500_000,   100_000),
    90:   (1_000_000, 500_000),
    9999: (2_000_000, 1_000_000),
}
```

**WSJ-Permissive (half the strict T1 floors; T2/T3 column = ~half of strict T2/T3):**
```python
_PRESET_WSJ_PERMISSIVE: Dict[int, Tuple[float, float]] = {
    7:    (25_000,   10_000),
    30:   (100_000,  50_000),
    90:   (500_000,  250_000),
    9999: (1_000_000, 500_000),
}
```
Rationale: permissive T1 floors equal the current strict T2/T3 floors — consistent internal logic. Permissive T2/T3 floors are half again. SA deliberation required to confirm these values before hardcoding.

**Custom:** resolves floor values from 8 individual `ingestion_config` keys (see below).

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION

---

##### Senior Architect (SA)

**SA-Q1: Preset switch — live accumulator reload vs. next cold-start only**

When an operator switches from WSJ-Strict to WSJ-Permissive, do the new floors apply immediately (live reload path calling `accumulator.set_dte_premium_tiers()`) or only after the next service restart?

Trade-offs:
- **Live reload:** operator can see immediate signal rate change. Risk: accumulator is stateful — existing in-flight episodes have accumulated premium under the old floor. Switching floors mid-episode could cause an episode to retroactively pass or fail Gate 6 on the next tick. Acceptable for an enrichment system; dangerous if episodes are used for immediate trade routing.
- **Next cold-start only:** zero complexity, zero risk. Operator must restart the stream worker to apply new floors. Acceptable for a P2 admin feature.

**Decision required: Live reload or cold-start-only?**

**SA-Q2: Preset stored in `ingestion_config` key/value table vs. a new `ingestion_presets` table**

Current `ingestion_config` is a flat key/value string store with no validation. Storing `DTE_TIER_PRESET = "wsj-strict"` as a single key works. But if `Custom` mode is selected, 8 additional keys must also be coherent (e.g., `dte_floor_dte7_t1` must be > `dte_floor_dte7_t23`). The flat K/V table has no cross-key validation.

Options:
- **A (recommended):** Store preset name in `DTE_TIER_PRESET`. If `custom`, read 8 individual keys. Validate floor ordering in `get_dte_premium_tiers()` at load time — log ERROR and fall back to WSJ-Strict if ordering is violated. No new table.
- **B:** New `ingestion_presets` JSONB table. More structured; adds migration complexity.

**Decision required: Option A or B?**

**SA-Q3: `_MIN_EVENT_PREMIUM` in same story or separate?**

The original ING-002-CONFIG scope was only `_MIN_EVENT_PREMIUM`. This story expands to DTE presets. Confirm: wire `min_event_premium` through `ingestion_config` in this same story (single config load call at startup covers both), or defer `_MIN_EVENT_PREMIUM` config to a third micro-story?

Recommendation: same story — the loader (`get_dte_premium_tiers()`) and the config init path are the same code touched. Doing both in one PR is cheaper than two half-stories.

**Decision required: Same PR or separate?**

---

##### Principal Backend Engineer (PBE)

**PBE-Q1: Import-time vs. async load for `get_dte_premium_tiers()`**

`get_config()` in `ingestion_config.py` is `async`. `_DEFAULT_DTE_PREMIUM_TIERS` is available synchronously at import time. The accumulator is currently instantiated synchronously in `tradier_stream.py` at module level.

Two options:
- **A:** `get_dte_premium_tiers()` is a sync function that calls `get_config()` via `asyncio.run()` — acceptable at startup (not in the hot path), but `asyncio.run()` inside an already-running async context (FastAPI lifespan) will raise `RuntimeError`.
- **B (recommended):** `get_dte_premium_tiers()` returns the hardcoded preset synchronously at module-level init, then the lifespan startup hook calls `accumulator.set_dte_premium_tiers(await get_dte_premium_tiers_async())` to wire in the DB-sourced preset. Same two-phase pattern already used for `set_tier_map()`.

**Decision required: Confirm Option B two-phase load is the correct pattern.**

**PBE-Q2: Accumulator singleton is module-level in `tradier_stream.py` — can `set_dte_premium_tiers()` be added?**

`set_tier_map()` already exists on `RepetitionAccumulator` and uses `threading.Lock`. The same pattern extends naturally to `set_dte_premium_tiers()`:
```python
def set_dte_premium_tiers(self, tiers: Dict[int, Tuple[float, float]]) -> None:
    with self._tier_map_lock:  # reuse existing lock — tiers + tier_map reads are co-guarded
        self.dte_premium_tiers = tiers
        self._max_dte_key = max(tiers) if tiers else None
```
Confirm: reusing `_tier_map_lock` for both `_tier_map` and `dte_premium_tiers` is safe (same lock; no deadlock risk since neither holder calls back into the other).

**Decision required: Confirm lock reuse pattern.**

**PBE-Q3: `ingestion_config` cache TTL is 60 seconds — does this create an acceptable propagation delay for preset switches?**

When admin switches preset via `POST /api/admin/ingestion/dte-tiers`, `update_config()` already sets `_cache_ts = 0.0` (invalidates cache). The next `get_config()` call fetches fresh. If live reload is chosen (SA-Q1), the admin endpoint calls `accumulator.set_dte_premium_tiers()` directly after writing to DB — no TTL wait. If cold-start-only is chosen, TTL is irrelevant. Either way, no issue.

**Confirm: No TTL race condition regardless of SA-Q1 decision.**

**PBE-Q4: Validation of Custom floor ordering**

If `DTE_TIER_PRESET = "custom"`, the loader reads 8 keys and constructs the dict. Required invariant per DTE bucket: `T1_floor >= T2_T3_floor` (strict floor must be >= permissive floor). If violated (e.g., operator sets `dte_floor_dte7_t1 = 20_000` but `dte_floor_dte7_t23 = 30_000`), the gate logic silently inverts (T1 gets a lower floor than T2/T3). The loader must validate and fall back to WSJ-Strict with a logged ERROR if any bucket fails this invariant.

**Confirm: Validation logic belongs in `get_dte_premium_tiers()`, not in the admin endpoint, so DB direct edits are also caught.**

---

##### Lead QA (QA)

**QA-Q1: Preset load test matrix (required at startup)**

| Scenario | `DTE_TIER_PRESET` DB value | Expected result |
|---|---|---|
| Normal | `"wsj-strict"` | Returns `_PRESET_WSJ_STRICT` |
| Normal | `"wsj-permissive"` | Returns `_PRESET_WSJ_PERMISSIVE` |
| Custom valid | `"custom"` + all 8 keys present + T1>=T2/T3 per bucket | Returns constructed dict |
| Custom invalid | `"custom"` + `dte7_t1=20k`, `dte7_t23=30k` (inverted) | Logs ERROR, returns `_PRESET_WSJ_STRICT` |
| Unknown preset | `"wsj-aggro"` (typo) | Logs WARNING, returns `_PRESET_WSJ_STRICT` |
| DB unavailable | Supabase timeout | Returns `_PRESET_WSJ_STRICT` (hardcoded fallback) |
| Missing key | `DTE_TIER_PRESET` row absent from DB | Returns `_PRESET_WSJ_STRICT` (default in `_DEFAULTS`) |

All 7 cases are required tests.

**QA-Q2: Admin endpoint test matrix**

`POST /api/admin/ingestion/dte-tiers` with `{"preset": "wsj-permissive"}`:
- Assert 200 OK
- Assert `ingestion_config` row updated: `DTE_TIER_PRESET = "wsj-permissive"`
- If live reload (SA-Q1 decision): assert `accumulator.dte_premium_tiers` now equals `_PRESET_WSJ_PERMISSIVE`
- Assert activity log entry written: `action = "ingestion_config.dte_preset.update"`, `details.preset = "wsj-permissive"`

`POST /api/admin/ingestion/dte-tiers` with `{"preset": "unknown-value"}`:
- Assert 422 Unprocessable Entity

Non-admin user hitting endpoint:
- Assert 403 Forbidden

**QA-Q3: Regression — existing accumulator tests must not break**

`_DEFAULT_DTE_PREMIUM_TIERS` is imported by `tradier_stream.py` tests. Confirm the new preset dicts (`_PRESET_WSJ_STRICT`, `_PRESET_WSJ_PERMISSIVE`) do not shadow or replace the original constant — `_DEFAULT_DTE_PREMIUM_TIERS` should be aliased to `_PRESET_WSJ_STRICT` so all existing import references continue to work:
```python
# In repetition_accumulator.py:
_PRESET_WSJ_STRICT = { ... }
_DEFAULT_DTE_PREMIUM_TIERS = _PRESET_WSJ_STRICT  # backward-compat alias — do not remove
```

**QA-Q4: `/health/stream` must expose active preset name**

`get_stats()` should include `"dte_tier_preset": "wsj-strict"` (or whichever is active). This gives Railway log observers immediate visibility into which methodology is running without an admin panel visit.

---

#### Implementation Plan (sequential, after deliberation sign-off)

**Step 1 — `repetition_accumulator.py`**
- Define `_PRESET_WSJ_STRICT`, `_PRESET_WSJ_PERMISSIVE`
- Alias `_DEFAULT_DTE_PREMIUM_TIERS = _PRESET_WSJ_STRICT` (backward compat)
- Add `set_dte_premium_tiers()` method with lock (PBE-Q2 pattern)

**Step 2 — `services/ingestion_config.py`**
- Add to `_DEFAULTS`:
  ```python
  "DTE_TIER_PRESET":         "wsj-strict",
  "min_event_premium":       10_000,
  "dte_floor_dte7_t1":       50_000,
  "dte_floor_dte7_t23":      25_000,
  "dte_floor_dte30_t1":      500_000,
  "dte_floor_dte30_t23":     100_000,
  "dte_floor_dte90_t1":      1_000_000,
  "dte_floor_dte90_t23":     500_000,
  "dte_floor_leaps_t1":      2_000_000,
  "dte_floor_leaps_t23":     1_000_000,
  ```
- Add all 9 keys to `_EXPECTED_DB_KEYS`
- Add `async get_dte_premium_tiers() -> Dict[int, Tuple[float, float]]` with full validation logic (QA-Q1 cases)

**Step 3 — `services/tradier_stream.py`**
- Keep synchronous module-level instantiation using `_PRESET_WSJ_STRICT` (no change)
- In lifespan startup hook (after registry warms): `accumulator.set_dte_premium_tiers(await get_dte_premium_tiers())`
- Expose `dte_tier_preset` string in `_stats` / `get_stats()` (QA-Q4)

**Step 4 — `routers/admin.py`**
```python
class DteTierPresetUpdate(BaseModel):
    preset: str  # "wsj-strict" | "wsj-permissive" | "custom"

_VALID_PRESETS = {"wsj-strict", "wsj-permissive", "custom"}

@router.get("/ingestion/dte-tiers")
async def get_dte_tier_config(admin: TokenData = Depends(_require_admin)):
    """Return active preset name + resolved floor table."""
    from services.ingestion_config import get_config, get_dte_premium_tiers
    cfg = await get_config()
    active_preset = cfg.get("DTE_TIER_PRESET", "wsj-strict")
    resolved_tiers = await get_dte_premium_tiers()
    return {"active_preset": active_preset, "resolved_tiers": resolved_tiers}

@router.post("/ingestion/dte-tiers")
async def set_dte_tier_preset(
    body: DteTierPresetUpdate,
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    if body.preset not in _VALID_PRESETS:
        raise HTTPException(status_code=422, detail=f"Invalid preset '{body.preset}'. Valid: {sorted(_VALID_PRESETS)}")
    from services.ingestion_config import update_config, get_dte_premium_tiers
    ok = await update_config("DTE_TIER_PRESET", body.preset, updated_by=admin.email)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update DTE_TIER_PRESET")
    # Live reload (if SA-Q1 decision = live):
    from services.tradier_stream import accumulator
    new_tiers = await get_dte_premium_tiers()
    accumulator.set_dte_premium_tiers(new_tiers)
    await log_action(admin.email, "ingestion_config.dte_preset.update", {"preset": body.preset}, _get_ip(request))
    return {"ok": True, "preset": body.preset, "resolved_tiers": new_tiers}
```

**Step 5 — Supabase Migration**

```sql
-- Migration: add_ingestion_config_dte_preset_rows
INSERT INTO ingestion_config (key, value, value_type, description) VALUES
  ('DTE_TIER_PRESET',       'wsj-strict', 'string',  'Active DTE premium floor preset. Valid: wsj-strict | wsj-permissive | custom'),
  ('min_event_premium',     '10000',      'float',   'Per-event minimum premium floor at parser (ING-002). Hard gate before accumulation.'),
  ('dte_floor_dte7_t1',     '50000',      'float',   'Custom preset: T1 floor for DTE <= 7'),
  ('dte_floor_dte7_t23',    '25000',      'float',   'Custom preset: T2/T3 floor for DTE <= 7'),
  ('dte_floor_dte30_t1',    '500000',     'float',   'Custom preset: T1 floor for DTE 8-30'),
  ('dte_floor_dte30_t23',   '100000',     'float',   'Custom preset: T2/T3 floor for DTE 8-30'),
  ('dte_floor_dte90_t1',    '1000000',    'float',   'Custom preset: T1 floor for DTE 31-90'),
  ('dte_floor_dte90_t23',   '500000',     'float',   'Custom preset: T2/T3 floor for DTE 31-90'),
  ('dte_floor_leaps_t1',    '2000000',    'float',   'Custom preset: T1 floor for DTE > 90 (LEAPS)'),
  ('dte_floor_leaps_t23',   '1000000',    'float',   'Custom preset: T2/T3 floor for DTE > 90 (LEAPS)')
ON CONFLICT (key) DO NOTHING;
```

**Step 6 — Frontend admin panel card (separate frontend story)**
- 3-button toggle: **WSJ-Strict** / **WSJ-Permissive** / **Custom**
- When Custom: 4x2 editable table (DTE bucket rows x T1 / T2-T3 columns), values editable inline
- Active preset: green badge
- Warning banner when not WSJ-Strict: *"Non-default preset active. WSJ-Strict is the validated methodology."*
- Calls `GET /api/admin/ingestion/dte-tiers` on load; `POST /api/admin/ingestion/dte-tiers` on change
- Custom cell updates use existing `PATCH /api/admin/ingestion/config` endpoint (no new endpoint needed)

---

#### Acceptance Criteria
- [ ] `_PRESET_WSJ_STRICT` and `_PRESET_WSJ_PERMISSIVE` defined in `repetition_accumulator.py`
- [ ] `_DEFAULT_DTE_PREMIUM_TIERS` aliased to `_PRESET_WSJ_STRICT` — all existing import references unbroken
- [ ] `set_dte_premium_tiers()` method added to `RepetitionAccumulator` with lock
- [ ] `get_dte_premium_tiers()` async loader in `ingestion_config.py` with all 7 QA-Q1 scenarios handled
- [ ] T1 >= T2/T3 invariant validated per bucket for custom preset; violation falls back to WSJ-Strict with logged ERROR
- [ ] 9 new `ingestion_config` rows inserted via migration; `validate_ingestion_config()` reports all keys present on startup
- [ ] `GET /api/admin/ingestion/dte-tiers` returns active preset + resolved floor table
- [ ] `POST /api/admin/ingestion/dte-tiers` validates preset name (422 on unknown), writes to DB, live-reloads accumulator (per SA-Q1 decision)
- [ ] Activity log entry written on every preset change
- [ ] `"dte_tier_preset"` key in `/health/stream` response showing active preset name
- [ ] `min_event_premium` wired through `ingestion_config` in same PR; `_MIN_EVENT_PREMIUM` uses DB value with hardcoded fallback
- [ ] All 7 QA-Q1 test cases pass
- [ ] All QA-Q2 admin endpoint test cases pass
- [ ] No regression in existing accumulator or stream tests
- [ ] Frontend card ships as follow-on in same sprint window (not blocking backend merge)

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

#### ✅ 3-Way Deliberation — COMPLETE (2026-05-03)
**All three roles signed off. Story cleared for implementation. Branch in progress.**

#### Deliberation Outcomes

**SA-Q1: Parser-layer coupling — DECIDED: Non-issue — add to existing enrichment block**
- `get_registry()` is already imported at module level in `options_flow_parser.py` for test patchability
- The existing enrichment block already calls `reg.lookup(symbol)` and mutates `ev.ticker`, `ev.strike`, `ev.expiry`, `ev.dte`, `ev.open_interest`
- Adding `ev.underlying_price` to this same block is not a new architectural decision — it completes an existing enrichment pass
- No parameter injection needed. Two-source design (tick data → registry override) is already the established pattern.

**SA-Q2: Cold-start log visibility — DECIDED: Single startup INFO log only; no per-tick warnings**
- Per-tick warnings during 30-min cold-start window would generate thousands of log lines and bury real errors
- Single `INFO` log at stream start: `"[flow] underlying_price fallback: registry not ready at cold-start — OTM classification degraded until warmup"`
- `/health/stream` counter `underlying_price_fallback_applied` provides Railway-level observability

**SA-Q3: `stock_price()` vs `ContractMeta.underlying_price` — DECIDED: `reg.stock_price(ev.ticker)`**
- `ContractMeta` does not carry `underlying_price` — it carries contract-level data
- `SymbolRegistry.stock_price(ticker)` returns equity price fetched at chain build time
- `stock_price()` returns `0.0` (not raises) for unknown tickers — guard `if sp > 0` handles this cleanly

**PBE-Q1: Hot-path safety — DECIDED: Safe — O(1) dict read, no IO, no lock, no await**
- `get_registry()` is a module-level singleton read
- `reg.is_ready()` is a bool attribute read
- `reg.stock_price(ticker)` is a dict lookup on `_stock_prices`
- Identical safety profile to existing `reg.lookup(symbol)` call already in parser hot path

**PBE-Q2: Write-lock during concurrent `build()` — DECIDED: No additional locking needed**
- `build()` runs on background refresh loop. Worst case: stale price from previous build cycle
- Python dict reads are GIL-protected for the atomic read. Stale-by-one-cycle is acceptable for a price fallback.
- Same reasoning already accepted for `reg.lookup(symbol)` in the existing enrichment block.

**PBE-Q3: Exact placement in enrichment block — DECIDED: After meta block, inside same try/except**
- Fallback fires whether or not `meta` was found — `stock_price` is ticker-level, not contract-level
- Placed after meta enrichment so `ev.ticker` is already resolved to canonical ticker before `stock_price()` call
- Implementation:
```python
# ING-004: Fallback underlying_price from registry stock_price.
# Fires whether or not meta was found — stock_price is ticker-level, not contract-level.
if ev.underlying_price == 0.0:
    sp = reg.stock_price(ev.ticker)
    if sp > 0.0:
        ev.underlying_price = sp
        _stats["underlying_price_fallback_applied"] += 1
```

**PBE-Q4: Counter initialisation — DECIDED: Module-level in `_stats` init block**
```python
_stats: dict = {
    "below_min_premium":                  0,  # ING-002
    "underlying_price_fallback_applied":  0,  # ING-004
}
```

**QA-Q1: Full test matrix (5 cases D-1 through D-5) — ALL REQUIRED**

| Case | Setup | Expected |
|---|---|---|
| D-1 | `underlying_price=0`, registry ready, `stock_price=150.0` | `ev.underlying_price=150.0`; counter +1 |
| D-2 | `underlying_price=155.0`, registry ready | Registry NOT called for price; counter unchanged |
| D-3 | `underlying_price=0`, registry `is_ready()=False` | `ev.underlying_price=0.0`; counter unchanged |
| D-4 | `underlying_price=0`, registry ready, `stock_price()=0.0` | `ev.underlying_price=0.0`; counter unchanged |
| D-5 | `underlying_price=0`, registry ready, `stock_price=150.0`, `meta=None` | Fallback fires independently; counter +1 |

**QA-Q2: `/health/stream` visibility — DECIDED: Automatic via existing `get_parser_stats()` wiring**
- `get_stats()` returns `dict(_stats)`. No additional plumbing needed.

**QA-Q3: Regression — DECIDED: Guard `if sp > 0` ensures zero-mutation for unknowns**
- Existing tests constructing ticks with `underlying_price=0.0` and mock `stock_price=0.0` behave identically
- Confirm with `pytest backend/parsers/` pass after implementation

**QA-Q4: Cold-start INFO log — DECIDED: No test required**
- Startup observability log is infrastructure, not business logic. Not in test scope.

#### Acceptance Criteria
- [x] `ev.underlying_price` populated from `registry.stock_price()` when tick value is 0.0
- [x] Fallback placed inside existing `try: reg = get_registry()` block, after meta enrichment
- [x] Fallback fires whether or not `meta` was found (ticker-level, not contract-level)
- [x] Fallback only fires when `registry.is_ready()` is True AND `sp > 0`
- [x] No circular import — `get_registry` already imported at module level
- [x] `_stats["underlying_price_fallback_applied"]` initialised to `0` at module level
- [x] Counter increments on fallback application; stays 0 when tick already has price
- [x] `underlying_price_fallback_applied` visible in `/health/stream` via `get_parser_stats()`
- [x] All 5 QA test cases (D-1 through D-5) pass
- [x] No regression in existing parser tests

---

### ING-005 — Align OTM Band Thresholds: Registry ↔ Accumulator
**Type:** Bug Fix / Consistency
**Priority:** P1
**Estimated Effort:** 1 day
**Depends On:** ING-004 (reliable `underlying_price` required before OTM classification is meaningful)
**Files:** `backend/signals/repetition_accumulator.py`, `backend/services/tradier_stream.py`

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION

Options on the table:
- **Option A** — Retire DEEP_OTM multiplier entirely (registry already pre-filters OTM per tier)
- **Option B** — Pass tier `atm_pct` into accumulator for tier-aware DEEP_OTM boundary
- **Option C** — Quick patch: change hardcoded `0.12` → `0.20` to match T1 max

Open deliberation questions:
- SA-Q1: Does Option A throw away a still-useful distinction, or is the registry pre-filter sufficient?
- SA-Q2: If Option B — does passing `tier` into accumulator violate layer boundaries?
- PBE-Q1: If Option A — audit all `deep_otm_multiplier` and `_classify_otm()` call sites before removal.
- PBE-Q2: If Option B — confirm `tier` flows through `OptionsFlowEvent` to `_DictEventWrapper` correctly.
- QA-Q1: Regression matrix: T1 at 18% OTM, T2 at 14% OTM, T3 at 9% OTM per chosen option.
- QA-Q2: Confirm existing `test_classify_otm` tests updated, not deleted.

#### Acceptance Criteria
- [ ] OTM thresholds consistent with registry tier definitions
- [ ] Option chosen recorded in `docs/FIXES.md` under ING-005 with SA rationale
- [ ] T1 contract at 18% OTM no longer incorrectly penalised
- [ ] All 3 QA regression cases pass

---

### ING-006 — Directional Aggression Weighting on Premium Floor
**Type:** Feature / Gate Enhancement
**Priority:** P0
**Estimated Effort:** 1 day
**Depends On:** ING-002 (merged ✅)
**Files:** `backend/parsers/bid_ask_classifier.py`, `backend/parsers/options_flow_parser.py`, `backend/signals/repetition_accumulator.py`

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION

Open deliberation questions:
- SA-Q1: `AT_BID`/`BELOW_BID` on both PUT and CALL returns True. Does a size threshold prevent routine small MM fills being flagged as aggressive?
- SA-Q2: Should `is_aggressive` be persisted as a column in `flow_events` for ING-007 pattern quality scoring?
- PBE-Q1: Confirm `is_aggressive` is included in `OptionsFlowEvent.__dict__` serialisation path to `_DictEventWrapper`.
- PBE-Q2: `aggression_discount=0.5` — hardcoded initially or configurable via `ingestion_config` from day one?
- QA-Q1: 8-case test matrix for `is_directionally_aggressive()` (see story section below).
- QA-Q2: Accumulator weighted premium test: $80k aggressive + $40k passive → weighted=$100k.

#### Implementation

**Step 1 — Replace `is_aggressive()` with `is_directionally_aggressive()` in `bid_ask_classifier.py`:**
```python
def is_directionally_aggressive(
    bid_ask_class: str,
    contract_type: str,
) -> bool:
    ba    = (bid_ask_class or "").strip().upper()
    ctype = (contract_type or "").strip().upper()
    if ba in ("AT_ASK", "ABOVE_ASK"):
        return True
    if ba in ("AT_BID", "BELOW_BID") and ctype in ("PUT", "CALL"):
        return True
    return False
```

**Step 2 — Update `parse_tradier_trade()` to use new function.**

**Step 3 — Add `is_aggressive` to `_DictEventWrapper.__slots__`.**

**Step 4 — Add aggression-weighted premium check in `ingest_tick()`.**

**Step 5 — Retain old `is_aggressive(trade_type)` as deprecated.**

#### Acceptance Criteria
- [ ] `is_directionally_aggressive(bid_ask_class, contract_type)` replaces `is_aggressive(trade_type)` in parser
- [ ] All 8 QA test matrix cases pass
- [ ] Accumulator uses aggression-weighted premium for Gate 6 floor check
- [ ] `aggression_discount` parameter on `RepetitionAccumulator` with default 0.5
- [ ] Old `is_aggressive()` retained as deprecated
- [ ] `is_aggressive` field available in `_DictEventWrapper`

---

### ING-007 — Multi-Day Repeat Window Lookback
**Type:** Feature / New Gate
**Priority:** P1
**Estimated Effort:** 3 days
**Depends On:** ING-002 (merged ✅), ING-003 (merged ✅)
**Files:** `backend/services/flow_store.py`, `backend/services/tradier_stream.py`, `backend/utils/contract_day_cache.py` (new), Supabase migration

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION

**🚨 Infrastructure Prerequisite — Supabase Migration FIRST:**
```sql
CREATE INDEX idx_flow_events_contract_day
ON flow_events (ticker, contract_type, strike, expiry, created_at DESC);

ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS order_side TEXT DEFAULT 'UNKNOWN';
ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS is_aggressive BOOLEAN DEFAULT FALSE;
```
Apply migration, run `EXPLAIN ANALYZE` on lookback query, confirm index hit before any Python.

Open deliberation questions:
- SA-Q1: Hard gate vs. soft enrichment flag — recommend starting as flag (`require_multi_day=False`).
- SA-Q2: Lookback window — 5 calendar days vs. 5 trading days?
- SA-Q3: `prior_days_active` — calendar days or days-with-qualifying-flow only?
- PBE-Q1: `cachetools.TTLCache` — confirm in `requirements.txt` or evaluate stdlib alternative.
- PBE-Q2: Background queue eventual consistency — acceptable for flag, not for hard gate.
- PBE-Q3: `asyncio.Queue` backpressure — add `maxsize=5000` with overflow counter.
- QA-Q1: Integration test with seeded `flow_events` fixture (3 prior days). Assert `prior_days_active=3`.
- QA-Q2: Cache TTL expiry test — after 5 min, re-fetch from DB.
- QA-Q3: `_process_trade()` latency benchmark before/after.

#### Acceptance Criteria
- [ ] Supabase migration applied; `EXPLAIN ANALYZE` confirms index hit
- [ ] `get_prior_contract_volume()` returns correct results for seeded fixture
- [ ] Background worker populates cache; hot path is non-blocking
- [ ] `ep.is_multi_day_repeat` flag set correctly
- [ ] `_stats["multi_day_repeat_count"]` and `_stats["multi_day_not_met"]` counters in `/health/stream`
- [ ] No measurable latency increase on `_process_trade()`
- [ ] All QA test cases pass

---

### ING-008 — Volume vs. OI Gate via Registry Injection
**Type:** Feature / New Gate
**Priority:** P1
**Estimated Effort:** 2 days
**Depends On:** ING-004, ING-005
**Files:** `backend/signals/repetition_accumulator.py`, `backend/parsers/options_flow_parser.py`

#### ⚠️ 3-Way Deliberation — REQUIRED BEFORE IMPLEMENTATION

**🚨 Prerequisite — Chain API OI Verification Required Before Any Code:**
Run one-off chain fetch on AAPL/SPY. Document OI quality in `docs/FIXES.md` under ING-008 before writing gate logic.

Open deliberation questions:
- SA-Q1: Prior-day OI vs. intraday — false positives early in session when intraday vol naturally low?
- SA-Q2: T3 tickers with OI=0 — auto-skip or lower `vol_oi_min_ratio`?
- PBE-Q1: Live boolean feature flag mechanism in `ingestion_config` without restart.
- PBE-Q2: `vol_oi_min_ratio` per-tier vs. global scalar?
- QA-Q1: OI=0 edge case — gate MUST skip silently (not drop episode).
- QA-Q2: OI=500, size=499 → dropped. OI=500, size=500 → passes.
- QA-Q3: Gate observably disabled when `vol_oi_check_enabled=False`.

#### Acceptance Criteria
- [ ] `open_interest` sourced from `ContractMeta` when tick OI = 0
- [ ] `_DictEventWrapper` includes `open_interest`
- [ ] Gate implemented but disabled by default; toggled via `ingestion_config`
- [ ] `_stats["vol_oi_suppressed"]` in `/health/stream`
- [ ] Tradier chain API OI quality documented in `docs/FIXES.md` under ING-008
- [ ] All 3 QA edge case tests pass

---

## Sprint Exit Criteria
All 7 stories pass acceptance criteria AND:
- [ ] No regression in existing passing tests (`pytest backend/`)
- [ ] `/health/stream` exposes all new counters: `below_min_premium`, `underlying_price_fallback_applied`, `multi_day_repeat_count`, `multi_day_not_met`, `vol_oi_suppressed`, `dte_tier_preset`
- [ ] 3-way deliberation sign-off documented for every story in this file
- [ ] `docs/ARCHITECTURE.md` updated to reflect new gate structure
- [ ] `docs/CHANGELOG.md` updated with sprint summary
- [ ] `docs/ORDER_SIDE_RESOLUTION.md` referenced from `docs/ARCHITECTURE.md`

---

*Sprint created: 2026-05-03 | Last updated: 2026-05-03 (ING-004 pre-merge panel findings resolved — ING-002-CONFIG deliberation restored, test expiry date fixed) | Owner: Dhruv Patel | Classification: P0 — WSJ Ingestion Alignment*
