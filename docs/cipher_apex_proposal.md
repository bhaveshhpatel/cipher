# Cipher Apex Engine — Implementation Proposal

> Generated: 2026-04-27

---

## Honest Assessment First

The Apex engine is well-designed but has **3 components that need external dependencies you don't currently have**, and **2 components that need careful sequencing** to not break the existing ingestion pipeline. Flag these before committing:

### Missing External Dependencies

| Component | What's Needed | Status |
|-----------|--------------|--------|
| Catalyst proximity check (Layer 4) | Earnings calendar feed — Tradier has `/markets/calendar` but it's event-sparse. Need a dedicated earnings API (Benzinga, Nasdaq, or Polygon) | ❌ Not integrated |
| Technical S/R level check (Layer 4) | ATR-band computation needs OHLCV history per ticker stored somewhere (Redis or Supabase `ticker_levels` table) | ❌ No storage exists |
| Sector sympathy scan (Layer 4) | Needs a sector→ticker map and a real-time cross-ticker sweep window | ⚠️ Partially doable with existing bus |

Everything else is **fully buildable with what you already have.**

---

## Proposal: Phased Swap-Out Plan

Do NOT swap everything at once. The Apex engine has 5 new layers — doing them in one shot risks breaking the live stream with no rollback. Here's the correct order:

---

### Phase 1 — Hard Gates (Layer 1 of Apex)
**New file: `signal_gate.py`**  
**Touch: `tradier_stream.py` `_process_trade()`**

This is the safest first step — pure rejection logic, no new storage, no new services.

What gets added:
```
Sweep-only gate        → trade_type != SWEEP → drop
Aggression gate        → uses bid_ask_class already on OptionsFlowEvent
Volume > OI gate       → open_interest field already on OptionsFlowEvent
Spread gate            → (ask - bid) / mid > 0.15 → drop
Min premium gate       → fill_price × size × 100 < $5K → drop
```

**Ingestion layer impact:**
- L1–L4 (ingestion) stays completely untouched
- The gate fires **after L4 dedup**, **before RepetitionAccumulator**
- This is a new `Layer 0.5` conceptually — sits at the L4/L5 boundary
- `_stats["hard_rejected"]` counter added for observability

**No breaking changes to ingestion.** ✅

---

### Phase 2 — Stacking Accumulator (Layer 2 of Apex)
**Modify: `signals/repetition_accumulator.py`**

Current → Apex changes:

| Parameter | Current | Apex |
|-----------|---------|------|
| Window | 30 min | 10 min |
| Min trades | ≥3 | ≥3 sweeps (sweep-only, Phase 1 gate ensures this) |
| Key | `(ticker, contract_type)` | `(ticker, strike, expiry, contract_type)` — exact contract |
| Episode premium gate | ≥$50K | ≥$100K small/mid, ≥$500K large-cap (tier-aware) |
| OTM constraint | None | 3–12% OTM from underlying price |
| DTE constraint | None | ≤30 days |
| Golden tag | ≥$1M (existing) | ≥$1M (keep, feeds 12-agent swarm trigger) |

**Ingestion layer impact:**
- Accumulator key change means **existing in-memory episodes are invalidated on deploy** — fine since you wiped the DB
- `underlying_price` already on `OptionsFlowEvent` (from registry enrichment) — OTM check is a simple `abs(strike - underlying) / underlying`
- Tier-aware premium gate needs `contract_meta.tier` — already carried through pipeline ✅

---

### Phase 3 — Composite Scoring (Layer 3 of Apex)
**Modify: `signals/composite_signal_engine.py`**

New formula:
```python
composite = (flow_score      × 0.45
           + backtest_score  × 0.30
           + vwp_factor      × 0.10
           + premium_tier    × 0.05
           + sector_score    × 0.10)
```

New components to build:
- `premium_tier_score` — 0.33/0.67/1.0 mapped from episode total premium ($100K/$500K/$1M bands) → trivial
- `sector_score` — needs sector sympathy detection (see Phase 4)
- Fire threshold raised: `0.65 → 0.72`
- Point total gate: `≥10 points` (tracked in accumulator episode object)

**Ingestion layer impact:** None. This is purely signal layer. ✅

---

### Phase 4 — Context Confirmation (Layer 4 of Apex)
**New file: `services/context_validator.py`**

Three sub-components, ordered by buildability:

#### 4A — Sector Sympathy (Buildable Now)
```python
SECTOR_MAP = {
    "NVDA": ["AMD", "MU", "SMCI", "INTC"],
    "AAPL": ["MSFT", "GOOGL", "META"],
    # etc.
}
```
- Listen on the async bus for sweep events within 15-min window per sector group
- Already have the bus — just add a `sector_sweep_window` dict keyed by sector group
- `sector_score = 1.0` if ≥1 sympathy sweep detected, else `0.0`

#### 4B — Technical S/R Level Check (Needs New Storage)
- Requires a `ticker_levels` Supabase table: `{ticker, support, resistance, atr, updated_at}`
- Populated by a new background task that pulls OHLCV from Tradier `/markets/history` every 30 min
- ATR-band check: `abs(underlying_price - nearest_level) / atr < 0.5` → near key level
- **This needs a new DB migration** — one table, 5 columns

#### 4C — Catalyst Proximity (Needs External Feed)
- Tradier `/markets/calendar` is too sparse
- **Recommendation: defer this to a follow-up PR** and stub it as `catalyst_score = 0.5` (neutral) until an earnings API is integrated
- OR use a static earnings calendar JSON updated weekly as a stopgap

---

### Phase 5 — Exit Signal Engine (Layer 5 of Apex)
**New file: `services/exit_monitor.py`**

This is the most novel component — nothing like it exists in the current codebase.

| Exit Signal | Trigger | Implementation |
|-------------|---------|----------------|
| Partial profit | Premium 2× from entry | Track `entry_premium` in episode; poll current option price via Tradier `/markets/quotes` |
| Steam drying | 0 new sweeps on contract for 24–48h | Timestamp-based episode TTL in accumulator |
| Counter-flow abort | Opposing validated sweep passes L1–L2 | Bus listener watching for opposite `contract_type` on same ticker |
| Level break stop | Underlying loses S/R level | Requires 4B to be live first |
| Time stop | DTE ≤3 days, no catalyst | DTE already on `ContractMeta` |

**Key design decision:** Exit signals publish to a new `"exit_signals"` bus channel → new WebSocket message type `exit_alert` → frontend needs a new handler. This is a frontend + backend change.

---

### Phase 6 — Tiered Swarm Integration
**Modify: `services/swarm_engine.py` + `tradier_stream.py`**

Replace the current flat swarm call with tiered dispatch:

```python
if golden_tag:                          # ≥$1M + composite ≥ 0.72
    swarm_result = await run_swarm(n=12)
elif composite >= 0.65:                 # standard path
    swarm_result = await run_swarm(n=6)
else:                                   # fast path — L1-L2 pass only
    swarm_result = await run_swarm(n=3)
```

Agent-to-layer mapping from the spec is already aligned with `swarm_engine.py`'s existing agent definitions — no agent code changes needed, just dispatch routing.

---

## What Changes in the Ingestion Layer

Short answer: **almost nothing.** But there are 3 touches:

| Ingestion File | Change | Why |
|----------------|--------|-----|
| `tradier_stream.py` `_process_trade()` | Add `signal_gate.check(ev)` call after dedup, before accumulator | Phase 1 gate insertion point |
| `parsers/options_flow_parser.py` | Ensure `underlying_price` always populated (needed for OTM check) | Phase 2 OTM constraint |
| `utils/dedup.py` | Non-sweep events tagged `SWEEP: false` but NOT discarded — they feed counter-flow detection | Apex Layer 0 spec |

That's it. L1 (registry), L2 (stream workers), L3 (parser), L4 (dedup) are otherwise unchanged.

---

## Recommended Build Order

```
Phase 1  →  Phase 2  →  Phase 3  →  Phase 6 (swarm tiering)
                ↓
            Phase 4A (sector sympathy — no new DB)
                ↓
            Phase 4B (S/R levels — new DB migration)
                ↓
            Phase 5 (exit engine — new bus channel + frontend)
                ↓
            Phase 4C (catalyst — external feed, last)
```

Phase 1–3 + 6 are a complete, shippable first version of Apex with no external dependencies and no new DB tables. That alone gets you the hard gates, tighter accumulator, new scoring formula, and tiered swarm — which covers ~80% of the win-rate improvement.

---

## One Open Question Before Implementation

The **aggression gate** in Phase 1 says:
> For calls: `execution_price < ask_price` → discard  
> For puts: `execution_price > bid_price` → discard

This means you only keep trades hitting the ask (calls) or hitting the bid (puts). That's extremely strict — it will drop a significant portion of legitimate institutional flow that fills between bid/ask on large block orders.

**Decision needed:** Hard reject (drop the tick entirely) or soft flag (reduce score but don't reject)?  
This single decision meaningfully affects signal volume.
