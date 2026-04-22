# Cipher — Technical Specifications

## Application Overview

Cipher is a real-time institutional options flow intelligence platform. It ingests live options trade data from Tradier's WebSocket streaming API, processes trades through a multi-stage signal pipeline, and surfaces actionable BUY/SELL/HOLD recommendations to authenticated users via a Next.js dashboard.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Vercel (Frontend — Next.js 14)                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐   │
│  │  Login Page  │  │  Dashboard   │  │  API Route (Auth)  │   │
│  └──────────────┘  └──────┬───────┘  └────────────────────┘   │
│                            │ HTTP + WebSocket                    │
└────────────────────────────┼────────────────────────────────────┘
                             │
┌────────────────────────────┼────────────────────────────────────┐
│  Railway (Backend — FastAPI)                                     │
│                            │                                     │
│  ┌─────────────────────────▼──────────────────────────────┐    │
│  │  Routers: /api/auth  /api/flow  /api/signals            │    │
│  │           /api/simulation  /ws/signals                  │    │
│  └──────────┬──────────────────────────────────────────────┘    │
│             │                                                     │
│  ┌──────────▼──────────────────────────────────────────────┐    │
│  │  Signal Pipeline                                         │    │
│  │  Tradier Stream → Parser → Accumulator → Composite      │    │
│  └──────────┬──────────────────────────────────────────────┘    │
│             │                                                     │
│  ┌──────────▼──────┐   ┌─────────────┐   ┌─────────────────┐   │
│  │  Async Event Bus │   │  Supabase   │   │  OpenAI GPT-4o  │   │
│  │  (in-process)    │   │ (PostgreSQL)│   │  (Swarm agents) │   │
│  └──────────────────┘   └─────────────┘   └─────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## API Endpoints

### Authentication

#### `POST /api/auth/token`
Authenticate a user and receive a JWT access token.

**Request** (`application/x-www-form-urlencoded`)
```
username=user@example.com&password=secret
```

**Response `200`**
```json
{
  "access_token": "eyJhbGci...",
  "token_type": "bearer"
}
```

**Errors**
| Code | Reason |
|---|---|
| 401 | Invalid credentials |

---

#### `POST /api/auth/register`
Register a new user account.

**Request** (`application/json`)
```json
{
  "email": "user@example.com",
  "password": "securepassword"
}
```

**Response `201`**
```json
{
  "email": "user@example.com",
  "message": "User created successfully"
}
```

**Errors**
| Code | Reason |
|---|---|
| 400 | Email already registered |

---

### Options Flow

#### `GET /api/flow/scan`
Retrieve a list of recent options flow events for a given ticker.

**Auth**: Bearer JWT required

**Query Parameters**
| Parameter | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `ticker` | string | ✅ | — | 1–10 chars, uppercased |
| `limit` | integer | ❌ | 50 | 1–200 |

**Response `200`**
```json
{
  "ticker": "AAPL",
  "events": [
    {
      "ticker": "AAPL",
      "contract_type": "CALL",
      "strike": 200.0,
      "expiry": "2026-05-16",
      "premium": 1500000,
      "trade_type": "SWEEP",
      "sentiment": "BULLISH",
      "influence_tier": "WHALE",
      "conviction_score": 0.87,
      "is_golden_sweep": true,
      "timestamp": "2026-04-22T20:15:00.000Z"
    }
  ]
}
```

**Flow Event Fields**
| Field | Type | Values |
|---|---|---|
| `contract_type` | string | `CALL`, `PUT` |
| `trade_type` | string | `SWEEP`, `BLOCK`, `SPLIT`, `SINGLE` |
| `sentiment` | string | `BULLISH`, `BEARISH`, `NEUTRAL` |
| `influence_tier` | string | `WHALE`, `INSTITUTIONAL`, `LARGE`, `RETAIL` |
| `conviction_score` | float | 0.0 – 1.0 |
| `is_golden_sweep` | bool | `true` if premium ≥ $500K and probabilistically flagged |

**Notes**: Currently returns mock data seeded deterministically by ticker hash. In production, queries Supabase for recent events or calls Tradier options chain.

---

### Composite Signals

#### `GET /api/signals/composite/{ticker}`
Get a composite signal score and recommendation for a ticker.

**Auth**: Bearer JWT required

**Path Parameters**
| Parameter | Type | Constraints |
|---|---|---|
| `ticker` | string | 1–10 chars |

**Response `200`**
```json
{
  "ticker": "NVDA",
  "recommendation": "BUY",
  "composite_score": 0.731,
  "flow_score": 0.812,
  "backtest_score": 0.604,
  "reasoning": "Composite analysis for NVDA: flow score 81%, backtest win-rate 60%. Combined score 73% suggests BUY."
}
```

**Score Formula**
```
composite_score = (flow_score × 0.6) + (backtest_score × 0.4)

Recommendation:
  composite >= 0.65 AND BULLISH → BUY
  composite >= 0.65 AND BEARISH → SELL
  else                          → HOLD
```

---

#### `GET /api/stream/stats`
Get real-time statistics about the Tradier stream.

**Auth**: Bearer JWT required

**Response `200`**
```json
{
  "stats": {
    "active_symbols": 16,
    "ticks": 4821,
    "classified": 4750,
    "signals": 312,
    "errors": 3
  }
}
```

---

### Simulation

#### `POST /api/simulation/run`
Run a multi-agent AI swarm simulation on options flow data for a ticker.

**Auth**: Bearer JWT required

**Request** (`application/json`)
```json
{
  "ticker": "TSLA",
  "flow_events": [ /* array of FlowEventOut objects from /api/flow/scan */ ],
  "n_agents": 6,
  "n_runs": 1
}
```

**Parameters**
| Field | Type | Default | Description |
|---|---|---|---|
| `ticker` | string | required | Ticker to simulate |
| `flow_events` | array | required | Flow events to feed into agents |
| `n_agents` | integer | 6 | Number of LLM agents (max 6) |
| `n_runs` | integer | 1 | Number of simulation runs |

**Response `200`**
```json
{
  "ticker": "TSLA",
  "consensus": "BUY",
  "confidence": 0.78,
  "vote_breakdown": {
    "BUY": 4,
    "SELL": 1,
    "HOLD": 1
  },
  "agent_verdicts": [
    {
      "role": "momentum",
      "name": "Momentum Trader",
      "direction": "BUY",
      "reasoning": "Strong sweep activity with above-ask prints suggests institutional accumulation.",
      "confidence": 0.85
    }
  ],
  "summary": "4 of 6 agents voted BUY with 78% average confidence."
}
```

**Agent Roles**
| Role | Name | Perspective |
|---|---|---|
| `momentum` | Momentum Trader | Follows tape and big money flow |
| `contrarian` | Contrarian Analyst | Fades overextension and crowded trades |
| `fundamental` | Fundamental Analyst | Flow vs. valuation and earnings catalysts |
| `technical` | Technical Analyst | IV and chart pattern context |
| `macro` | Macro Strategist | Broad market condition context |
| `risk` | Risk Manager | Downside, position sizing, tail risk |

---

### WebSocket

#### `WS /ws/signals`
Subscribe to the real-time signal stream.

**Query Parameters**
| Parameter | Type | Required |
|---|---|---|
| `token` | string | ✅ JWT access token |

**Connection**
```
wss://your-api-domain/ws/signals?token=eyJhbGci...
```

**Close Codes**
| Code | Reason |
|---|---|
| 4001 | Invalid or expired JWT |

**Message Types**

*Heartbeat (server → client, every ~25s)*
```json
{ "type": "ping" }
```

*Signal (server → client)*
```json
{
  "type": "signal",
  "ticker": "NVDA",
  "alert_level": "CONVICTION",
  "direction": "BUY",
  "composite_score": 0.82,
  "flow_score": 0.91,
  "backtest_score": 0.68,
  "sentiment": "BULLISH",
  "premium": 2400000,
  "trade_type": "SWEEP",
  "influence_tier": "WHALE",
  "timestamp": "2026-04-22T20:15:00.000Z"
}
```

**Alert Level Thresholds**
| Level | Composite Score |
|---|---|
| `CONVICTION` | ≥ 0.85 |
| `STRONG_SIGNAL` | ≥ 0.70 |
| `ALERT` | ≥ 0.55 |
| `WATCH` | < 0.55 |

---

### Health

#### `GET /health`
Backend health check.

**Response `200`**
```json
{ "status": "ok", "service": "cipher-api" }
```

#### `GET /`
Root endpoint.

**Response `200`**
```json
{ "message": "Cipher API v1.0 — Decode the Market" }
```

---

## Data Models

### `OptionsFlowEvent` (Backend Parser Output)
```python
ticker:           str
contract_type:    str    # CALL | PUT
strike:           float
expiry:           str    # YYYY-MM-DD
premium:          float  # USD
trade_type:       str    # SWEEP | BLOCK | SPLIT | SINGLE
sentiment:        str    # BULLISH | BEARISH | NEUTRAL
influence_tier:   str    # WHALE | INSTITUTIONAL | LARGE | RETAIL
conviction_score: float  # 0.0 – 1.0
is_golden_sweep:  bool
timestamp:        str    # ISO 8601
dte:              int    # Days to expiry
```

### `RepetitionEpisode` (Signal Accumulator)
```python
ticker:          str
contract_type:   str
events:          List[OptionsFlowEvent]
total_premium:   float
trade_count:     int
is_accelerating: bool    # True if recent trades accelerating
```
**Accumulation Window**: 30 minutes  
**Minimum Trades**: 3  
**Minimum Premium**: $50,000

### `CompositeSignal`
```python
ticker:          str
recommendation:  str    # BUY | SELL | HOLD
composite_score: float  # 0.0 – 1.0
flow_score:      float
backtest_score:  float
reasoning:       str
```

---

## Frontend Routes

| Route | Component | Description |
|---|---|---|
| `/` | `page.tsx` | Login / landing page |
| `/dashboard` | `dashboard/page.tsx` | Main authenticated dashboard |

## Frontend State Management

All state is managed via custom React hooks — no external state library (Redux, Zustand, etc.):

| Hook | Purpose | Backend Endpoint |
|---|---|---|
| `useAuth` | JWT storage, login, logout, auth state | `POST /api/auth/token` |
| `useSignalStream` | WebSocket connection, signal queue | `WS /ws/signals` |
| `useFlow` | Options flow events for a ticker | `GET /api/flow/scan` |
| `useSimulation` | Swarm simulation invocation + result | `POST /api/simulation/run` |

---

## Signal Processing Pipeline

```
Tradier WebSocket
      │
      ▼
parse_tradier_trade()          → OptionsFlowEvent
      │
      ▼
bid_ask_classifier             → fill aggressiveness tag
      │
      ▼
trade_type_detector            → SWEEP | BLOCK | SPLIT | SINGLE
      │
      ▼
RepetitionAccumulator          → groups into RepetitionEpisode
  (window=30min, min_trades=3, min_premium=$50K)
      │
      ▼
build_composite()              → CompositeSignal (BUY/SELL/HOLD)
      │
      ▼
async_bus.publish("signals")   → broadcast to all WS clients
```

---

## Default Streaming Symbols

The following 16 symbols are streamed on startup (configurable via env or DB):

```
AAPL, TSLA, NVDA, SPY, QQQ, MSFT, AMZN, META,
GOOGL, AMD, PLTR, SOFI, HOOD, RIVN, CRWD, NET
```

---

## Authentication Flow

1. User submits email/password to `POST /api/auth/token`
2. Backend verifies credentials against Supabase users table
3. Returns signed JWT (HS256, expires in 1440 minutes by default)
4. Frontend stores JWT in memory (not localStorage — sandboxed iframe safe)
5. All subsequent HTTP requests include `Authorization: Bearer <token>` header
6. WebSocket connections pass `?token=<jwt>` as query parameter
7. `get_current_user` FastAPI dependency validates JWT on every protected route

---

## Deployment Configuration

### Backend (Railway)
- **Procfile**: `web: uvicorn main:app --host 0.0.0.0 --port $PORT`
- **railway.toml**: defines build and start commands
- **Health check**: `GET /health`

### Frontend (Vercel)
- **vercel.json**: route rewrites and headers
- **Build**: `npm run build` (Next.js static + SSR)
- **Environment**: set via Vercel project settings

---

## Performance & Limits

| Constraint | Value |
|---|---|
| Flow scan max limit | 200 events per request |
| Ticker max length | 10 characters |
| WS heartbeat interval | 25 seconds |
| JWT expiry | 1440 minutes (24 hours) |
| Swarm max agents | 6 |
| Repetition window | 30 minutes |
| Min episode premium | $50,000 |
| Min episode trades | 3 |
| Premium cap for flow score | $10,000,000 |

