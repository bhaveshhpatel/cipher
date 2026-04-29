# Cipher — Signal Engine

Reference for the composite signal scoring pipeline. Source of truth: `backend/signals/`.

---

## Overview

Every options flow tick that passes Layers 1–4 (registry lookup, parse, dedup) enters the
repetition accumulator. When the accumulator emits a qualifying episode (Gate 1 + Gate 2), the
composite signal engine scores it across three dimensions and derives a recommendation.

The full pipeline per tick:

```
RepetitionAccumulator.ingest_tick(ev)
    └── Gate 1 (persist): trade_count >= 3 OR total_premium >= $10,000
    └── Gate 2 (retrigger): Δ total_premium >= $50,000 since last emission
           ↓
        RepetitionEpisode returned
           ↓
        build_composite(ep, accumulator)
           ↓
        CompositeSignal
           ↓
        bus.publish_all("signals")         → WebSocket delivery
        bus.publish_all("db_writer")       → flow_episodes table
        bus.publish_all("signal_writer")   → signal_history table
```

---

## Accumulator Gating

Source: `backend/signals/repetition_accumulator.py`

### Episode Key

```
(ticker, contract_type, strike, expiry)
```

One episode per unique contract. Episode resets after 30 minutes of inactivity.

### Gate 1 — Persist Threshold (OR logic)

| Condition | Threshold |
|---|---|
| `trade_count` | ≥ 3 ticks |
| `total_premium` | ≥ $10,000 |

Below both: `ingest_tick()` returns `None` — tick dropped, `accumulator_gated` stat incremented.

Single large print ($10k+) fires on tick 1 via the premium OR branch. Repeated sub-$10k prints need 3 ticks. Pure retail noise ($9k AND 2 ticks) never fires.

### Gate 2 — Signal Re-Emission Guard

After Gate 1 is crossed, a new signal is only emitted when:

```
total_premium - last_signaled_premium >= SIGNAL_RETRIGGER_THRESHOLD ($50,000)
```

Or on the **first** Gate 1 crossing (`last_signaled_premium == 0`).

This prevents QQQ/SPY episodes from writing a new `signal_history` row on every tick once threshold is crossed.

### Acceleration Flag

`is_accelerating = True` when ≥ 2 ticks within the last 5 minutes on the same episode.

---

## Composite Score Formula

Source: `backend/signals/composite_signal_engine.py`

\[
\text{composite\_score} = \text{flow\_score} \times 0.55 + \text{backtest\_score} \times 0.35 + \text{volume\_premium\_factor} \times 0.10
\]

### Component 1 — Flow Score (weight: 55%)

```python
def compute_flow_score(ep: RepetitionEpisode) -> float:
    prem   = min(ep.total_premium / 10_000_000, 1.0)   # normalized to $10M cap
    accel  = 0.15 if ep.is_accelerating else 0.0
    trades = min(ep.trade_count / 20, 0.20)             # capped at 0.20 (20 trades)
    return round(min(1.0, prem * 0.65 + accel + trades), 3)
```

| Sub-component | Formula | Max contribution |
|---|---|---|
| Premium component | `(total_premium / $10M) × 0.65` | 0.65 |
| Acceleration bonus | `+0.15` if ≥ 2 ticks within 5 min | 0.15 |
| Trade count | `min(trade_count / 20, 0.20)` | 0.20 |
| **Flow score cap** | `min(1.0, sum)` | **1.00** |

### Component 2 — Backtest Score (weight: 35%)

Source: `backend/signals/backtest_validator.py`

```python
bt_s = get_backtest_score(
    ep.ticker,
    ep.contract_type,
    latest.dte,
    latest.influence_tier,
)
```

Historical win-rate lookup by `(ticker, contract_type, DTE bucket, influence_tier)`.
Returns a float in `[0.0, 1.0]`. Returns `0.5` (neutral) if no historical data exists for the combination.

Influence tier is one of: `WHALE`, `INSTITUTIONAL`, `LARGE`, `RETAIL`.

### Component 3 — Volume-Premium Factor (weight: 10%)

```python
def volume_weighted_premium_factor(ep: RepetitionEpisode) -> float:
    """min(1.0, premium / (oi * 100)). Returns 0.5 when OI is zero."""
    latest_oi = getattr(ep.events[-1], "open_interest", 0) or 0
    if latest_oi <= 0:
        return 0.5
    premium = getattr(ep.events[-1], "premium", 0) or 0
    return round(min(1.0, premium / (latest_oi * 100)), 4)
```

Measures the size of the current print relative to open interest. A print that represents a large fraction of OI is more significant than one that is noise-level relative to total outstanding contracts. Returns `0.5` as neutral when OI data is unavailable.

---

## Recommendation Derivation

```python
if composite_score >= 0.65 and sentiment == "BULLISH":
    rec = "BUY"
elif composite_score >= 0.65 and sentiment == "BEARISH":
    rec = "SELL"
else:
    rec = "HOLD"
```

`sentiment` comes from the latest tick in the episode (`latest.sentiment`), classified by the parser
as `BULLISH`, `BEARISH`, or `NEUTRAL` based on `contract_type` and `bid_ask_class`.

| Composite Score | Sentiment | Recommendation |
|---|---|---|
| ≥ 0.65 | BULLISH | BUY |
| ≥ 0.65 | BEARISH | SELL |
| < 0.65 | Any | HOLD |
| ≥ 0.65 | NEUTRAL | HOLD |

---

## Alert Levels

Source: `backend/signals/repetition_accumulator.py` → `get_alert_level(ep)`

Alert level is derived from the episode's **cumulative total premium**, independently of the composite
score. It is injected into the bus message in `tradier_stream._process_trade()` before publish,
and persisted to `flow_episodes.alert_level` by `flow_store._bus_signal_listener`.

| Level | Total Premium |
|---|---|
| `CONVICTION` | ≥ $1,000,000 |
| `STRONG_SIGNAL` | ≥ $500,000 |
| `ALERT` | ≥ $200,000 |
| `WATCH` | < $200,000 |

> `alert_level` and `recommendation` are distinct fields. `recommendation` is BUY/SELL/HOLD from
> the composite score. `alert_level` is CONVICTION/STRONG_SIGNAL/ALERT/WATCH from premium size.
> Do not conflate them. See ALERT-LEVEL fix in `docs/FIXES.md`.

---

## CompositeSignal Dataclass

```python
@dataclass
class CompositeSignal:
    ticker:                str
    recommendation:        str          # BUY / SELL / HOLD
    composite_score:       float        # [0.0, 1.0]
    flow_score:            float        # [0.0, 1.0]
    backtest_score:        float        # [0.0, 1.0]
    volume_premium_factor: float        # [0.0, 1.0]
    reasoning:             str          # human-readable string
    swarm_direction:       Optional[str]    # populated only by build_composite_async
    swarm_confidence:      Optional[float]
    swarm_bull_votes:      Optional[int]
    swarm_bear_votes:      Optional[int]
    swarm_hold_votes:      Optional[int]
    swarm_agents:          List[dict]
```

`swarm_*` fields are `None` when using `build_composite()` (synchronous path, per-tick production).
They are populated only when `build_composite_async()` is called with a live `run_ensemble` function
(explicit admin/swarm invocation).

---

## Reasoning String

Auto-generated on every signal. Example:

```
"5 CALL trades on NVDA ($1,240,000 total premium).
Flow score 52%, backtest win-rate 68%, volume-premium factor 50%.
Accelerating flow detected.
Composite: 71% -> BUY."
```

---

## Swarm Engine (Explicit Invocation Only)

Source: `backend/services/swarm_engine.py`, `backend/simulation/ensemble_runner.py`

- 12 Groq `llama-3.3-70b-versatile` agents each independently reason over the flow event list.
- Agents vote BUY / SELL / HOLD. Majority direction sets `swarm_direction`.
- `swarm_confidence` = fraction of agents agreeing with majority.
- **Not called automatically per tick.** Must be explicitly invoked via admin panel or direct API call.
- Called via `build_composite_async(ep, accumulator)` — the async variant of `build_composite`.
- `build_composite()` (sync) used in `_process_trade()` hot path — no swarm involved.

---

## Bus Payload Shape

Signal emitted on `"signals"` channel (type = `"signal"`):

```json
{
  "type": "signal",
  "data": {
    "ticker":          "NVDA",
    "direction":       "REPEAT_BUY",
    "contract_type":   "CALL",
    "strike":          950.0,
    "expiry":          "2026-06-20",
    "total_premium":   1240000,
    "trade_count":     5,
    "alert_level":     "CONVICTION",
    "is_accelerating": true,
    "seed_episode":    "NVDA CALL $950 2026-06-20 trades=5 prem=$1,240,000",
    "timestamp":       "2026-04-28T14:32:00.000Z"
  }
}
```

Composite signal emitted on all channels (type = `"composite_signal"`):

```json
{
  "type": "composite_signal",
  "data": {
    "signal": {
      "ticker":                "NVDA",
      "recommendation":        "BUY",
      "composite_score":       0.712,
      "flow_score":            0.731,
      "backtest_score":        0.680,
      "volume_premium_factor": 0.500,
      "alert_level":           "CONVICTION",
      "reasoning":             "5 CALL trades on NVDA ($1,240,000 total premium)..."
    },
    "episode": {
      "contract_type":   "CALL",
      "direction":       "REPEAT_BUY",
      "influence_tier":  "WHALE",
      "total_premium":   1240000,
      "trade_count":     5,
      "is_accelerating": true,
      "timestamp":       "2026-04-28T14:32:00.000Z"
    }
  }
}
```

---

## Influence Tiers

Classified by parser in `parsers/options_flow_parser.py` based on episode-level premium:

| Tier | Premium Threshold |
|---|---|
| `WHALE` | ≥ $1,000,000 |
| `INSTITUTIONAL` | ≥ $500,000 |
| `LARGE` | ≥ $100,000 |
| `RETAIL` | < $100,000 |

Used as a dimension in `get_backtest_score()` — historical win-rates are segmented by tier because
institutional flow has different predictive characteristics than retail flow.

---

## Signal Persistence

| Table | Writer | Trigger |
|---|---|---|
| `flow_episodes` | `flow_store._bus_signal_listener` | `composite_signal` on `db_writer` channel |
| `signal_history` | `signal_store` listener | `composite_signal` on `signal_writer` channel |

Both subscribers read `sig.get("alert_level")` — not `recommendation` — for the alert level field.
