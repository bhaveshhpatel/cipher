# Cipher Signals Engine: Deep Analysis & Synthesis

> Generated: April 24, 2026
> Repository: https://github.com/bhaveshhpatel/cipher

---

## Current Engine (Phase 5A) — What It Actually Does

The signal pipeline flows as: **Tradier SSE tick → `parse_tradier_trade()` → `DedupCache` → `RepetitionAccumulator` → `build_composite_async()` → AI Swarm → Supabase + WebSocket**.

The composite score formula is:

```
composite = flow_score × 0.55 + backtest_score × 0.35 + vwp_factor × 0.10
```

Where `flow_score` is computed as:

```
flow_score = min(1.0, (total_premium / 10,000,000) × 0.65 + accel_0.15 + trades_0.20)
```

A BUY or SELL recommendation fires only when `composite ≥ 0.65`. The `RepetitionAccumulator` requires **≥3 trades and ≥$50K premium** in a 30-minute rolling window before emitting an episode. Sweep detection is handled in `DedupCache` as 3+ exchanges within a 5-second window.

---

## Three-Way Detailed Comparison

### Filter Layer Comparison

| Dimension | **Current (Phase 5A)** | **Proposal 1 (Stacked Sweep Catalyst)** | **Proposal 2 (4-Phase Holy Grail)** |
|---|---|---|---|
| **Sweep enforcement** | Detected post-ingest in `dedup.py` (3 exchanges / 5s) but NOT a hard discard filter | Hard reject non-sweeps at ingest | Hard reject non-sweeps at Phase 1 |
| **Bid/Ask aggression** | `bid_ask_classifier.py` exists, but not used as a hard filter — it's metadata only | Hard filter: calls AT_ASK, puts AT_BID | Hard filter: calls ≥ ask, puts ≤ bid |
| **Volume > OI** | NOT checked — `volume_premium_factor` uses OI but does not enforce V > OI | Hard filter, Phase 1 | Hard filter, Phase 1 ("New Bet" filter) |
| **OTM constraint** | NOT checked — strike position is not validated | Soft: OTM preference + OTM band scoring | Hard: 5–10% OTM only |
| **DTE constraint** | `REGISTRY_MAX_DTE=90` limits registry but no scoring penalty for DTE | Soft: DTE in target window scores +1 | Hard: DTE ≤ 21 days |
| **Premium threshold** | Episode threshold: ≥ $50K total | Soft score: above threshold +1 | Hard: $100K (small/mid), $500K (large), $1M "Golden" tag |
| **Spread/liquidity guard** | NOT implemented | Hard: discard wide spread / poor liquidity | Implied by OTM 5–10% constraint |
| **Catalyst check** | NOT implemented | Scores +2 if catalyst in DTE window | Implied "catalyst window" via DTE ≤ 21 |
| **Sector sympathy** | NOT implemented | Scores +1 if sector ETF aligned | Phase 3: correlated ticker sweep scan |
| **Technical confirmation** | NOT implemented | Scores +2 if price at S/R zone | Pre-alert check: no entry into resistance |
| **Stacking/repetition** | ≥3 trades / 30-min accumulator window | Wait for 2nd or 3rd confirming sweep | 3 sweeps same ticker/strike/expiry in 10-min window |
| **Scoring model** | Weighted numeric composite (0–1 scale) | Additive point system (max ~15 pts, threshold ≥10) | Phase gates — must pass all phases sequentially |
| **AI layer** | 12-agent Groq swarm with majority vote | Not included | Not included |
| **Exit signals** | NOT defined in engine | 4 explicit exit rules (time stop, counter-flow, level break, partial profit) | 3 exit signals: 2× premium, steam dry up (0 sweeps 24–48h), counter-flow put sweep |
| **Entry timing** | First episode fires alert | Wait for 2nd/3rd confirming sweep | Fire on 3rd stack in 10-min window |
| **Backtest integration** | `backtest_validator.py` (ticker/type/DTE/tier win-rate lookup) | Not included | Not included |

### Scoring Model Comparison

| Aspect | Current | Proposal 1 | Proposal 2 |
|---|---|---|---|
| **Model type** | Continuous weighted composite (0–1) | Discrete additive point score (0–~15) | Sequential hard gate (pass/fail phases) |
| **Min threshold** | composite ≥ 0.65 | Score ≥ 10, no hard filter failure | Must pass Phase 1 + 2 + 3 in order |
| **Acceleration bonus** | +0.15 to flow_score if `is_accelerating` | Implicit via "wait for 3rd print" | Explicit: 3 sweeps in 10-min window |
| **Golden signal tag** | `is_golden_sweep` flag persisted | Not named but implied by high score | "Golden Tag" at ≥$1M premium |
| **Risk management** | None in engine | Size rules, stop based on underlying level | Defined take profit (2×), counter-flow abort |

---

## Combined "Cipher Apex" Engine — The ~100% Win-Rate Approach

This merges the best structural elements of all three into one unified pipeline. The key insight is: **Proposal 2's hard gates are the skeleton, Proposal 1's scoring is the muscle, and Cipher's existing swarm is the brain.**

---

### Layer 0 — Dedup + Sweep Detection (Already Built)

Keep `DedupCache` exactly as is. Ensure non-sweep episodes are **tagged** `SWEEP: false` but not immediately discarded — they feed the accumulator for counter-flow detection only.

---

### Layer 1 — Hard Rejection Gates

> Add to `options_flow_parser.py` or new `signal_gate.py`

These must reject before the `RepetitionAccumulator` ever sees the event:

1. **Sweep-only gate:** `trade_type != SWEEP` → discard (reject blocks, singles, splits)
2. **Aggression gate:** For calls, `execution_price < ask_price` → discard. For puts, `execution_price > bid_price` → discard. (`bid_ask_classifier.py` already classifies this — just enforce it)
3. **Volume > OI gate:** `daily_volume <= open_interest` → discard (new bet filter)
4. **Spread gate:** `(ask - bid) / mid > 0.15` → discard (15% max spread)
5. **Minimum premium gate:** Individual trade premium ≥ $5K (configurable)

---

### Layer 2 — Stacking Accumulator (Upgrade `RepetitionAccumulator`)

Replace the current 30-min / ≥3 trades window with:

- **Tight window:** ≥3 sweeps on exact same `(ticker, strike, expiry, type)` within **10 minutes** (Proposal 2 Rule 7)
- **Episode premium gate:** Total episode premium ≥ $100K (small/mid-cap) or ≥ $500K (large-cap), with a Golden tag at ≥ $1M
- **OTM constraint:** Strike must be 3–12% OTM from current underlying price
- **DTE constraint:** ≤ 30 days (extends Proposal 2's 21-day rule to capture monthly cycles)

---

### Layer 3 — Composite Scoring (Upgrade `composite_signal_engine.py`)

Replace the current 3-component formula with a **hybrid additive+weighted** model:

| Signal Component | Points | Weight in Composite |
|---|---|---|
| Sweep confirmed | +2 (hard gate already passed) | Required |
| At ask / at bid | +2 (hard gate already passed) | Required |
| Volume > OI | +2 (hard gate already passed) | Required |
| Premium tier (≥$100K / ≥$500K / ≥$1M) | +1 / +2 / +3 | `premium_tier_score × 0.15` |
| OTM in 5–10% band | +1 | Part of `flow_score` |
| DTE ≤ 21 days | +1 | Part of `flow_score` |
| Positive OI change | +1 | Part of `flow_score` |
| Acceleration (`is_accelerating`) | +2 | Keep existing `+0.15` |
| Sector sympathy sweeps detected | +2 | New `sector_score × 0.10` |
| Backtest win-rate | Continuous 0–1 | Keep existing `× 0.30` |

New composite formula:

```
composite = flow_score × 0.45
          + backtest_score × 0.30
          + vwp_factor × 0.10
          + premium_tier × 0.05
          + sector_sympathy × 0.10
```

**Fire alert only when composite ≥ 0.72** (raised from 0.65 to reduce false positives), AND point total ≥ 10, AND no hard gate was bypassed.

---

### Layer 4 — Context Confirmation (New `context_validator.py`)

Before the alert fires to the user, run:

- **Technical level check:** Is the underlying at or near a key S/R level or breaking out? (Use a simple ATR-band check against recent high/low stored per ticker)
- **Sector ETF sympathy:** If NVDA triggers, scan for concurrent sweeps in AMD, MU, SMCI within 15 minutes
- **Catalyst proximity:** Check if earnings, FDA event, or known macro event falls within the DTE window (requires a calendar feed or earnings data integration)

---

### Layer 5 — Exit Signal Engine (New `exit_monitor.py`)

The current engine has **zero exit logic** — this is the biggest gap. Add:

| Exit Signal | Trigger | Action |
|---|---|---|
| **Partial profit** | Option premium 2× from entry | Scale out 50% of position |
| **Full exit — steam drying** | 0 new sweeps on same contract for 24–48h | Close remaining position |
| **Abort — counter-flow** | Validated opposing sweep (calls → put sweep, puts → call sweep) passes Layers 1–2 on same ticker | Immediate full exit |
| **Level break stop** | Underlying loses the technical S/R level that triggered the trade | Full exit |
| **Time stop** | DTE ≤ 3 days and no catalyst has materialized | Close to avoid theta decay |

---

## AI Phase 5 Swarm — Where It Fits in the Pipeline

The existing 12-agent swarm (`swarm_engine.py` + `ensemble_runner.py`) is currently a **post-scoring overlay** that runs after `build_composite()`. It is powerful but underutilized. Here's exactly where each agent tier maps to the filter layers:

### Optimal Swarm Placement Map

| Filter Layer | AI Agent Role | Function |
|---|---|---|
| **Layer 1 (Hard Gates)** | ❌ Don't use AI here | Hard gates must be deterministic and sub-millisecond — AI latency would kill throughput |
| **Layer 2 (Stacking Accumulator)** | **Dark Pool / Tape Reader** (Tier 3) | Analyze whether stacking pattern looks like institutional accumulation vs. noise; flag synthetic stacking patterns |
| **Layer 3 (Composite Scoring)** | **Options Flow Specialist + Quant/Statistical Arb** (Tier 2) | Re-score the flow_score and backtest_score components with contextual reasoning; detect anomalous IV behavior |
| **Layer 3 (Sector Sympathy)** | **Sector Rotation Strategist** (Tier 3) | Confirm whether sector flow is genuine rotation signal or correlated noise; identify lead/lag relationships |
| **Layer 4 (Context Confirmation)** | **Technical Analyst + Macro Strategist + Fundamental Analyst** (Tier 1) | Validate technical level, macro backdrop (FOMC, CPI proximity), and fundamental catalyst existence |
| **Layer 4 (Catalyst check)** | **Fundamental Analyst + Sentiment Analyst** (Tier 1/2) | Detect known catalysts from news sentiment; validate if the DTE window aligns with a real event |
| **Layer 5 (Exit Signals)** | **Risk Manager + Contrarian Analyst + Volatility Trader** (Tier 1/3) | Monitor counter-flow patterns, flag theta decay risk, issue abort signals when smart money reversal is detected |
| **Final verdict gate** | **Full swarm majority vote** | Current behavior — keep this as the final binary BUY/SELL/HOLD confirmation, but only fire when Layers 1–4 all pass |

---

### Recommended Swarm Architecture Change

Currently the swarm runs on **every episode** regardless of signal quality. This is expensive and noisy. Change to a tiered approach that maps directly to the existing `SWARM_N_AGENTS` env var snap logic:

| Swarm Tier | Agent Count | Trigger Condition | Purpose |
|---|---|---|---|
| **Fast path** | 3 agents | All episodes passing Layers 1–2 | Quick sweep-validity check only |
| **Standard path** | 6 agents | Episodes with composite ≥ 0.65 | Options Flow Specialist, Technical Analyst, Risk Manager |
| **Full swarm** | 12 agents | Golden Tag signals only (≥$1M premium, composite ≥ 0.72) | All 12 agents deliberate — highest-confidence alert |

This concentrates AI depth exactly where it matters most — on your rarest, highest-conviction signals — while reducing Groq API costs on lower-tier flow.

---

## Implementation Priority Order

1. **Immediate (highest ROI):** Enforce `bid_ask_classifier.py` output as a hard gate in Layer 1 — the code already exists, it just isn't enforced
2. **High:** Add `Volume > OI` check per event before `RepetitionAccumulator` ingest
3. **High:** Tighten accumulator window from 30 min → 10 min and add OTM + DTE constraints to episode validation
4. **Medium:** Build `sector_sympathy_scanner.py` — cross-ticker sweep correlation within a 15-min window
5. **Medium:** Raise composite alert threshold from 0.65 → 0.72
6. **Medium:** Build `exit_monitor.py` — the most glaring missing piece of the full trade lifecycle
7. **Lower:** Integrate catalyst calendar feed for DTE-window matching
8. **Lower:** Implement tiered swarm routing (3/6/12 agents based on signal tier)
9. **Phase 6:** Wire `TradeExecutor` into the exit monitor for automated position management
