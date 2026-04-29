# Cipher — API Reference

> Last updated: 2026-04-28 (branch: stable/ingestion-flow-2026-04-28)
> Base URL (production): `https://cipher-backend.up.railway.app`
> All `/api/*` routes require `Authorization: Bearer <jwt>` unless noted.

---

## Authentication

### `POST /api/auth/register`
Register a new user.

**Body (JSON)**
```json
{ "email": "user@example.com", "password": "string" }
```

**Response `200`**
```json
{ "access_token": "<jwt>", "token_type": "bearer" }
```

---

### `POST /api/auth/login`
Login and receive a JWT.

**Body (JSON)**
```json
{ "email": "user@example.com", "password": "string" }
```

**Response `200`**
```json
{ "access_token": "<jwt>", "token_type": "bearer" }
```

---

### `GET /api/auth/me`
Returns the currently authenticated user.

**Response `200`**
```json
{ "id": "uuid", "email": "user@example.com", "role": "user" }
```

---

## Flow

### `GET /api/flow/scan`
Paginated scan of persisted flow episodes.

> Queries `flow_episodes` table (not `flow_events`). Fixed in Phase 4.

**Query Parameters**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `ticker` | string | — | Filter by underlying symbol (e.g. `SPY`) |
| `min_premium` | float | — | Minimum total premium on episode |
| `direction` | string | — | `BUY` \| `SELL` \| `HOLD` |
| `alert_level` | string | — | `CONVICTION` \| `STRONG_SIGNAL` \| `ALERT` \| `WATCH` |
| `limit` | int | 50 | Max rows returned (max 200) |
| `offset` | int | 0 | Pagination offset |

**Response `200`**
```json
{
  "episodes": [
    {
      "id": "uuid",
      "ticker": "SPY",
      "direction": "BUY",
      "alert_level": "CONVICTION",
      "total_premium": 1250000.0,
      "trade_count": 12,
      "first_seen_at": "2026-04-28T14:30:00Z",
      "last_seen_at": "2026-04-28T14:45:00Z",
      "is_accelerating": true
    }
  ],
  "total": 142,
  "limit": 50,
  "offset": 0
}
```

> **Note:** `alert_level` is one of `CONVICTION / STRONG_SIGNAL / ALERT / WATCH` driven by `RepetitionAccumulator.get_alert_level()`. Fixed 2026-04-28 — previously always returned `WATCH`.

---

## Signals

### `GET /api/signals/composite/{ticker}`
Returns the latest composite signal for a ticker. Hits live DB first, falls back to mock.

**Path Parameters**

| Param | Type | Description |
|-------|------|-------------|
| `ticker` | string | Underlying symbol (e.g. `AAPL`) |

**Response `200`**
```json
{
  "ticker": "AAPL",
  "direction": "BUY",
  "composite_score": 0.82,
  "flow_score": 0.91,
  "backtest_score": 0.75,
  "volume_premium_factor": 0.68,
  "conviction": 0.82,
  "tier": "T1",
  "alert_level": "CONVICTION",
  "swarm_direction": "BUY",
  "swarm_confidence": 0.87,
  "swarm_votes": { "BUY": 9, "SELL": 2, "HOLD": 1 },
  "generated_at": "2026-04-28T14:30:00Z",
  "source": "live"
}
```

**Score Formula**
```
composite_score = flow_score × 0.55 + backtest_score × 0.35 + volume_premium_factor × 0.10
direction       = "BUY" | "SELL" if composite_score ≥ 0.65, else "HOLD"
```

---

### `GET /api/signals/list`
Paginated list of composite signals with optional filters.

**Query Parameters**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `ticker` | string | — | Filter by symbol |
| `direction` | string | — | `BUY` \| `SELL` \| `HOLD` |
| `tier` | string | — | `T1` \| `T2` \| `T3` |
| `min_conviction` | float | — | Minimum composite score (0.0–1.0) |
| `limit` | int | 20 | Max rows (max 100) |
| `offset` | int | 0 | Pagination offset |

**Response `200`**
```json
{
  "signals": [ { "...": "CompositeSignal fields" } ],
  "total": 87,
  "source": "live"
}
```

---

### `GET /api/signals/history`
Paginated signal history from `signal_history` table.

**Query Parameters**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `ticker` | string | — | Filter by symbol |
| `direction` | string | — | `BUY` \| `SELL` \| `HOLD` |
| `tier` | string | — | `T1` \| `T2` \| `T3` |
| `min_conviction` | float | — | Minimum conviction score |
| `limit` | int | 50 | Max rows (max 200) |
| `offset` | int | 0 | Pagination offset |

> Auth: `SUPABASE_SERVICE_ROLE_KEY` required server-side. No anon fallback.

---

## Simulation

### `POST /api/simulate`
Runs the AI swarm against a provided flow event. Returns ensemble result.

**Body (JSON)**
```json
{
  "ticker": "SPY",
  "direction": "BUY",
  "premium": 500000,
  "conviction": 0.88,
  "n_agents": 6,
  "n_runs": 1
}
```

| Field | Type | Constraints |
|-------|------|-------------|
| `n_agents` | int | Snapped to nearest: 3, 6, 9, 12 |
| `n_runs` | int | 1–10 |

**Response `200`**
```json
{
  "direction": "BUY",
  "confidence": 0.83,
  "bull_count": 5,
  "bear_count": 1,
  "hold_count": 0,
  "agents": [
    { "name": "MomentumAgent", "vote": "BUY", "confidence": 0.91 }
  ],
  "source": "groq"
}
```

> Without `GROQ_API_KEY` set, returns `HOLD` with `source: "fallback"`.

---

## Health

### `GET /api/health/stream`
Live stream pipeline health. No DB queries — reads in-process `_stats` dict.
Bearer token required.

**Response `200`**

```json
{
  "mode": "live",
  "active_symbols": 31920,
  "ticks": 482910,
  "classified": 44821,
  "deduped": 3201,
  "signals": 87,
  "errors": 2,
  "reconnects": 1,
  "last_tick_at": "2026-04-28T20:45:01.234Z",
  "last_reconnect_at": "2026-04-28T14:31:00.000Z",
  "uptime_seconds": 22541.3
}
```

**Field Reference**

| Field | Type | Description |
|-------|------|-------------|
| `mode` | string | `starting` \| `live` \| `demo` \| `idle` \| `reconnecting` \| `market_closed` |
| `active_symbols` | int | OCC contracts currently covered by stream workers |
| `ticks` | int | Total raw timesale events received since process start |
| `classified` | int | Events that passed parse + dedup and reached the accumulator |
| `deduped` | int | Events dropped by Layer 4 DedupCache |
| `signals` | int | Composite signals emitted to `AsyncEventBus` |
| `errors` | int | Stream-level errors (connection, parse failures) |
| `reconnects` | int | Number of worker reconnect attempts |
| `last_tick_at` | ISO-8601 \| null | UTC timestamp of last classified tick |
| `last_reconnect_at` | ISO-8601 \| null | UTC timestamp of last reconnect |
| `uptime_seconds` | float | Seconds since process started |

> Added in B-008. `classified`, `deduped`, `signals` were always `0` before FLOW-DEBUG fix (2026-04-28) upgraded drop gate logging and wired stat increments correctly.

---

## Admin

> All admin endpoints require `role: admin` in the JWT payload. Scoped to `bhaveshhpatel@yahoo.com`.

### `GET /api/admin/tier-thresholds`
Returns current tier classification thresholds and cache state.

**Response `200`**
```json
{
  "t1_min_volume": 5000000,
  "t1_min_price": 10.0,
  "t1_min_oi": 500,
  "t2_min_volume": 1000000,
  "t2_min_price": 5.0,
  "t2_min_oi": 100,
  "cache_age_seconds": 42,
  "cache_ttl_seconds": 300
}
```

---

### `PATCH /api/admin/tier-thresholds`
Update one or more tier threshold values.

**Body (JSON, partial)**
```json
{ "t1_min_volume": 7500000, "t1_min_oi": 750 }
```

**Response `200`** — updated thresholds object (same shape as GET).

> Invalidates the 300s in-process threshold cache immediately.

---

### `GET /api/admin/tier-distribution`
Returns count of symbols currently in each tier from the active universe snapshot.

**Response `200`**
```json
{ "T1": 42, "T2": 318, "T3": 2841, "unclassified": 14719 }
```

---

## WebSocket

### `WS /api/ws`
Real-time signal delivery. JWT passed as query param on connect.

```
wss://cipher-backend.up.railway.app/api/ws?token=<jwt>
```

**Connection lifecycle**
1. Server validates JWT on connect. Invalid → close `4001`.
2. Server sends `{"type": "ping"}` every 25s.
3. Client must reply `{"type": "pong"}` within 10s or server closes `1001`.
4. Server pushes `{"type": "signal", "data": { ...CompositeSignal }}` on each new signal.

> **Frontend B-026:** Pong response not yet implemented in the frontend. Railway will kill the connection after 10s of no pong.

---

## Error Responses

| Status | Meaning |
|--------|---------|
| `401` | Missing or invalid JWT |
| `403` | Valid JWT but insufficient role (admin-only route) |
| `422` | Request validation error (Pydantic) |
| `500` | Internal server error — check Railway logs |
