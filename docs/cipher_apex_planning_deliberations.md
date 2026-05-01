# Cipher Apex Planning Deliberations — Architect vs Principal Engineer

## Document Role
This document captures the full planning dialogue, proposals, objections, refinements, and resolutions between the Senior Software Architect and the Principal Backend Software Engineer across the Apex signal pipeline planning sessions. It is written as a structured deliberation record rather than a short summary, and is intended to preserve the reasoning that led to the final story and sprint plan.

It starts from the architect's original architectural verdict that triggered the entire planning session, and continues through every major pushback, correction, and resolution.

---

## Participants
- Senior Software Architect
- Principal Backend Software Engineer

## Goals of the Deliberation
- Reconcile the existing flow pipeline with the intended Apex architecture.
- Identify silent correctness bugs in parser, signal direction, accumulation, and composite scoring.
- Separate cleanup work from new feature work.
- Convert architecture concerns into implementable stories with explicit acceptance criteria and test coverage.
- Preserve important invariants, especially around options-flow interpretation.

---

## Session 1 — Architect's Original Architectural Verdict

This was the starting document. The architect reviewed the full Cipher backend and produced the following written verdict before any stories were assigned.

---

### Architectural Verdict on Cipher Apex

#### The Parser Problem — You Are Dropping Eligible Trades
This is the most urgent issue.

**open_interest is wrong on most ticks.**
The parser reads `raw.get("open_interest", 0)` from the Tradier timesale stream payload. Tradier's timesale WebSocket event does not reliably carry OI. It is a chain-level field, not a timesale field. The registry enrichment at the bottom of the parser overwrites `open_interest` with `meta.open_interest` from the chain cache, but only when `reg.is_ready() AND reg.lookup(symbol)` succeeds. Any tick that arrives before the registry is fully warm, or for a symbol not in the registry, gets `open_interest=0`. If you enforce the `Volume > OI` gate with this data, you will discard every tick with `open_interest=0`, which is a large percentage of your flow.

**underlying_price is often 0.**
The parser reads `raw.get("underlying_price", 0)`. Tradier timesale events do not always carry this field. The OTM band check in Apex L2 (strike must be 3–12% OTM from underlying_price) will silently pass or fail incorrectly whenever `underlying_price=0`. You need the registry's stock-level price enrichment path for this. It is not coming from the stream tick.

**is_aggressive is unreliable for synthetic quotes.**
The parser correctly flags `is_synthetic_quote=True` when `bid=ask=0`, forces `is_aggressive=False`, and applies a 60% conviction haircut. This is fine. But the aggression gate in Apex L1 would discard all synthetic quotes, which includes many legitimate institutional prints on thinly traded contracts where the market maker has not published a two-sided quote yet. You would be silently dropping real whale flow.

**fill_price=0 guard is insufficient.**
The parser returns None if `size=0`, but not explicitly if `fill=0`. The fill computation is:
```python
fill = float(raw.get("last") or raw.get("price") or ((bid+ask)/2 if (bid+ask)>0 else 0))
```
If `last=None`, `price=None`, `bid=0`, `ask=0`, fill resolves to `0.0` and the event passes through with `premium=0`. Gate 1 at the accumulator catches this with `min_premium=$10k`, but Apex L1's premium >= $5K gate would also catch it. Not a regression, but worth making explicit with a `if fill == 0: return None` guard.

---

#### The Real Whale and Institutional Flow Problem With Apex L1
Putting on the options flow hat here: the sweep-only gate is the most dangerous filter in the whole proposal.

In practice, whales and institutional desks do not exclusively sweep. Here is what you would be discarding:

**Block trades.** A single $2M block on NVDA 600C 30DTE is more signal than 3 sweeps of $50K each. An Apex L1 sweeps-only gate would discard it entirely.

**Iceberg accumulation.** Institutions deliberately split large orders into single-exchange fills to avoid detection. These register as singles or splits, not sweeps. The whole point of whale accumulation is that it does not look like a sweep.

**Pre-market and low-liquidity hours.** Sweep detection requires three or more exchanges. Pre-market, many contracts only route through one or two exchanges. A $500K opening print at 9:31 AM on a single exchange is not a sweep, but it is often the most important tick of the day: first-mover institutional positioning before the retail crowd sees it.

**LEAPS and long-dated positions.** Big money hedging portfolio risk often uses far-dated options in single large fills. These never sweep because they are not urgent. The whole point is quiet accumulation.

**Recommendation:** Do not use sweep-only as a hard discard gate. Instead, tier the gate by trade type:
- SWEEP: passes with lower premium threshold ($5K)
- BLOCK (single exchange, high premium): passes with higher threshold ($50K minimum premium)
- SPLIT: passes with medium threshold ($25K)
- SINGLE (small): discard below $10K

This captures institutional block flow that sweep-only would miss, while still filtering retail noise.

---

#### The Volume > OI Gate
Beyond the data quality problem, this gate has a fundamental market microstructure flaw.

New contract openings are the highest-signal events. When OI is 0 or very low, it means this is fresh positioning: no existing open interest, someone is making a new bet from scratch. That is exactly what you want to capture. A Volume > OI ratio of 10:1, where volume massively exceeds OI, is one of the strongest unusual activity signals in the entire options flow discipline. It means new money is flowing in aggressively. Discarding it inverts the signal.

**Correct implementation:** Volume > OI should be a positive scoring boost, not a hard rejection gate. Add it as a point multiplier in Apex L3, not a L1 discard.

---

#### The Spread Gate Is Sound, With One Caveat
`(ask - bid) / mid > 0.15` is reasonable for liquid names, but for T3 symbols (small/mid-cap, thin markets), spreads of 20–40% are normal. A 15% max spread gate would eliminate virtually all T3 flow.

**Recommendation:** Apply spread gate only to T1 symbols, or raise the threshold to 25% for T2/T3.

---

#### Apex L2 Window Change: 30 min to 10 min
This is directionally correct for sweeps. Real institutional sweep campaigns on the same exact strike and expiry typically complete within minutes, not hours. The 30-minute window in the current accumulator was designed for persistence breadth, not signal precision. For Apex (signal-only path), 10 minutes is right.

**However:** The 30-minute window on the persistence path (flow_events) should stay. You want flow_events to capture all qualifying ticks across a wider window for historical analysis. The 10-minute window only applies to the signal emission logic, not the DB write gate.

---

#### Apex L4 Context Validator — ATR Check Is Underpowered
Using ATR-band checks against stored high/low is a trailing indicator that tells you where price has been, not where institutional activity is pointing.

The more useful contextual check you can build with what you already have is: cross-contract confirmation on the same ticker. If NVDA 600C sweeps AND NVDA 590C sweeps within 15 minutes, that is a much stronger signal than either alone. The buyer is deliberately buying across multiple strikes (ladder accumulation). This is buildable with existing accumulator infrastructure by scanning for concurrent episodes on the same ticker. No external data feed needed.

---

#### Tiered Swarm — This Is Correct
The current always-on 12-agent swarm is architecturally wasteful. The tiered 3/6/12 approach is the right call.

One addition: add a circuit breaker. If the Groq API latency exceeds 2s on the fast 3-agent path, fall through to deterministic scoring only. You cannot have Groq latency blocking the hot path on a streaming system doing 2,500 events/minute.

---

#### Architect's Recommended Change Priority

| Priority | Change | Why |
|---|---|---|
| 1 | Fix underlying_price enrichment in parser — pull from registry raw_quotes | OTM band check is blind without it |
| 2 | Fix open_interest reliability — only enforce V>OI as a score boost, not a gate | You are inverting the signal otherwise |
| 3 | Replace sweep-only gate with tiered trade type and premium thresholds | Blocks, icebergs, and pre-market flow are real signal |
| 4 | Implement Apex L1 hard gates (signal_gate.py) minus the two broken ones above | Self-contained, high ROI |
| 5 | Apex L2 accumulator: 10-min window for signal path only, 30-min for persist path | Precision without losing historical depth |
| 6 | Add fill=0 explicit guard to parser | Minor but eliminates a class of silent bad data |
| 7 | Spread gate: T1 ≤15%, T2/T3 ≤25% | Stops over-filtering thin-market names |

---

## Session 2 — Principal Engineer Response to the Architect's Verdict

The principal engineer reviewed the architect's verdict and agreed on the technical direction but added several specific pushbacks and introduced additional concerns not mentioned in the verdict.

### Response 1: S0 Must Be a Hard Prerequisite
**Principal engineer position:** Before any Apex work touches the signal or composite layer, the old async swarm infrastructure must be removed. The dead ensemble path was not just unused code. It created test-mock ambiguity, polluted mental models, and risked future developers accidentally wiring new Apex swarm logic into the wrong legacy layer.

**Resolution:** S0 became a mandatory blocking prerequisite for all other stories.

### Response 2: Alert Threshold Flush Needed to Accompany Any Threshold Change
**Principal engineer position:** Changing thresholds in `get_alert_level()` without flushing the debounce cache meant that in-flight episodes could produce false escalations or stuck de-escalations immediately post-deploy. The cache carried stale alert-level state that the new thresholds would contradict.

**Resolution:** S1 added explicit `_signal_last_emit.clear()` at stream startup.

### Response 3: The fill=0 Guard Must Be Explicit Even If Downstream Gates Catch It
**Principal engineer position:** Relying on downstream gates to reject zero-fill events was acceptable as a safety net, but explicit early returns made failure modes visible. If a future gate threshold changed, the fill=0 path would silently start producing bad records.

**Resolution:** `if fill == 0: return None` added as an explicit guard in the parser.

### Response 4: The OTM Band Change from 3–12% to 0–25% Deserves Its Own Story Bullet
**Principal engineer position:** Expanding OTM eligibility to ATM (0%) and wider (25%) changed the surface area of qualifying events significantly. This was not a minor parameter change. It needed its own explicit acceptance criterion and its own test scenarios to confirm that ATM flow was processed and far-OTM flow still had a premium multiplier acting as a quality gate.

**Resolution:** S4 explicitly called out ATM flow eligibility, deep-OTM multiplier, and test scenarios for each.

---

## Session 3 — Deeper Parser Semantics Debate

### Issue: Sentiment Was Naive Across the Entire Parser
**Principal engineer observation:** Even after all the data-quality fixes in the architect's verdict, the biggest logical error was elsewhere. The sentiment field was being assigned based only on contract type: `CALL => BULLISH`, `PUT => BEARISH`. This ignored execution intent entirely.

The engineer raised the following cases as misclassified under the naive model:
- A covered call writer selling CALLs at the bid is bearish or neutral, not bullish.
- A protective PUT buyer is hedging, and the aggressive act of buying downside protection is still bearish flow signal.
- A hedge fund selling $2M in SPY puts at the bid is expressing a floor view. That is bullish, never bearish.

**Architect's initial position:** The `is_aggressive` flag was a reasonable proxy for direction.

**Principal engineer pushback:** `is_aggressive` only covered buy-side aggression. `AT_BID` and `BELOW_BID` fills, where a seller was initiating against the bid, were given `is_aggressive=False` and low conviction scores. A whale blowing through the bid on $3M in NVDA calls was receiving the same conviction boost as a passive mid-print. That was economically wrong.

**Resolution:** Introduce a full direction classification system with:
- Buy-side aggression: `AT_ASK`, `ABOVE_ASK`
- Sell-side aggression: `AT_BID`, `BELOW_BID`
- Ambiguous: `MID`
- Uninformative: synthetic quotes

Direction inference should combine bid/ask class with contract type.

---

## Session 4 — Direction Classification Matrix

The architect and principal engineer worked through the full 10-case matrix:

| bid_ask_class | contract_type | order_side | sentiment | Rationale |
|---|---|---|---|---|
| ABOVE_ASK | CALL | BUY | BULLISH | Urgent buyer paying up for calls |
| AT_ASK | CALL | BUY | BULLISH | Initiating long call position |
| ABOVE_ASK | PUT | BUY | BEARISH | Urgent buyer paying up for puts |
| AT_ASK | PUT | BUY | BEARISH | Initiating downside protection or short bet |
| AT_BID | CALL | SELL | BEARISH | Writing or closing calls, short gamma |
| BELOW_BID | CALL | SELL | BEARISH | Desperate call seller, strong short signal |
| AT_BID | PUT | SELL | BULLISH | Selling puts at bid, floor view expressed |
| BELOW_BID | PUT | SELL | BULLISH | Aggressive put seller, conviction the floor holds |
| MID | CALL | UNKNOWN | BULLISH | Ambiguous, fallback to contract type |
| MID | PUT | UNKNOWN | BEARISH | Ambiguous, fallback to contract type |

### Extended Debate on SELL PUT = BULLISH

**Principal engineer emphasis:** The AT_BID + PUT = SELL + BULLISH case is the most easily misclassified case in the entire matrix. The engineer insisted on elevating it beyond a unit test.

The argument:
- Selling puts at the bid is the strategy known as the cash-secured put or the "Wheel" strategy.
- It is used constantly by institutions to generate premium while expressing willingness to own the underlying at the strike price.
- When a hedge fund sells $2M in SPY puts at the bid, it is stating: "SPY will not fall below this strike, or if it does, we are happy to be long at that level."
- This is unambiguously bullish or neutral. It is never bearish.
- Flagging it as BEARISH (because it is a PUT) misrepresents the entire trade and degrades signal quality.

**Architect acknowledgment:** The architect agreed and additionally pointed out the symmetric case. Selling calls is the most overlooked bearish signal. Someone blowing through the bid on call options is either closing in panic or has strong conviction the calls are overpriced. This is a high-conviction short signal that was previously being treated as bearish-weak.

**Resolution:** Both cases became CI gate invariants, enforced before any other test in the pipeline.

---

## Session 5 — Registry Enrichment Overwrite Bug Discovery

### Discovery
The principal engineer inspected the parser code and found a two-assignment bug on sentiment.

**First assignment** (correct location, wrong logic):
```python
if ctype == "CALL":
    ev.sentiment = "BULLISH"
elif ctype == "PUT":
    ev.sentiment = "BEARISH"
```

**Second assignment** (in the registry enrichment block, overwrites the first):
```python
ev.sentiment = "BULLISH" if meta.contract_type == "CALL" else "BEARISH"
```

The second assignment ran unconditionally after a successful registry lookup. Because the registry was warm for the majority of ticks in a live session, this meant even a correctly classified SELL PUT event would be relabeled BEARISH by the enrichment block.

**Architect position:** The registry block was added to ensure contract-type metadata came from the authoritative chain source rather than the stream event, which sometimes had wrong or missing `option_type` values.

**Principal engineer position:** The intent was correct but the implementation was wrong. The block should update contract metadata fields only. Direction and sentiment must be re-derived from the updated contract type using the real classification function, not overwritten with naive logic.

**Resolution:**
- Remove `ev.sentiment = ...` from the registry enrichment block entirely.
- After setting `ev.contract_type = meta.contract_type`, call `classify_order_direction()` again.
- The second call is authoritative because it has the registry-corrected contract type but still uses the original bid/ask class from the actual execution.

---

## Session 6 — Conviction Scoring Was Buy-Side Biased

**Principal engineer observation:** The conviction formula used `is_aggressive` to award a 0.40 floor:
```python
(0.4 if aggressive else 0.15)
```
`is_aggressive` returned True only for `AT_ASK` and `ABOVE_ASK`. Any sell-side initiation — including a massive AT_BID sweep — received 0.15. A whale selling $3M in puts at the bid would score identically to a passive retail mid-print.

**Architect response:** That was a clear asymmetry error.

**Resolution:**
- Add `is_sell_aggressive(trade_type: str) -> bool` for `AT_BID` and `BELOW_BID`.
- Compute `is_directionally_aggressive = is_aggressive or is_sell_aggressive`.
- Use `is_directionally_aggressive` in the conviction formula.
- Pass `is_directionally_aggressive` into golden-sweep classification.

---

## Session 7 — Golden Sweep Parameter Rename and Golden BLOCK Addition

**Principal engineer observation:** `is_golden_sweep()` accepted a parameter named `above_ask`. Once sell-side aggression was recognized, that name was misleading because a golden signal could now originate from sell-side flow too.

**Resolution:** Rename parameter to `is_directionally_aggressive`.

**Principal engineer second observation:** A $1.5M BLOCK execution had no golden classification path. The only golden path was sweep-based. That excluded one of the highest-signal trade types in institutional flow.

**Resolution:** Add golden BLOCK logic: `if trade_type == "BLOCK" and premium >= 1_000_000: return True`. Block golden classification does not require aggression because the premium itself is the signal.

---

## Session 8 — Module Boundary Debate: Where Should Direction Logic Live?

**Architect's suggestion:** Add direction classification directly to `bid_ask_classifier.py` since it reads from bid/ask output.

**Principal engineer pushback:** These were different abstraction levels.
- `bid_ask_classifier.py` answers: "Where in the spread did this fill occur?"
- `order_side_classifier.py` answers: "What directional intent do we infer from that placement combined with contract type?"

Mixing them would create a module with two distinct semantic responsibilities, harder independent testing, and unnecessary coupling.

**Resolution:** New file `parsers/order_side_classifier.py`. Imports from `bid_ask_classifier.py` for constants but is tested independently.

---

## Session 9 — Hot Path Review of `tradier_stream.py`

### Discovery: Direction Was Still Hardcoded After All Parser Fixes
The architect and principal engineer reviewed `tradier_stream.py` and found the following code block in `_process_trade()`:

```python
if sig_ep.contract_type == "CALL":
    direction = "REPEAT_BUY"
elif sig_ep.contract_type == "PUT":
    direction = "REPEAT_SELL"
else:
    direction = "REPEAT_BUY" if ev.sentiment == "BULLISH" else "REPEAT_SELL"
```

This meant all parser direction improvements would be silently thrown away at publish time. A SELL PUT episode would be correctly classified in the parser and then reclassified as REPEAT_SELL here before:
- Being persisted to `flow_episodes`.
- Being published as a bus signal.
- Being included in the composite message.
- Being displayed on the frontend.

The architect identified that this bug appeared in two locations in the same function (used for both `persist_flow_episode()` and bus publish).

**Principal engineer addition:** Demo mode at the bottom of the file had the same naive logic:
```python
direction = "REPEAT_BUY" if ctype == "CALL" else "REPEAT_SELL"
```
Demo mode was not production flow, but it encoded the wrong model for every developer and tester who saw it.

**Resolution:**
- Replace hot-path direction derivation with `sig_ep.dominant_direction`.
- Fix demo mode to randomize order side and map through the real direction function.

---

## Session 10 — Episode Dominant Direction Debate

**Architect's initial suggestion:** Use `ev.order_side` from the latest event to compute direction in the hot path.

**Principal engineer pushback:** Latest-event shortcuts introduced a subtle failure mode. If the last event in a SELL PUT campaign happened to be a mid-print, `ev.order_side` would be `UNKNOWN` and direction would fall back to contract type, producing REPEAT_SELL again.

The correct abstraction was to compute direction from the premium-weighted history of the entire episode. An episode where $1.8M of premium came from AT_BID PUT fills should resolve as REPEAT_BUY even if the last tick was a $5K mid-print.

**Resolution:** Add `dominant_direction` property to `RepetitionEpisode`:
```python
@property
def dominant_direction(self) -> str:
    buy_prem = sell_prem = 0.0
    for e in self.events:
        d = order_side_to_direction(
            getattr(e, "order_side", "UNKNOWN"),
            getattr(e, "contract_type", "CALL"),
        )
        if d == "REPEAT_BUY":
            buy_prem += getattr(e, "premium", 0.0)
        else:
            sell_prem += getattr(e, "premium", 0.0)
    return "REPEAT_BUY" if buy_prem >= sell_prem else "REPEAT_SELL"
```

---

## Session 11 — Episode Influence Tier Was Using Last-Tick Premium

**Principal engineer observation:** The composite bus message published:
```python
"influence_tier": ev.influence_tier,
```
This was the latest tick's tier, not the episode tier. A $2.5M episode whose last print was a small $40K fill would publish as LARGE or RETAIL on the bus, even though the episode was WHALE-level.

**Resolution:** Add `episode_influence_tier(ep)` helper function:
```python
def episode_influence_tier(ep):
    prem = ep.total_premium
    if prem >= 2_000_000: return "WHALE"
    if prem >= 500_000:   return "INSTITUTIONAL"
    if prem >= 100_000:   return "LARGE"
    return "RETAIL"
```
Use this in both the `composite_msg` episode block and the `signal_gate.py` tier logic.

---

## Session 12 — Backtest Score Was Semantically Fraudulent

**Principal engineer position:** The current backtest score used a seeded pseudorandom function. It was not a backtest. It produced numbers that looked statistically valid but carried no predictive meaning. In a production system, this was actively harmful because it could move composite scores up or down based on meaningless noise wearing the clothes of historical signal.

**Architect response:** The weight was low enough to be mostly harmless.

**Principal engineer pushback:** Low weight was not the right standard. Zero influence was the right standard. Any non-zero contribution from a fake number degraded the integrity of every composite recommendation the platform produced.

**Resolution:** S6 forced `backtest_score = 0.0` in the composite formula. The field stays in the output schema for continuity. Weight is zero until S8 delivers a real implementation.

---

## Session 13 — Story S2.5 Added as Explicit DB Migration

**Principal engineer observation:** Adding `order_side` and `strong_sentiment` to persistence payloads without a deployed schema migration would cause live write failures. This could not be hidden inside S2 as an implicit dependency.

**Resolution:** S2.5 created as a standalone story representing an additive Supabase migration:
```sql
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS order_side TEXT
    CHECK (order_side IN ('BUY', 'SELL', 'UNKNOWN')) DEFAULT 'UNKNOWN',
  ADD COLUMN IF NOT EXISTS strong_sentiment BOOLEAN NOT NULL DEFAULT FALSE;
```
S2.5 must deploy before S6 hot-path changes go live.

---

## Session 14 — Swarm Safety Gate

**Principal engineer position:** S7 could not be scoped until the runtime model of the stream worker was confirmed. If `_process_trade()` awaited composite calls sequentially, any async model call to Groq would stall event ingestion on a live stream.

**Architect position:** The tiered swarm proposal was architecturally sound with a hard 2s timeout.

**Principal engineer position:** The timeout was not sufficient protection if the call model was sequential. A 2-second stall at 2,500 events per minute was unacceptable. The circuit breaker was correct, but the async safety of the call site needed confirmation before any swarm code was written.

**Resolution:** S7 was blocked pending `stream_worker.py` review. Story was retained in the plan as conditional.

---

## Session 15 — Coverage Policy Became Architecture, Not a Norm

**Principal engineer position:** Coverage requirements scattered across individual stories would be honored inconsistently under schedule pressure. They needed to be stated once as cross-cutting rules that applied to every story by default.

**Resolution:** The following rules became architecture-level policy:
- New files: 100% line and branch coverage.
- Modified files: every new or changed branch covered.
- Regression suite: all green before merge.
- Direction invariants: dedicated CI gate tests, run first.
- Import fallbacks: use `try/except` with `pragma: no cover` only where justified.

---

## Session 16 — Issue 5: sector_score 0.10 Weight Gap
*April 30, 2026 — post spec-review deliberation*

### Issue
With `sector_score = 0.0` and `backtest_score = 0.0`, the composite weight formula active weights sum to only 0.90. Every composite_score emitted before S5 lands is silently capped at 0.90.

**Architect:** The weights as written sum to 0.90 when sector_score and backtest_score are both zero. Every score emitted before S5 lands is silently capped. Frontend consumers calibrating thresholds won't know their "0.90 conviction" is actually a ceiling hit — they'll tune their alerting logic against a ceiling they can't see. That's a product quality bug even if the math is technically correct.

**Principal Engineer:** I don't want to redistribute the 0.10 to the other weights. That shifts the entire scoring baseline. We've already tuned flow/volume/premium splits against real data from the stream. Changing them now means re-validating every threshold across every tier — that's weeks of recalibration on a live system.

**Architect:** Agreed on not redistributing. The weight structure is right. But the ceiling must be explicit — it cannot be silent. If the frontend is normalizing scores to a 0–1 scale and treating 0.90 as "high conviction," they need to know that 0.90 is actually the maximum achievable score in the pre-S5 period. Put it in the payload.

**Principal Engineer:** That works. A `composite_score_ceiling` field in the bus payload is cheap to add and gives downstream consumers exactly what they need to normalize without changing any core logic.

**Resolution:** Weights stay unchanged. A `composite_score_ceiling: 0.90` field is added to the composite bus payload starting in S6. Frontend treats any score > 0.85 as effectively maximum conviction pre-S5. The field is removed when S5 wires real ladder context into sector_score and sector_score receives a non-zero value.

---

## Session 17 — Issue 6: ATM Band Has No Concrete Threshold
*April 30, 2026 — post spec-review deliberation*

### Issue
The spec used the phrase "ATM eligible" with no numeric definition. Without a concrete threshold, different engineers would implement the boundary differently.

**Architect:** "ATM eligible" with no number will be implemented three different ways. I've seen this before on production systems — one engineer does ±$1 from the strike, another does ±1% of underlying, another does exact strike match only. Each produces a meaningfully different set of qualifying contracts. On a live signal system, that inconsistency goes undetected until a signal regression surfaces weeks later. This has to be a number in the spec.

**Principal Engineer:** ±1% is too tight on high-priced underlyings. NVDA at $900 — a $9 spread is 1%. Institutions buy ATM NVDA all the time and deliberately land slightly off-center on the strike. A 1% band would exclude a large portion of real ATM institutional prints on those names. I'd say ±2%.

**Architect:** ±2% makes sense. It needs to be a percentage of underlying price, not an absolute dollar amount. Absolute amounts break across different underlying price regimes. A ±$5 threshold works on a $50 stock but is effectively zero on a $900 stock. Percentage is the only portable definition.

**Principal Engineer:** Agreed. And we need to handle the zero-underlying-price case. If `underlying_price == 0`, we can't compute the ATM band at all. Those events should fall back to standard premium floor and skip OTM classification entirely — don't try to classify them as ATM or deep OTM.

**Resolution:** ATM is defined as `abs(strike - underlying_price) / underlying_price <= 0.02`. This is now a required acceptance criterion in S4. Events with `underlying_price == 0` fall back to standard floor — no OTM classification attempted.

---

## Session 18 — Issue 7: trade_count == 1 Sweep Bypass Ambiguity
*April 30, 2026 — post spec-review deliberation*

### Issue
The spec specified a sweep bypass condition using `ep.trade_count == 1`, but `trade_count` was not defined anywhere in the spec. It was ambiguous between two entirely different semantics.

**Architect:** The spec says `ep.trade_count == 1` but nowhere defines what `trade_count` measures. Reading the accumulator code, I can see at least two plausible interpretations: episode event count (how many OptionsFlowEvent objects have been added to this episode) or fill count within a single stream tick (the `fill_count` field on an individual event). Those are completely different things. A single-tick event with 100 fills would fail the condition under one interpretation and pass under the other.

**Principal Engineer:** It's episode event count — `len(ep.events)`. One event entered the accumulator for this (ticker, strike, expiry) key. The `fill_count` field lives on the individual `OptionsFlowEvent` and refers to how many exchange fills were reported within that single stream tick. They're different layers entirely. `fill_count` is about execution mechanics within one tick. `len(ep.events)` is about how many ticks have accumulated in the episode window.

**Architect:** That distinction has to be in the spec explicitly. Any future engineer implementing the bypass condition in isolation — without reading the accumulator internals — will make the wrong assumption. The spec should use `len(ep.events) == 1` and include a comment explaining what it is not.

**Principal Engineer:** Agreed. We should also add the negative test case explicitly: `len(ep.events) == 2` with the same SWEEP type and same premium must NOT trigger the bypass. That protects against an engineer accidentally broadening the condition.

**Resolution:** Spec and sprint plan now use `len(ep.events) == 1` instead of `ep.trade_count == 1`, with an explicit comment distinguishing from `fill_count`. The bypass negative test case is added to S4 acceptance criteria: `len(ep.events) == 2` with same SWEEP and premium must NOT bypass min_sweeps.

---

## Session 19 — Issue 8: CI Invariants Missing BUY Side
*April 30, 2026 — post spec-review deliberation*

### Issue
The CI gate test list covered only SELL PUT and SELL CALL invariants. The BUY CALL and BUY PUT quadrants were unguarded.

**Architect:** The CI gate currently only covers SELL PUT and SELL CALL. If a parser refactor breaks BUY PUT direction — makes it bullish instead of bearish — nothing in the CI gate catches it. The change passes all tests. The bug ships. That's a gap in the invariant set.

**Principal Engineer:** That's a real gap. BUY PUT = BEARISH is just as fundamental as SELL PUT = BULLISH. We added the sell-side invariants because SELL PUT = BULLISH is the counter-intuitive case — the one that violates naive contract-type logic. But we left the buy side unguarded by assuming it was "obvious." Obvious invariants are the ones that regress silently. Nobody writes a test for something they think is obviously true.

**Architect:** Exactly. And the regression scenario is realistic, not theoretical. If someone refactors `classify_order_direction()` and gets the BUY-side logic wrong in a way that accidentally unifies CALL and PUT to both be BULLISH when bought, every BEARISH BUY PUT signal in the pipeline becomes a false bullish signal. That's a directional inversion on a real trade type that we'd catch in production anomaly reports, not in CI.

**Principal Engineer:** Eight new assertions: BUY CALL and BUY PUT for sentiment, order_side, and strong_sentiment, plus the two REPEAT direction mappings for BUY + CALL and BUY + PUT. That covers the full four-quadrant matrix with all properties gated.

**Resolution:** Eight new test assertions added across spec and sprint plan covering all four quadrants: BUY CALL = BULLISH, BUY PUT = BEARISH (with `order_side=BUY`, `strong_sentiment=True` for each), and the `REPEAT_BUY`/`REPEAT_SELL` mappings for both BUY + CALL and BUY + PUT. `test_direction_invariants.py` now enforces 14 total assertions (6 original SELL-side + 8 new BUY-side).

---

## Session 20 — Issue 4: Apex L4 / L3 Numbering vs Execution Order Ambiguity
*April 30, 2026 — post spec-review deliberation*

### Issue
The architecture diagram numbers the Ladder Detector as "Apex L4" and the Composite Scorer
as "Apex L3." In most layered systems, higher numbers run later. Here L4 runs **before** L3,
which is the opposite of what a reader expects. The diagram carried a parenthetical
clarification (`[runs before composite — feeds sector_score]`), but this created a latent
confusion risk for every engineer picking up the code cold.

**Architect:** The numbering is defensible — it reflects the original design order, where
the composite was designed first (L3) and the ladder was inserted later as a feeder (L4).
But "L4 feeds L3" violates the mental model of every engineer who reads layered architecture
diagrams, where higher-numbered layers receive input from lower-numbered ones. I've reviewed
the hot-path description and the story-to-layer map — both describe the correct execution
order (accumulator → ladder → composite), but the numbering contradicts it. Anyone wiring
up the hot path from the diagram alone will get the order wrong.

**Principal Engineer:** I'd prefer not to renumber mid-planning. We have a spec, a story
plan, and a deliberations doc that all use L3/L4 as they are. Renaming now creates a churn
cost across four documents for a naming problem that the parenthetical already calls out.
That said, I agree the parenthetical is easy to miss. The real fix is to make the execution
order explicit in the hot path description — a numbered sequence that can't be misread —
rather than changing the layer numbers.

**Architect:** Fair. Renumbering four documents for a cosmetic issue is the wrong trade-off.
But the fix has to do more than add a parenthetical. Three things need to be explicit and
consistent: (1) the hot path execution sequence must be stated as a numbered list, not just
implied by arrow flow in the diagram; (2) the Layer Descriptions section must open with an
explicit "Execution Order Note" callout before the first layer description; (3) the
Story-to-Layer Map must add a column for execution order position so it's machine-readable
per story.

**Principal Engineer:** Agreed on all three. On the spec side — S5 (the ladder story) is
correctly scoped. The spec already says "passes output as context into Apex L3 composite
scorer." The architecture doc is the one carrying the ambiguity. The stories file is clean.
The deliberations doc should record this resolution but the spec and sprint plan don't need
new sections for this issue.

**Resolution:**
- Apex L4/L3 numbering is preserved as-is. Renumbering was rejected as high-churn
  cosmetic work with no functional payoff.
- Architecture doc updated with three additions:
  1. Hot-path bullet in the Layer 1 (Stream Ingestion) section replaced with a
     numbered 7-step execution sequence that makes L4-before-L3 explicit.
  2. New "Execution Order Note" callout box added immediately before the Layer
     Descriptions section, stating the full Apex runtime sequence:
     `L1 → L2 → L4 → L3 → L5` and the reason numbering diverges from execution order.
  3. Story-to-Layer Map gets a new "Hot-path exec position" column showing the
     runtime sequence position of each story's affected layer.
- Spec (`cipher_apex_engineering_spec.md`): no changes. S5 already correctly
  describes the feeder relationship.
- Sprint plan (`cipher_apex_story_and_sprint_plan.md`): no changes. Story
  dependencies already enforce the correct implementation order.
  
---
## Session 21 — Issue 9: REPEAT_BUY Collapses BUY CALL and SELL PUT — Loss of Execution Mechanic Dimension
*May 1, 2026 — panel deliberation: Senior Software Architect + Principal Backend Software Engineer + Lead QA Engineer*

### Issue Raised
The product owner raised the following concern:

> The current `order_side_to_direction()` mapping collapses two structurally distinct
> trade mechanics into a single feature: REPEAT_BUY.
>
> - BUY + CALL = REPEAT_BUY: aggressive directional bet; urgency and conviction; net long delta via premium outlay.
> - SELL + PUT = REPEAT_BUY: passive income generation, volatility trade (short vega), or floor expression; net long delta via premium collection.
>
> Both are delta-positive, but their mechanics, Greeks profile, and market interpretation
> are entirely different. Collapsing them makes the system unable to distinguish between
> a highly aggressive directional bet and passive positioning. This is a future capability
> gap even if we do not use the dimension today.

---

### Architect Opening Position

**Architect:** The concern is structurally correct. The `REPEAT_BUY` label was always an
abstraction over delta direction — it captures what the market is expressing, not how it's
being expressed. The pipeline was designed from first principles around the output question:
"Is this flow bullish or bearish?" — not the execution question: "How is the participant
expressing that view?"

That said, I do not think the resolution is to add a third direction value like
`REPEAT_BUY_PASSIVE` or to split the `REPEAT_BUY` path. That would break every downstream
consumer of the direction field simultaneously and requires DB migration, frontend logic
changes, and signal threshold recalibration — all for metadata that, by the product owner's
own admission, we don't intend to act on yet.

The correct resolution is to add a **metadata field** that preserves the execution mechanic
dimension alongside the direction output, without altering direction semantics or any
downstream consumer. The direction field stays as-is. The new field is an additive signal
enrichment.

**My proposal: add `execution_mechanic` as a new string field on `OptionsFlowEvent` and
in the persistence payload.** The field captures how the delta-directional exposure was
achieved, independently of whether the direction is REPEAT_BUY or REPEAT_SELL.

---

### Principal Engineer Response

**Principal Engineer:** I agree with the architect's framing — don't touch direction
semantics. Any change to `REPEAT_BUY`/`REPEAT_SELL` would cascade into every downstream
consumer: frontend, WebSocket bus payload, `flow_episodes` persistence schema, and the
composite scorer's `recommendation` field. That's a large blast radius for an enrichment.

The mechanic metadata approach is clean because it decouples the two concerns:
- "What direction?" → `dominant_direction` (existing, unchanged)
- "How was that direction achieved?" → `execution_mechanic` (new, additive)

On the implementation: the mechanic classification belongs in `order_side_classifier.py`
alongside the existing `classify_order_direction()` function, since it reads from the same
inputs: `order_side` and `contract_type`. It should be added to the `OrderDirection`
NamedTuple as a fourth field.

**Proposed mechanic taxonomy:**

| order_side | contract_type | execution_mechanic        | rationale                                      |
|------------|---------------|---------------------------|------------------------------------------------|
| BUY        | CALL          | DIRECTIONAL_LONG          | Long call, net premium outlay, urgency signal  |
| BUY        | PUT           | DIRECTIONAL_SHORT         | Long put hedge or short bet, premium outlay    |
| SELL       | PUT           | PASSIVE_BULLISH           | Put selling, short vega, income/floor view     |
| SELL       | CALL          | PASSIVE_BEARISH           | Call selling, short gamma, income or hedge     |
| UNKNOWN    | CALL          | AMBIGUOUS_LONG            | Mid-print call — ambiguous intent              |
| UNKNOWN    | PUT           | AMBIGUOUS_SHORT           | Mid-print put — ambiguous intent               |

This six-value taxonomy is complete and non-overlapping. Every (order_side, contract_type)
combination maps to exactly one mechanic. No ambiguity in implementation.

---

### Lead QA Engineer Position

**QA Lead:** From a testability standpoint, the mechanic field is a significant improvement
over the current model because it gives tests a way to assert *how* a signal was generated,
not just what direction it resolved to. Two tests that currently produce identical
`REPEAT_BUY` outputs are now distinguishable:

```
BUY CALL → REPEAT_BUY + DIRECTIONAL_LONG   ← aggressive long
SELL PUT → REPEAT_BUY + PASSIVE_BULLISH    ← passive floor view
```

This matters for regression tests on the Apex composite layer. Right now, if a developer
inadvertently transposes the conviction formula weights for buy-side vs sell-side, the
test can only catch it via score magnitude. With `execution_mechanic`, a test can assert
the mechanic directly and immediately surface any misclassification without needing to
calibrate score thresholds.

I propose adding 6 mechanic invariants to the CI gate alongside the 14 existing direction
invariants. This brings the total direction CI gate to 20 assertions — still a single
test file, still O(1) to run.

**Additional QA concern:** The `execution_mechanic` field must be included in the
`flow_events` persistence payload and the composite bus payload from day one, not added
as a future enrichment. If we ship without it, we'll have a period of historical data
in `flow_events` with no mechanic column, which degrades the value of S8 (real backtest
scoring) because you can't retrospectively stratify by execution mechanic.

---

### Architect Response to QA

**Architect:** Agreed on both points. The mechanic must persist from the moment the field
exists, not optionally. The QA engineer is right that retroactive enrichment is not
realistic for a high-volume stream table — you'd be running an UPDATE on millions of rows
with no good join key.

On the DB side, this means S2.5 gets a third new column:

```sql
ALTER TABLE flow_events
  ADD COLUMN IF NOT EXISTS execution_mechanic TEXT
    CHECK (execution_mechanic IN (
      'DIRECTIONAL_LONG', 'DIRECTIONAL_SHORT',
      'PASSIVE_BULLISH',  'PASSIVE_BEARISH',
      'AMBIGUOUS_LONG',   'AMBIGUOUS_SHORT'
    ))
    DEFAULT 'AMBIGUOUS_LONG';
```

On the CI gate: I support the 6 new mechanic invariants. However, I want to be precise
about what they assert. The mechanic invariants should not duplicate direction assertions —
they should exclusively assert the mechanic given the same inputs. No cross-linking.

---

### Resolution

**Agreed by all three:**

1. **`execution_mechanic` field added to `OrderDirection` NamedTuple** as a fourth field.
   The existing three fields (`order_side`, `sentiment`, `strong_sentiment`) are unchanged.

2. **`classify_order_direction()` returns the mechanic** alongside existing output.
   Implementation is a lookup against the six-entry taxonomy table above. No new function —
   same function, same inputs, one additional output field.

3. **`OptionsFlowEvent` gains `execution_mechanic: str = "AMBIGUOUS_LONG"`** as a new
   field alongside `order_side` and `strong_sentiment`. Default is `AMBIGUOUS_LONG`
   to safely handle any path that doesn't flow through the classifier.

4. **S2.5 migration adds `execution_mechanic` column** to `flow_events` with a `CHECK`
   constraint on the six valid values and `DEFAULT 'AMBIGUOUS_LONG'`.

5. **Composite bus payload includes `execution_mechanic`** in the `signal` block.
   This is a non-breaking addition — existing consumers can ignore the field.

6. **6 new CI gate invariants** added to `test_direction_invariants.py`:

   ```
   (AT_ASK,    CALL) → DIRECTIONAL_LONG
   (AT_ASK,    PUT)  → DIRECTIONAL_SHORT
   (AT_BID,    PUT)  → PASSIVE_BULLISH
   (AT_BID,    CALL) → PASSIVE_BEARISH
   (MID,       CALL) → AMBIGUOUS_LONG
   (MID,       PUT)  → AMBIGUOUS_SHORT
   ```

7. **No direction field changes.** `REPEAT_BUY` and `REPEAT_SELL` semantics are untouched.
   `dominant_direction` on `RepetitionEpisode` is untouched. No downstream consumers need
   updates for correctness — mechanic is purely additive.

8. **Story impact: S2 gains one sub-task.** No new story needed. The implementation is
   a single additive change to `order_side_classifier.py` and `options_flow_parser.py`,
   a column in S2.5, and a field addition to the bus payload in S6.

---

### What Does NOT Change (explicit agreement)

- `order_side_to_direction()` mapping is unchanged. SELL + PUT = REPEAT_BUY stands.
- The 14 existing direction CI invariants are unchanged.
- `dominant_direction` on `RepetitionEpisode` remains premium-weighted direction — it does
  NOT aggregate execution mechanics. Mechanic is a per-event field only.
- No new story sprint slot required. S2 sub-task, S2.5 column, S6 bus field.
---

## Final Summary: What the Deliberations Changed

The architect's original verdict correctly identified the most urgent data quality and signal logic problems. The principal engineer added depth and found additional issues that would have caused silent correctness failures even after the architect's fixes were applied. Sessions 16–19 resolved four remaining specification gaps identified during the spec and sprint plan review.

| Area | Architect Verdict | Principal Engineer Addition |
|---|---|---|
| Underlying price enrichment | Fix via registry | Confirmed, also needed for direction re-derive after enrichment |
| open_interest gate | Move to score boost | Agreed |
| Sweep-only gate | Replace with tiered trade types | Agreed, also added explicit golden BLOCK threshold |
| Spread gate | T1/T3 tiered threshold | Agreed |
| Window split | 10-min signal, 30-min persist | Agreed |
| ATR context check | Replace with ladder detection | Agreed, scoped as S5 |
| Swarm circuit breaker | Hard timeout | Agreed, added async safety gate |
| Sentiment classification | Use is_aggressive as proxy | Elevated to full direction matrix with invariants |
| Registry enrichment | Update contract fields | Discovered sentiment overwrite bug, required re-derive |
| Conviction scoring | Use is_aggressive for score | Fixed sell-side bias, introduced is_sell_aggressive |
| Hot-path direction | Not reviewed initially | Found naive contract-type hardcoding, fixed to dominant_direction |
| Episode influence tier | Not reviewed initially | Found last-tick shortcut, fixed to episode total |
| Backtest score | Reduce weight | Pushed to zero weight until real implementation |
| DB migration | Implicit | Made explicit as S2.5 |
| Test coverage | Story-level mentions | Elevated to architecture-level policy |
| Demo mode | Not reviewed | Fixed naive direction encoding |
| sector_score weight gap (Issue 5) | Not in original verdict | composite_score_ceiling added to bus payload; weights unchanged |
| ATM band threshold (Issue 6) | Not in original verdict | ATM defined as ±2% of underlying price; zero-price fallback |
| trade_count ambiguity (Issue 7) | Not in original verdict | len(ep.events) == 1 with explicit comment; negative test added |
| BUY-side CI invariants (Issue 8) | Not in original verdict | 8 new assertions; all four quadrants now gated |

---

## Non-Negotiable Invariants — Final Agreement

These invariants were agreed upon by both the architect and principal engineer as CI gate conditions.

**SELL-side (original):**
1. AT_BID + PUT must resolve to order_side=SELL, sentiment=BULLISH, strong_sentiment=True.
2. BELOW_BID + PUT must resolve to sentiment=BULLISH.
3. SELL + PUT must map to direction REPEAT_BUY.
4. SELL + CALL must map to direction REPEAT_SELL.

**BUY-side (added — Issue 8):**
5. AT_ASK + CALL must resolve to order_side=BUY, sentiment=BULLISH, strong_sentiment=True.
6. AT_ASK + PUT must resolve to order_side=BUY, sentiment=BEARISH, strong_sentiment=True.
7. BUY + CALL must map to direction REPEAT_BUY.
8. BUY + PUT must map to direction REPEAT_SELL.

**General:**
9. Registry enrichment must never overwrite sentiment with naive contract-type logic.
10. Production composite scoring must not include a non-zero backtest weight until S8 is implemented.
11. Episode influence tier must use total episode premium, not latest tick premium.
12. dominant_direction must be premium-weighted, not first-event or latest-event.
13. composite_score_ceiling must be present in bus payloads while sector_score is inactive (pre-S5 wiring).
14. composite_score_ceiling must be removed from bus payloads once S5 wires real ladder context.
