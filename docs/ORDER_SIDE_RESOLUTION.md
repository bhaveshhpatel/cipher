# Order Side Resolution — Architecture Decision Record
**Date:** 2026-05-03
**Status:** Accepted
**Sprint:** WSJ Ingestion Alignment (ING-001)
**Owner:** Dhruv Patel

---

## Decision

Cipher uses **fill placement relative to the bid/ask spread** as the sole proxy for order-side aggression. True exchange-reported `order_side` (aggressor flag) is not used and is not sourced from any external feed.

---

## Background

During sprint planning for WSJ Ingestion Alignment, ING-001 was opened to verify whether Tradier's timesale WebSocket stream exposes an `order_side`, `side`, or `aggressor_side` field per tick. This would have enabled distinguishing:
- BUY CALL at ask (bullish opener)
- SELL PUT at bid (conviction bullish — put writer)
- BUY PUT at ask (bearish opener)
- SELL CALL at bid (conviction bearish — call writer)

## Finding

Tradier's timesale stream does **not** include any aggressor-side field. The complete documented timesale payload fields are:

```
type, symbol, exchange, bid, ask, last, size, date,
open, high, low, close, prevclose
```

No `order_side`. No `side`. No `aggressor_side`. No `trade_condition`.

This is a platform-level limitation of Tradier's market data tier. True per-print `order_side` requires:
- **OPRA full feed** (institutional-tier, ~$10k+/month)
- **CBOE LiveVol** (institutional)
- **Polygon.io options trades** (`/v3/trades/{optionsTicker}`) — available on Starter+ plans, includes `conditions` array but condition codes do not map cleanly to buy/sell aggressor in all cases

## Resolution

Fill placement relative to the bid/ask spread is the **industry-standard proxy** for aggression when true `order_side` is unavailable:

| Fill Placement | Contract Type | Interpretation | `is_directionally_aggressive` |
|---|---|---|---|
| `AT_ASK` / `ABOVE_ASK` | CALL | Buyer paying up — bullish opener | ✅ True |
| `AT_ASK` / `ABOVE_ASK` | PUT | Buyer paying up — bearish opener | ✅ True |
| `AT_BID` / `BELOW_BID` | PUT | Put seller writing at bid — conviction bullish | ✅ True |
| `AT_BID` / `BELOW_BID` | CALL | Call seller writing at bid — conviction bearish | ✅ True |
| `MID` | Any | Ambiguous — passive print | ❌ False |

This classification is used by CBOE LiveVol retail UI, Unusual Whales, Market Chameleon, and is the methodology WallStreetJesus almost certainly uses (given his data sources are retail-tier, not OPRA).

**This is more correct for WSJ purposes than `order_side` alone** — because a put seller hitting the bid IS aggressive bullish positioning regardless of which side the exchange flagged as the formal aggressor.

## Impact

- `is_directionally_aggressive(bid_ask_class, contract_type)` in `parsers/bid_ask_classifier.py` requires **no `order_side` parameter**
- `order_side_classifier.py` (`order_side_to_direction()`) is retained as-is for potential future use if OPRA/Polygon data is integrated
- `OptionsFlowEvent.order_side` field is NOT added (no data to populate it from)
- `_stats["order_side_unknown"]` counter is NOT needed (no unknown state to track)

## Future Consideration

If Cipher ever upgrades to Polygon.io options trades endpoint (`/v3/trades/{optionsTicker}`), revisit this decision. The `conditions` array may allow aggressor-side inference for a subset of exchanges. At that point, `order_side_classifier.py` can be activated and `order_side` can be added to `OptionsFlowEvent` and `flow_events` schema.

---

*Decision recorded as part of ING-001 resolution. Referenced by: `docs/SPRINT_WSJ_INGESTION_ALIGNMENT.md`, `docs/ARCHITECTURE.md`*
