# Cipher Apex — Layered Signal Architecture

**Date:** April 30, 2026 (revised April 30, 2026 — architect review: issues 1, 2, 3, 9; issues 5, 6, 7, 8)
**Subject:** Full Apex Signal Pipeline — Ingestion through Signal Emission  
**Repository:** `bhaveshhpatel/cipher`

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         CIPHER APEX SIGNAL PIPELINE                             │
│                     Ingestion → Classification → Signal                         │
└─────────────────────────────────────────────────────────────────────────────────┘

╔═════════════════════════════════════════════════════════════════════════════════╗
║  ▌ INGESTION SUBSYSTEM  (Layers 0–3)                                           ║
╚═════════════════════════════════════════════════════════════════════════════════╝

┌─────────────────────────────────────────────────────────────────────────────────┐
│  LAYER 0 — SYMBOL REGISTRY                                                      │
│  services/symbol_registry.py                                                     │
│                                                                                 │
│  • Pre-loads ~16,000 OCC contract metadata into O(1) in-memory dict             │
│  • Refreshes every 30 min (15 min on expiry days)                               │
│  • Exposes: contract_type · strike · expiry · DTE · open_interest               │
│  • Exposes: stock_price(ticker) · get_daily_volume(ticker) via raw_quotes       │
│  • Exposes: tier map from TierEngine for T1/T2/T3 classification                │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │ registry ready signal
┌───────────────────────────────────▼─────────────────────────────────────────────┐
│  LAYER 1 — STREAM INGESTION                                                     │
│  services/stream_manager.py · services/tradier_stream.py                        │
│                                                                                 │
│  • Manages 32 parallel stream workers with staggered 200ms startup             │
│  • Global semaphore caps concurrent token fetches at 3                          │
│  • Respects Tradier 429 Retry-After headers — no retry storms                  │
│  • Routes raw tick payloads to parser                                           │
│  • Hot path: _process_trade() → parser → dedup → gate → accumulator →          │
│              ladder → composite → broadcast                                     │
│  • Demo mode: randomized order_side mapped through direction classifier         │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │ raw tick
┌───────────────────────────────────▼─────────────────────────────────────────────┐
│  LAYER 2 — PARSER + CLASSIFIER                                                  │
│  parsers/options_flow_parser.py · parsers/order_side_classifier.py             │
│  parsers/bid_ask_classifier.py  · parsers/trade_type_detector.py               │
│                                                                                 │
│  FILL RESOLUTION                                                                │
│  ├── fill = last ?? price ?? (bid+ask)/2                                        │
│  ├── if fill == 0 → return None (explicit guard)                               │
│  └── if size == 0 → return None                                                 │
│                                                                                 │
│  QUOTE CLASSIFICATION                                                           │
│  ├── is_synthetic_quote = (bid == 0 AND ask == 0)                              │
│  ├── if synthetic → synthesize ±0.5% NBBO from fill                            │
│  └── classify_bid_ask() → ABOVE_ASK | AT_ASK | MID | AT_BID | BELOW_BID       │
│                                                                                 │
│  DIRECTION INFERENCE  (order_side_classifier.py)                                │
│  ├── BUY side:  AT_ASK | ABOVE_ASK                                             │
│  ├── SELL side: AT_BID | BELOW_BID                                             │
│  ├── CALL + BUY  → BULLISH  (strong)   ← CI GATE INVARIANT (Issue 8)          │
│  ├── CALL + SELL → BEARISH  (strong)                                           │
│  ├── PUT  + BUY  → BEARISH  (strong)   ← CI GATE INVARIANT (Issue 8)          │
│  ├── PUT  + SELL → BULLISH  (strong)   ← CI GATE INVARIANT (original)         │
│  ├── MID  / synthetic → fallback to contract type, strong_sentiment=False      │
│  └── Returns OrderDirection(order_side, sentiment, strong_sentiment)           │
│                                                                                 │
│  TRADE TYPE DETECTION  (trade_type_detector.py)                                 │
│  ├── SWEEP:  exchange_cnt >= 3 AND fill_count >= 3                             │
│  ├── BLOCK:  (size >= 500 AND fill_count == 1)                                 │
│  │           OR (premium >= 500K AND exchange_cnt <= 2)                        │
│  ├── GOLDEN SWEEP:  SWEEP + is_directionally_aggressive + premium >= 500K     │
│  ├── GOLDEN BLOCK:  BLOCK + premium >= 1M                                      │
│  └── SPLIT / SINGLE: remaining classifications                                  │
│                                                                                 │
│  CONVICTION SCORING                                                             │
│  ├── is_directionally_aggressive = is_aggressive OR is_sell_aggressive         │
│  ├── base = 0.40 if directionally_aggressive else 0.15                        │
│  ├── +0.25 if golden sweep or golden block                                     │
│  ├── +min(premium / 10M, 0.25)                                                 │
│  └── +dte_urgency → capped at 1.0                                              │
│                                                                                 │
│  REGISTRY ENRICHMENT  (two-pass direction)                                      │
│  ├── Pass 1: classify_order_direction(ba_class, raw_ctype, is_synthetic)       │
│  ├── Registry lookup → update contract_type, strike, expiry, DTE, OI          │
│  ├── Pass 2: re-run classify_order_direction(ba_class, meta.contract_type, …) │
│  ├── Enrich underlying_price from reg.stock_price() if missing                 │
│  └── Enrich daily_volume from reg.get_daily_volume()                           │
│                                                                                 │
│  OUTPUT: OptionsFlowEvent with all fields populated                             │
│  Key fields: order_side · sentiment · strong_sentiment · trade_type            │
│              conviction_score · is_golden_sweep · underlying_price             │
│              daily_volume · open_interest · DTE · premium                      │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │ OptionsFlowEvent
┌───────────────────────────────────▼─────────────────────────────────────────────┐
│  LAYER 3 — DEDUPLICATION                                                        │
│  utils/dedup.py                                                                 │
│                                                                                 │
│  • 5-second TTL cache keyed on (occ_symbol, size, round(fill_price, 1))        │
│  • Sweep upgrade: 3+ exchanges reporting same trade within 8s → SWEEP           │
│  • Eliminates duplicate multi-exchange reports from same institutional print    │
│  • Clean events fan out to BOTH paths below simultaneously                     │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │ deduped event (fan-out)
           ┌────────────────────────┴──────────────────────────┐
           │                                                   │
           ▼  (concurrent — independent consumer)              ▼
┌──────────────────────────────┐             ┌─────────────────────────────────────┐
│  PERSISTENCE PATH            │             │  SIGNAL PATH                        │
│  (30-min window)             │             │  (10-min window)                    │
│  long-lived parallel write   │             │                                     │
│                              │             │  Continues into Apex Signal         │
│  services/flow_store.py      │             │  Subsystem below ↓                  │
│  • Buffer + flush 500ms      │             │                                     │
│  • 100-row immediate flush   │             │                                     │
│  • Writes flow_events        │             │                                     │
│  • Runs independently of     │             │                                     │
│    signal path outcome       │             │                                     │
│  • Fields: order_side,       │             │                                     │
│    strong_sentiment,         │             │                                     │
│    is_synthetic_quote,       │             │                                     │
│    is_aggressive,            │             │                                     │
│    conviction_score, ...     │             │                                     │
│                              │             │                                     │
│  ↺ continues for every       │             │                                     │
│    deduped event regardless  │             │                                     │
│    of signal path outcome    │             │                                     │
└──────────────────────────────┘             └──────────────────┬──────────────────┘
                                                                │
╔═══════════════════════════════════════════════════════════════╪═════════════════╗
║  ▌ APEX SIGNAL SUBSYSTEM  (Apex L1–L6)                        │                ║
╚═══════════════════════════════════════════════════════════════╪═════════════════╝
                                                                │
                                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  APEX L1 — SIGNAL GATE                                                          │
│  signals/signal_gate.py                                                         │
│                                                                                 │
│  HARD DISCARD RULES (noise rejection)                                           │
│  ├── Spread gate: spread > 50% → reject  (uniform across all tiers)            │
│  │   Deliberately permissive — pre-market and thin-hour institutional flow      │
│  │   on T1 names (NVDA, TSLA) routinely shows wide quoted spreads              │
│  ├── Synthetic quote: reject UNLESS premium clears institutional floor          │
│  │   (preserves pre-market institutional prints on illiquid contracts)          │
│  └── Zero-fill / zero-size → already caught in parser, confirmed here          │
│                                                                                 │
│  TIERED PREMIUM FLOORS BY TRADE TYPE                                            │
│  ├── T1: SWEEP >= 50K · BLOCK >= 100K · SPLIT >= 150K · SINGLE >= 250K        │
│  └── T2/T3: SWEEP >= 25K · BLOCK >= 50K · SPLIT >= 100K · SINGLE >= 150K     │
│                                                                                 │
│  PREVIOUSLY BROKEN GATES — REMOVED                                              │
│  ├── Volume > OI hard reject → REMOVED (inverts signal, moved to L3 boost)    │
│  └── Sweep-only gate → REMOVED (would drop blocks, icebergs, pre-market)      │
│                                                                                 │
│  OUTPUT: GateVerdict(passed: bool, reason: str)                                 │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │ passed events only
┌───────────────────────────────────▼─────────────────────────────────────────────┐
│  APEX L2 — DUAL-WINDOW ACCUMULATOR                                              │
│  signals/repetition_accumulator.py                                              │
│                                                                                 │
│  WINDOW SPLIT                                                                   │
│  ├── Signal window:      10 minutes (precision — sweep campaigns complete fast)│
│  └── Persistence window: 30 minutes (breadth — historical depth preserved)     │
│                                                                                 │
│  DTE-AWARE PREMIUM FLOORS                                                       │
│  ├── 0–7 DTE:    T1 = 50K    · T2/T3 = 25K                                    │
│  ├── 8–30 DTE:   T1 = 500K   · T2/T3 = 100K                                   │
│  ├── 31–90 DTE:  T1 = 1M     · T2/T3 = 500K                                   │
│  └── 91+ DTE:    T1 = 2M     · T2/T3 = 1M                                     │
│                                                                                 │
│  OTM ELIGIBILITY  (Issue 6 — April 30 2026)                                    │
│  ├── ATM band: abs(strike - underlying_price) / underlying_price <= 0.02       │
│  │   Definition: ±2% of underlying price (NOT an absolute dollar amount)       │
│  │   Rationale: NVDA at $900 has a $9 gap per 1% — absolute thresholds break  │
│  ├── ATM (0–2% OTM): eligible at standard premium floors                      │
│  ├── Standard OTM (2–12%): eligible at standard premium floors                │
│  ├── Deep OTM (>12%): eligible at 1.5× premium floor multiplier               │
│  └── underlying_price == 0 → standard floor, no OTM classification attempted  │
│                                                                                 │
│  SWEEP BYPASS  (Issue 7 — April 30 2026)                                       │
│  ├── Condition: len(ep.events) == 1 · trade_type == SWEEP · premium >= 500K   │
│  ├── IMPORTANT: len(ep.events) = episode event count, NOT fill_count           │
│  │   fill_count is a field on individual OptionsFlowEvent (fills within tick)  │
│  │   len(ep.events) == 1 means exactly one event entered the accumulator       │
│  ├── Bypasses min_sweeps when single massive sweep makes repetition moot       │
│  └── Negative: len(ep.events) == 2 with same SWEEP + premium → NO bypass      │
│                                                                                 │
│  EPISODE DIRECTION                                                              │
│  ├── dominant_direction: premium-weighted across all episode events            │
│  ├── Maps each event via order_side_to_direction(order_side, contract_type)    │
│  ├── BUY  + CALL → REPEAT_BUY   ← CI GATE INVARIANT (Issue 8)                 │
│  ├── BUY  + PUT  → REPEAT_SELL  ← CI GATE INVARIANT (Issue 8)                 │
│  ├── SELL + PUT  → REPEAT_BUY   ← CI GATE INVARIANT (original)                │
│  └── SELL + CALL → REPEAT_SELL  ← CI GATE INVARIANT (original)                │
│                                                                                 │
│  ALERT LEVELS (on total episode premium)                                        │
│  ├── >= 2M → CONVICTION                                                        │
│  ├── >= 500K → STRONG_SIGNAL                                                   │
│  ├── >= 100K → ALERT                                                           │
│  └── below → WATCH                                                             │
│                                                                                 │
│  OUTPUT: RepetitionEpisode with dominant_direction · alert_level · events      │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │ qualifying episode
┌───────────────────────────────────▼─────────────────────────────────────────────┐
│  APEX L4 — LADDER DETECTOR  [runs before composite — feeds sector_score]        │
│  signals/ladder_detector.py                                                     │
│                                                                                 │
│  • Scans all active episodes for the same ticker + same expiry                 │
│  • Fires when 3+ distinct strikes accumulate within the active window          │
│  • Produces LadderSignal: ticker · expiry · strikes[] · total_premium          │
│  • Output passed as context into Apex L3 composite scorer below                │
│  • Does not fire across different expiries (prevents false positives)           │
│  • Stale episode state is evicted via normal accumulator TTL                   │
│                                                                                 │
│  WHY THIS MATTERS:                                                              │
│  Multi-strike same-expiry positioning is cross-contract confirmation.           │
│  NVDA 600C + 590C + 580C sweeps within 15 min = deliberate ladder,             │
│  not coincidence. Stronger signal than any single strike in isolation.          │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │ episode + ladder context
┌───────────────────────────────────▼─────────────────────────────────────────────┐
│  APEX L3 — COMPOSITE SCORER  [receives ladder context from L4]                  │
│  signals/composite_signal_engine.py                                             │
│                                                                                 │
│  FORMULA (production weights)                                                   │
│  ├── flow_score              × 0.55                                            │
│  ├── volume_premium_factor   × 0.20                                            │
│  ├── premium_tier_score      × 0.15                                            │
│  ├── sector_score            × 0.10  (reserved — activates when L4 ladder      │
│  │                                    data is wired in S5; 0.0 until then)     │
│  └── backtest_score          × 0.00  (zero until S8 real implementation)      │
│                                                                                 │
│  SCORE CEILING  (Issue 5 — April 30 2026)                                      │
│  ├── While sector_score == 0.0 and backtest_score == 0.0, active weights       │
│  │   sum to 0.90 → composite_score is silently capped at 0.90                  │
│  ├── Decision: weights stay unchanged (redistributing 0.10 would invalidate    │
│  │   threshold calibration done against the 0.55/0.20/0.15 split)             │
│  ├── Ceiling exposed explicitly in composite bus payload:                      │
│  │   composite_score_ceiling: 0.90                                             │
│  └── Field removed from payload when S5 wires real ladder context and          │
│      sector_score receives a non-zero value                                     │
│                                                                                 │
│  FLOW SCORE INPUTS                                                              │
│  ├── conviction_score from parser                                               │
│  ├── is_golden_sweep / is_golden_block boost                                   │
│  ├── is_accelerating episode flag                                               │
│  └── sentiment confidence discount: ×0.80 if strong_sentiment == False        │
│                                                                                 │
│  VOLUME/PREMIUM FACTOR                                                          │
│  ├── Uses real open_interest from registry (not stream tick OI)                │
│  └── Volume > OI: score boost, not a discard gate                              │
│                                                                                 │
│  EPISODE INFLUENCE TIER  (not last-tick tier)                                  │
│  ├── >= 2M total episode premium → WHALE                                       │
│  ├── >= 500K → INSTITUTIONAL                                                   │
│  ├── >= 100K → LARGE                                                           │
│  └── < 100K → RETAIL                                                          │
│                                                                                 │
│  OUTPUT: CompositeSignal with composite_score · composite_score_ceiling         │
│          recommendation · reasoning · backtest_score=0.0 · flow_score          │
│          alert_level · influence_tier                                           │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │ composite signal
┌───────────────────────────────────▼─────────────────────────────────────────────┐
│  APEX L5 — SIGNAL BROADCAST + PERSISTENCE                                       │
│  services/tradier_stream.py (_process_trade hot path)                           │
│                                                                                 │
│  DIRECTION RESOLUTION (final)                                                   │
│  ├── direction = sig_ep.dominant_direction  (not contract_type shortcut)       │
│  └── Carries SELL PUT → REPEAT_BUY correctly into all downstream sinks         │
│                                                                                 │
│  EPISODE PERSISTENCE                                                            │
│  ├── persist_flow_episode() writes to flow_episodes table                      │
│  ├── Fields: direction · dominant_direction · order_side · strong_sentiment    │
│  └── influence_tier from episode_influence_tier(ep)                            │
│                                                                                 │
│  SIGNAL BUS MESSAGES                                                            │
│  ├── type: "signal" — per-tick qualified event                                 │
│  └── type: "composite_signal" — episode-level composite recommendation         │
│                                                                                 │
│  COMPOSITE SIGNAL PAYLOAD                                                       │
│  ├── signal: ticker · recommendation · composite_score                         │
│  │           composite_score_ceiling: 0.90  ← explicit ceiling (pre-S5)       │
│  │           flow_score · backtest_score · reasoning · alert_level             │
│  │           order_side · strong_sentiment                                     │
│  └── episode: contract_type · direction · influence_tier · total_premium       │
│               trade_count · is_accelerating · timestamp                         │
│                                                                                 │
│  NOTE: composite_score_ceiling removed from payload when S5 ladder             │
│  context is wired and sector_score receives a non-zero value.                  │
│                                                                                 │
│  FRONTEND BROADCAST                                                             │
│  └── Supabase Realtime → flow_episodes + signal_history channels               │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │ (future — S7)
┌───────────────────────────────────▼─────────────────────────────────────────────┐
│  APEX L6 — TIERED SWARM  [BLOCKED — pending stream worker review]               │
│  signals/apex_swarm.py  (not yet implemented)                                  │
│                                                                                 │
│  • Only allowed swarm layer in the entire pipeline                              │
│  • Tiered activation: 3 agents (ALERT) · 6 agents (STRONG) · 12 (CONVICTION)  │
│  • Hard async timeout: 2 seconds on 3-agent path                               │
│  • Circuit breaker: 3 consecutive timeouts → open for 5 min                   │
│  • Fallback: deterministic scoring via build_composite() always available      │
│  • Prerequisite: confirm _process_trade() is async-safe before implementing   │
│                                                                                 │
│  WHY BLOCKED:                                                                   │
│  If stream_worker.py processes events sequentially, any awaited Groq call      │
│  stalls the entire ingestion hot path. Safety must be confirmed first.          │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Layer Descriptions

---

### Layer 0 — Symbol Registry
**File:** `services/symbol_registry.py`

The registry is the foundation of the entire pipeline. It pre-loads the full ~16,000-symbol OCC options universe into memory at startup, eliminating any need for per-tick API calls. Every downstream layer depends on it for authoritative contract metadata.

**What it provides:**
- `lookup(occ_symbol)` → contract metadata: contract type, strike, expiry, DTE, open interest
- `stock_price(ticker)` → current underlying price from raw quote cache
- `get_daily_volume(ticker)` → daily stock volume from raw quote cache
- Tier map from TierEngine: T1 (mega-cap), T2 (large-cap), T3 (everything else)

**Refresh cadence:** Every 30 minutes during market hours. Every 15 minutes on expiry days when contract expirations shift the universe rapidly.

**Why it matters:** Without a warm registry, the parser cannot enrich underlying price, contract type, or open interest. Signals based on stale or missing metadata would have incorrect OTM bands, wrong conviction scores, and broken direction inference.

---

### Layer 1 — Stream Ingestion
**Files:** `services/stream_manager.py`, `services/tradier_stream.py`

The ingestion layer manages the live Tradier WebSocket stream across 32 parallel workers. Three critical fixes govern its safety characteristics.

**Worker startup (B-021):** Workers stagger their startup at 200ms intervals to prevent simultaneous token fetch bursts. 32 workers spread across 6.4 seconds instead of all hitting the Tradier auth endpoint at once.

**Token semaphore (B-022):** A global `asyncio.Semaphore(3)` caps concurrent `get_session_token()` calls at 3 regardless of worker count. This prevents account throttling under Tradier's single-session policy.

**429 handling (B-023):** When Tradier returns HTTP 429, the worker reads `Retry-After` and sleeps the exact server-specified duration before retrying. No retry storms.

**Hot path:** Each raw tick is routed through `_process_trade()`, which chains: parser → deduplication → Apex L1 gate → Apex L2 accumulator → Apex L4 ladder → Apex L3 composite → Apex L5 broadcast. Direction in the final publish step comes from `sig_ep.dominant_direction`, not a naive contract-type shortcut.

---

### Layer 2 — Parser and Classifier
**Files:** `parsers/options_flow_parser.py`, `parsers/order_side_classifier.py`, `parsers/bid_ask_classifier.py`, `parsers/trade_type_detector.py`

The parser is the most complex layer. It transforms raw stream bytes into a fully enriched `OptionsFlowEvent` with correct trade type, direction, sentiment, and conviction.

**Fill resolution:** Uses `last` as the primary fill price, falls back to `price`, then mid-price from bid/ask. Explicit `fill == 0` guard returns None before any further processing.

**Quote classification:** Identifies whether the fill was above the ask (urgent buyer), at the ask (initiating buyer), mid (ambiguous), at the bid (initiating seller), or below the bid (urgent seller). Synthetic quotes (bid=ask=0) are flagged and given a synthesized ±0.5% NBBO.

**Direction inference:** This is the most important semantic layer. The direction of a trade cannot be inferred from contract type alone. A SELL PUT is bullish, not bearish. The `order_side_classifier.py` module computes direction from the combination of bid/ask placement and contract type, producing an `OrderDirection` with `order_side`, `sentiment`, and `strong_sentiment`.

**Two-pass enrichment:** Direction is computed once on raw stream data and again after registry lookup may correct the contract type. The second pass is authoritative. The registry block never overwrites sentiment directly — it only updates contract metadata fields, and then direction is re-derived.

**Trade type detection:** BLOCK detection was extended beyond size heuristics to include high-premium, low-exchange-count prints (the institutional block pattern). Golden classification covers both SWEEP (multi-exchange aggression) and BLOCK (concentrated institutional prints at $1M+).

**Conviction scoring:** The conviction formula was made direction-symmetric. Both buy-side aggression (AT_ASK) and sell-side aggression (AT_BID) now contribute equally to the score. Previously, only buy-side aggression was recognized.

---

### Layer 3 — Deduplication
**File:** `utils/dedup.py`

Multi-exchange options prints generate multiple stream events for the same underlying execution. Deduplication collapses these into a single clean event and upgrades trade type when exchange count qualifies.

**Cache key:** `(occ_symbol, size, round(fill_price, 1))` with a 5-second TTL.

**Sweep upgrade:** If the same execution is reported by 3 or more exchanges within 8 seconds, the event is upgraded to `SWEEP`. This is how real cross-exchange sweeps are detected.

**Fan-out at this layer:** Clean deduplicated events are dispatched simultaneously to two independent, concurrent consumers. The persistence consumer (`flow_store.py`) runs with a 30-minute window and writes every qualifying event to `flow_events` regardless of what happens downstream in the signal path. The signal consumer feeds into the Apex subsystem with a 10-minute window. These paths are not sequentially chained — a signal path rejection does not suppress a persistence write, and a persistence write does not imply a signal was emitted.

---

### Apex L1 — Signal Gate
**File:** `signals/signal_gate.py`

The first Apex-specific layer. Its job is to discard noise before it reaches the accumulator. Two originally proposed gates were removed because they would have silently dropped real institutional flow.

**Removed: sweep-only gate.** Institutional desks use blocks, icebergs, and pre-market single-exchange prints as much as they use sweeps. A sweep-only gate would discard $2M blocks, pre-market single-exchange opening prints, and LEAPS accumulation — all high-signal categories.

**Removed: Volume > OI hard reject.** This inverted the signal. High volume relative to OI means new positioning, which is exactly what the platform is trying to detect. This was moved to a positive scoring boost in L3 instead.

**Retained: spread gate (50% uniform threshold).** The spread gate rejects any quote where `spread > 50%` of the ask price, applied uniformly across all tiers. This threshold is deliberately permissive: pre-market flow on T1 names like NVDA and TSLA routinely shows wide quoted spreads that would be incorrectly rejected by a tighter tier-specific cap. The gate targets genuine junk — zero-bid, extreme-width synthetic quotes — not normal thin-market conditions.

**Retained: premium floors by trade type.** Floors are set per tier and per trade type. SWEEP is given the lowest floor (it already proves multi-exchange urgency). SINGLE requires the highest floor (no structural urgency signal, so premium must compensate).

---

### Apex L2 — Dual-Window Accumulator
**File:** `signals/repetition_accumulator.py`

The accumulator groups individual qualifying ticks into episodes — structured sequences of related flow on the same ticker, strike, and expiry. An episode that clears the accumulator's criteria becomes a qualified signal.

**Window split:** The signal window is 10 minutes. Institutional sweep campaigns on a specific strike typically complete within minutes. The 30-minute persistence window was designed for historical depth, not real-time signal precision.

**DTE-aware floors:** Different expiry structures carry different information. A $50K SWEEP on a 0DTE contract is extremely urgent. The same $50K on a 90DTE contract is routine positioning noise. Premium floors scale with DTE to reflect this.

**ATM eligibility and OTM classification (Issue 6):** ATM is defined as `abs(strike - underlying_price) / underlying_price <= 0.02` — a ±2% band expressed as a fraction of underlying price, not an absolute dollar amount. This prevents incorrect exclusion on high-priced underlyings (e.g., NVDA at $900+ where a $9 gap is only 1%). Contracts in the ATM band use standard premium floors. Deep OTM (>12%) requires a 1.5× premium multiplier. Events with `underlying_price == 0` fall back to standard floor with no OTM classification attempted.

**Sweep bypass (Issue 7):** A single episode event (`len(ep.events) == 1`) with `trade_type == SWEEP` and `premium >= $500K` can bypass the `min_sweeps` requirement. `len(ep.events)` is the count of `OptionsFlowEvent` objects accumulated in the episode — this is NOT the `fill_count` field on an individual event, which counts fills within a single stream tick. When one massive sweep enters the accumulator as episode event #1, the repetition threshold adds no information — the bypass fires immediately.

**Dominant direction:** The episode's overall direction is computed as the premium-weighted sum of all constituent events' directions. A SELL PUT campaign produces `REPEAT_BUY` even if a few mid-prints happened to be ambiguous. This is not a last-event shortcut — it uses the full episode history.

---

### Apex L4 — Ladder Detector
**File:** `signals/ladder_detector.py`

The ladder detector runs **before** the composite scorer and passes its output as context into the L3 composite formula. It identifies coordinated multi-strike positioning on the same ticker and expiry — a form of cross-contract confirmation that is significantly stronger than any single-strike episode in isolation.

**Execution order in hot path:** Apex L2 (accumulator) → **Apex L4 (ladder)** → Apex L3 (composite, receives ladder context) → Apex L5 (broadcast). This ordering is required because `sector_score` in L3 is the reserved slot for ladder context. If L4 ran after L3, the sector input would always be zero.

**What constitutes a ladder:** Three or more distinct strikes on the same underlying and same expiry that each have active qualifying episodes within the current window.

**Why it matters:** When an institution buys NVDA 600C, 590C, and 580C sweeps within 15 minutes, this is a deliberate positioning structure across strikes, not coincidental overlap. The total intent is directional and the conviction across strikes makes it higher quality than any single strike alone.

**Output:** `LadderSignal` with ticker, expiry, strikes list, and combined total premium. This feeds the reserved `sector_score` input in L3 once wired in S5+S6. When S5 wires real ladder context, `sector_score` receives a non-zero value and `composite_score_ceiling` is removed from the bus payload.

---

### Apex L3 — Composite Scorer
**File:** `signals/composite_signal_engine.py`

The composite scorer receives both the qualifying episode from L2 and the ladder context from L4, then combines multiple independent signal dimensions into a single score and recommendation.

**Score ceiling (Issue 5):** With `sector_score = 0.0` and `backtest_score = 0.0`, the active weights sum to 0.90, capping `composite_score` at 0.90 until S5 wires ladder context. The weights were deliberately left unchanged — redistributing the 0.10 would shift the scoring baseline and invalidate threshold calibration. Instead, the ceiling is exposed explicitly in the composite bus payload as `composite_score_ceiling: 0.90`. Frontend consumers should treat scores above 0.85 as effectively maximum conviction pre-S5. This field is removed when S5 wires real ladder data.

**Backtest weight is zero.** The prior implementation used a seeded pseudorandom score. Any non-zero contribution from a fake number degrades every recommendation. Backtest weight will remain zero until S8 implements a real historical win-rate computation from `flow_events`.

**Sentiment confidence discount.** When `strong_sentiment` is False (mid-prints, synthetic quotes, or ambiguous fills), the flow score is multiplied by 0.80. Low-confidence direction should not score the same as clean directional flow.

**Volume > OI boost.** High volume relative to open interest indicates new positioning — fresh money entering a contract that previously had little activity. This is now a positive score input in L3, not a discard gate in L1.

**Episode influence tier.** The tier published in composite output is derived from total episode premium, not the latest tick's premium. A campaign that has accumulated $2.5M over 10 minutes publishes as WHALE even if the last tick was a small $40K add.

---

### Apex L5 — Signal Broadcast and Persistence
**File:** `services/tradier_stream.py` (hot path)

The broadcast layer is where all upstream computation becomes durable records and real-time bus messages. It uses the correctly computed episode direction throughout — no contract-type shortcuts survive to this layer.

**Direction:** `sig_ep.dominant_direction` is the final direction for all sinks. This correctly produces `REPEAT_BUY` for SELL PUT campaigns.

**Persistence fields:** All direction metadata is persisted — `order_side`, `strong_sentiment`, and episode-level fields. Schema migration S2.5 must be deployed before this layer's writes are activated.

**Composite signal payload:** The bus message includes `composite_score_ceiling: 0.90` while sector_score is inactive (pre-S5). This field is removed once S5 wires real ladder context. The message also includes both tick-level metadata (order_side, strong_sentiment) and episode-level metadata (direction, influence_tier, total_premium, is_accelerating).

**Frontend broadcast:** Supabase Realtime pushes INSERT events to frontend clients via `flow_episodes` and `signal_history` channels. No changes to this mechanism — it is inherently event-driven.

---

### Apex L6 — Tiered Swarm (Future — Blocked)
**File:** `signals/apex_swarm.py` (not yet implemented)

The swarm layer is the only place in the entire pipeline where AI model calls are allowed. All other layers are deterministic.

**Activation tiers:** 3 agents for ALERT signals, 6 for STRONG_SIGNAL, 12 for CONVICTION. This prevents the current always-on 12-agent pattern from running on every qualifying tick.

**Safety requirements:** Hard 2-second timeout on the 3-agent path. Circuit breaker opens after 3 consecutive timeouts and routes to deterministic fallback for 5 minutes.

**Blocking condition:** Before any swarm code is written, `stream_worker.py` must be reviewed to confirm the async model of `_process_trade()`. If events are processed sequentially, an awaited Groq call stalls the entire ingestion pipeline at 2,500 events per minute. That is unacceptable. Implementation does not start until this is confirmed safe.

---

## CI Gate Invariants

These invariants are enforced by dedicated tests that run before any other test in the suite. They cannot regress.

> **Updated April 30 2026 (Issue 8 resolution):** All four direction quadrants are now required
> CI invariants. The original table covered only SELL-side cases. BUY CALL and BUY PUT are
> equally regressionable — a parser refactor breaking BUY PUT direction would not have been
> caught by the original invariant set.

### SELL-Side Invariants (original)

| Invariant | Test assertion |
|---|---|
| AT_BID + PUT = SELL side, BULLISH sentiment | `classify_order_direction("AT_BID", "PUT", False).sentiment == "BULLISH"` |
| BELOW_BID + PUT = SELL side, BULLISH sentiment | `classify_order_direction("BELOW_BID", "PUT", False).sentiment == "BULLISH"` |
| AT_BID + PUT = order_side SELL | `classify_order_direction("AT_BID", "PUT", False).order_side == "SELL"` |
| AT_BID + PUT = strong_sentiment True | `classify_order_direction("AT_BID", "PUT", False).strong_sentiment == True` |
| SELL + PUT maps to REPEAT_BUY | `order_side_to_direction("SELL", "PUT") == "REPEAT_BUY"` |
| SELL + CALL maps to REPEAT_SELL | `order_side_to_direction("SELL", "CALL") == "REPEAT_SELL"` |

### BUY-Side Invariants (added — Issue 8)

| Invariant | Test assertion |
|---|---|
| AT_ASK + CALL = BUY side, BULLISH sentiment | `classify_order_direction("AT_ASK", "CALL", False).sentiment == "BULLISH"` |
| AT_ASK + CALL = order_side BUY | `classify_order_direction("AT_ASK", "CALL", False).order_side == "BUY"` |
| AT_ASK + CALL = strong_sentiment True | `classify_order_direction("AT_ASK", "CALL", False).strong_sentiment == True` |
| AT_ASK + PUT = BUY side, BEARISH sentiment | `classify_order_direction("AT_ASK", "PUT", False).sentiment == "BEARISH"` |
| AT_ASK + PUT = order_side BUY | `classify_order_direction("AT_ASK", "PUT", False).order_side == "BUY"` |
| AT_ASK + PUT = strong_sentiment True | `classify_order_direction("AT_ASK", "PUT", False).strong_sentiment == True` |
| BUY + CALL maps to REPEAT_BUY | `order_side_to_direction("BUY", "CALL") == "REPEAT_BUY"` |
| BUY + PUT maps to REPEAT_SELL | `order_side_to_direction("BUY", "PUT") == "REPEAT_SELL"` |

---

## Data Quality Flags in `flow_events`

| Column | Type | Meaning | Usage |
|---|---|---|---|
| `is_aggressive` | bool | Fill at or above ask (real NBBO) | Exclude from aggression analytics when `is_synthetic_quote=True` |
| `is_sell_aggressive` | bool | Fill at or below bid (real NBBO) | Symmetric to is_aggressive for sell-side initiated flow |
| `is_golden_sweep` | bool | Multi-exchange sweep >= 500K | Always reliable |
| `is_golden_block` | bool | Single-venue block >= 1M | Always reliable |
| `is_synthetic_quote` | bool | bid=ask=0, NBBO synthesized from fill | Exclude from aggression and net-premium calculations |
| `order_side` | text | BUY / SELL / UNKNOWN | Derived from bid_ask_class + contract_type |
| `strong_sentiment` | bool | True when direction is unambiguous | Use to filter weak-direction analytics |

---

## Story-to-Layer Map

| Story | Layer(s) affected |
|---|---|
| S0 — Swarm cleanup | Apex L3 composite (removes async path from `composite_signal_engine.py`) |
| S1 — Threshold reconciliation + emit flush | Apex L2 accumulator (`repetition_accumulator.py`) |
| S2 — Parser + detector fixes | Layer 2 parser (`options_flow_parser.py`, `order_side_classifier.py`, `bid_ask_classifier.py`, `trade_type_detector.py`), Layer 0 registry (`symbol_registry.py`), Apex L2 accumulator (`dominant_direction` property) |
| S2.5 — DB migration | Apex L5 persistence schema (`flow_events` table) |
| S3 — Signal gate | Apex L1 (`signal_gate.py` — new file) |
| S4 — Dual-window accumulator | Apex L2 (`repetition_accumulator.py` refactor) |
| S5 — Ladder detection | Apex L4 (`ladder_detector.py` — new file) |
| S6 — Composite overhaul + hot path | Apex L3 (`composite_signal_engine.py`), Apex L5 hot path (`tradier_stream.py`) |
| S7 — Tiered swarm | Apex L6 (`apex_swarm.py` — new file, blocked) |
| S8 — Real backtest | Apex L3 (`composite_signal_engine.py` — backtest weight reactivation) |

---

## Architect Review Notes
*Applied April 30, 2026 — issues resolved in revision 1:*

- **Issue 1 (spread gate):** Corrected diagram from invented tiered 15%/25% thresholds to the spec-authoritative 50% uniform gate. Added inline rationale explaining why a permissive threshold is intentional.
- **Issue 2 (dual numbering):** Added explicit `INGESTION SUBSYSTEM` and `APEX SIGNAL SUBSYSTEM` section dividers in the diagram to make the layer numbering reset visually unambiguous.
- **Issue 3 (persistence path):** Redrawn persistence path as a concurrent long-lived fan-out consumer, not a terminal dead-end branch. Added explicit note that persistence runs independently of signal path outcome.
- **Issue 9 (story map blast radius):** Expanded S2 story map row to list all six affected files explicitly. Other rows also expanded for precision.

*Applied April 30, 2026 — issues resolved in revision 2:*

- **Issue 5 (sector_score ceiling):** Added `SCORE CEILING` block to Apex L3 diagram node. Documents that weights sum to 0.90 pre-S5, decision not to redistribute the 0.10, and that `composite_score_ceiling: 0.90` is added to composite bus payload. Field removed when S5 wires real ladder context. Updated Apex L5 diagram node and layer description accordingly.
- **Issue 6 (ATM band threshold):** Replaced vague "ATM eligible" language with the approved definition `abs(strike - underlying_price) / underlying_price <= 0.02`. Added OTM classification tiers (ATM 0–2%, Standard 2–12%, Deep OTM >12%) and the zero-underlying-price fallback. Updated both diagram and layer description.
- **Issue 7 (trade_count sweep bypass semantics):** Replaced `trade_count == 1` with `len(ep.events) == 1` throughout diagram. Added explicit clarifying comment distinguishing episode event count from `fill_count` within a single tick. Added negative bypass condition to diagram.
- **Issue 8 (BUY-side CI invariants):** Added BUY CALL and BUY PUT direction labels to the direction inference block in Layer 2 diagram. Expanded CI Gate Invariants table from 6 rows (SELL-side only) to 14 rows covering all four quadrants. Added update note documenting the reasoning.
