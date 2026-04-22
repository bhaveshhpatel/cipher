# Cipher — Business Features

> Feature registry for the Cipher institutional options flow intelligence platform.
> Each feature has a unique ID, status, and description.
>
> **Status Legend**
> - `✅ LIVE` — Fully implemented and deployed
> - `🟡 PARTIAL` — Implemented but using mock/stub data
> - `🔧 WIRED` — Infrastructure exists, not yet active
> - `📋 PLANNED` — Defined but not yet built

---

## Authentication & Access Control

### CIP-AUTH-001 — User Registration
**Status**: ✅ LIVE  
Users can register for a Cipher account with email and password. Passwords are hashed using bcrypt via `passlib`. User records are stored in Supabase (PostgreSQL).

---

### CIP-AUTH-002 — JWT Login
**Status**: ✅ LIVE  
Registered users can authenticate with email and password to receive a signed JWT access token (HS256). Token expiry defaults to 24 hours (1440 minutes), configurable via `ACCESS_TOKEN_EXPIRE_MINUTES`.

---

### CIP-AUTH-003 — Protected Route Guard (Backend)
**Status**: ✅ LIVE  
All API endpoints (except `/health`, `/`, `/api/auth/*`) require a valid Bearer JWT in the `Authorization` header. Requests with missing or invalid tokens receive a `401 Unauthorized` response.

---

### CIP-AUTH-004 — Protected Route Guard (Frontend)
**Status**: ✅ LIVE  
The `/dashboard` page checks authentication state on mount. Unauthenticated users are automatically redirected to the login page (`/`).

---

### CIP-AUTH-005 — Session Sign Out
**Status**: ✅ LIVE  
Users can sign out from the dashboard header. The JWT is cleared from in-memory state and the user is redirected to the login page.

---

## Real-Time Signal Streaming

### CIP-STREAM-001 — Tradier WebSocket Connection
**Status**: 🟡 PARTIAL  
The backend connects to Tradier's streaming API on startup. If a valid Tradier API key is present, live option trade ticks are processed. Without a key, the system automatically falls back to demo mode (simulated tick generation).

---

### CIP-STREAM-002 — Demo Mode (Simulated Ticks)
**Status**: ✅ LIVE  
When no Tradier API key is configured, the backend runs a built-in demo mode that generates realistic simulated options flow ticks. This allows full platform functionality without a live brokerage connection.

---

### CIP-STREAM-003 — Default Symbol Watchlist
**Status**: ✅ LIVE  
The platform streams 16 high-activity symbols by default on startup: `AAPL, TSLA, NVDA, SPY, QQQ, MSFT, AMZN, META, GOOGL, AMD, PLTR, SOFI, HOOD, RIVN, CRWD, NET`. Configurable via environment variable or database.

---

### CIP-STREAM-004 — In-Process Async Event Bus
**Status**: ✅ LIVE  
An in-process async pub/sub event bus routes processed signals from the streaming pipeline to all active WebSocket client subscribers. Supports multiple concurrent subscribers without blocking.

---

### CIP-STREAM-005 — Real-Time Signal WebSocket Feed
**Status**: ✅ LIVE  
Authenticated users can subscribe to a live WebSocket feed at `WS /ws/signals?token=<jwt>`. Signals are pushed in real time as they are generated. A heartbeat ping is sent every 25 seconds to keep connections alive.

---

### CIP-STREAM-006 — Stream Health Statistics
**Status**: ✅ LIVE  
A dedicated endpoint (`GET /api/stream/stats`) exposes real-time streaming health metrics: active symbols, total ticks received, classified trades, signals generated, and error count.

---

## Options Flow Parsing & Classification

### CIP-FLOW-001 — Raw Trade Parsing
**Status**: ✅ LIVE  
Raw trade ticks from Tradier are parsed into structured `OptionsFlowEvent` objects containing: ticker, contract type, strike, expiry, premium, DTE (days to expiry), and timestamp.

---

### CIP-FLOW-002 — Bid/Ask Aggressiveness Classification
**Status**: ✅ LIVE  
Each trade is classified by fill aggressiveness relative to the bid/ask spread: `ABOVE_ASK` (aggressive buyer), `AT_ASK`, `AT_MID`, `AT_BID`, `BELOW_BID` (aggressive seller). This is a key indicator of institutional intent.

---

### CIP-FLOW-003 — Trade Type Detection
**Status**: ✅ LIVE  
Each trade is classified by execution pattern:
- **SWEEP** — Multi-exchange aggressive buy sweeping multiple levels
- **BLOCK** — Single large-size print
- **SPLIT** — Large order split across multiple fills
- **SINGLE** — Standard single-exchange fill

---

### CIP-FLOW-004 — Influence Tier Classification
**Status**: ✅ LIVE  
Each trade is assigned an influence tier based on premium size and trade characteristics:
- **WHALE** — Highest premium, institutional-scale
- **INSTITUTIONAL** — Large, structured flow
- **LARGE** — Above-average retail/small institutional
- **RETAIL** — Standard retail-size trades

---

### CIP-FLOW-005 — Golden Sweep Detection
**Status**: ✅ LIVE  
Trades with premium ≥ $500,000 that also exhibit sweep characteristics are flagged as **Golden Sweeps** — a high-conviction bullish signal used by institutional traders.

---

### CIP-FLOW-006 — Flow Scan by Ticker
**Status**: 🟡 PARTIAL  
Users can request a scan of recent options flow events for any ticker (`GET /api/flow/scan?ticker=AAPL`). Currently returns mock data seeded deterministically by ticker. In production, will query Supabase for live events.

---

## Signal Intelligence & Scoring

### CIP-SIGNAL-001 — Repetition Accumulator
**Status**: ✅ LIVE  
The platform monitors for repeated, directionally consistent options activity in a rolling 30-minute window. A `RepetitionEpisode` is triggered when a minimum of 3 trades with at least $50,000 total premium are detected for a ticker/contract direction.

---

### CIP-SIGNAL-002 — Flow Score Calculation
**Status**: ✅ LIVE  
A flow score (0–1) is computed per episode based on:
- Total premium (weight: 0.65, capped at $10M)
- Acceleration (weight: 0.15 if trades are accelerating)
- Trade count (weight: up to 0.20)

---

### CIP-SIGNAL-003 — Backtest Win-Rate Scoring
**Status**: ✅ LIVE  
A historical win-rate score (0–1) is assigned based on the combination of: ticker, contract type (CALL/PUT), DTE bucket, and influence tier. This score reflects how frequently similar setups have been profitable historically.

---

### CIP-SIGNAL-004 — Composite Signal Score
**Status**: ✅ LIVE  
A final composite score is computed as a weighted blend:
```
composite_score = (flow_score × 0.60) + (backtest_score × 0.40)
```
This score determines the final BUY/SELL/HOLD recommendation and alert level.

---

### CIP-SIGNAL-005 — BUY/SELL/HOLD Recommendation
**Status**: ✅ LIVE  
A structured recommendation is generated from the composite score and dominant sentiment:
- `composite ≥ 0.65` AND `BULLISH` → **BUY**
- `composite ≥ 0.65` AND `BEARISH` → **SELL**
- All other cases → **HOLD**

---

### CIP-SIGNAL-006 — Alert Level Assignment
**Status**: ✅ LIVE  
Each signal is assigned one of four alert levels based on composite score:
- **CONVICTION** — Highest confidence (score ≥ 0.85)
- **STRONG_SIGNAL** — High confidence (score ≥ 0.70)
- **ALERT** — Moderate confidence (score ≥ 0.55)
- **WATCH** — Monitoring (score < 0.55)

---

### CIP-SIGNAL-007 — Composite Signal API
**Status**: 🟡 PARTIAL  
Users can request a composite signal for any ticker via `GET /api/signals/composite/{ticker}`. Currently returns mock composite data. In production, serves live composite signals from the streaming pipeline.

---

### CIP-SIGNAL-008 — Mid-Cap Unusual Activity Screener
**Status**: 🔧 WIRED  
A screener module (`midcap_screener.py`) is implemented to detect unusual options activity in mid-cap stocks. Not yet integrated into the main streaming pipeline or exposed via API.

---

## AI Swarm Simulation

### CIP-SWARM-001 — Multi-Agent Simulation Engine
**Status**: ✅ LIVE  
Users can run a multi-agent AI simulation on options flow data for any ticker. The simulation dispatches flow context to multiple GPT-4o-mini agents concurrently, each with a distinct trading persona and analytical approach.

---

### CIP-SWARM-002 — Momentum Trader Agent
**Status**: ✅ LIVE  
An aggressive momentum-focused agent that evaluates options flow by following the tape and big money prints. Returns `BUY/SELL/HOLD` with one-sentence reasoning.

---

### CIP-SWARM-003 — Contrarian Analyst Agent
**Status**: ✅ LIVE  
A contrarian-focused agent that looks for overextension and crowded trades to fade. Provides a counter-consensus perspective on the flow data.

---

### CIP-SWARM-004 — Fundamental Analyst Agent
**Status**: ✅ LIVE  
A fundamentals-grounded agent that weighs options flow against valuation metrics and earnings catalysts. Adds macro corporate context to the verdict.

---

### CIP-SWARM-005 — Technical Analyst Agent
**Status**: ✅ LIVE  
A technical analysis-focused agent that evaluates options flow in the context of chart patterns, support/resistance levels, and implied volatility (IV) dynamics.

---

### CIP-SWARM-006 — Macro Strategist Agent
**Status**: ✅ LIVE  
A macroeconomic perspective agent that frames options flow within broader market conditions, sector rotation, and macro risk environment.

---

### CIP-SWARM-007 — Risk Manager Agent
**Status**: ✅ LIVE  
A risk-focused agent that emphasizes downside scenarios, position sizing implications, and tail risk in its verdict. Acts as a counterbalance to momentum-driven agents.

---

### CIP-SWARM-008 — Ensemble Verdict Aggregation
**Status**: ✅ LIVE  
Agent verdicts (BUY/SELL/HOLD) are aggregated by `ensemble_runner.py` into a consensus direction, confidence score, and vote breakdown (e.g., `{BUY: 4, SELL: 1, HOLD: 1}`).

---

### CIP-SWARM-009 — Configurable Agent Count
**Status**: ✅ LIVE  
Users can configure the number of agents to run (1–6) per simulation from the dashboard UI. This controls cost vs. consensus depth trade-off.

---

### CIP-SWARM-010 — Multiple Simulation Runs
**Status**: ✅ LIVE  
Users can configure multiple simulation runs (`n_runs`) to observe variance in agent verdicts across repeated evaluations of the same flow data.

---

## Dashboard & UI

### CIP-UI-001 — Login / Landing Page
**Status**: ✅ LIVE  
A dedicated login page (`/`) with email/password authentication form. On success, user is redirected to `/dashboard` with JWT stored in-memory.

---

### CIP-UI-002 — Main Dashboard
**Status**: ✅ LIVE  
A single-page authenticated dashboard at `/dashboard` with a dark terminal aesthetic (near-black background, JetBrains Mono font, cyan/purple/gold accent system). Houses all core features via tabbed navigation.

---

### CIP-UI-003 — FLOW Tab — Options Flow Table
**Status**: ✅ LIVE  
Users can enter a ticker symbol and trigger a flow scan. Results are displayed in a sortable table showing: ticker, contract type, strike, expiry, premium, trade type, sentiment, influence tier, conviction score, and golden sweep flag.

---

### CIP-UI-004 — SWARM Tab — AI Simulation Panel
**Status**: ✅ LIVE  
Users can trigger an AI swarm simulation against loaded flow data. The panel displays: consensus verdict, confidence, vote breakdown, and individual agent verdicts with reasoning.

---

### CIP-UI-005 — Live Signal Feed
**Status**: ✅ LIVE  
A real-time scrolling feed of incoming WebSocket signals displayed in the dashboard. Signals are color-coded and badged by alert level (CONVICTION → cyan, STRONG_SIGNAL → purple, ALERT → gold, WATCH → muted). Shows premium formatted as `$1.5M` / `$500K`.

---

### CIP-UI-006 — Composite Signal Card
**Status**: ✅ LIVE  
A card component displaying the composite signal score, recommendation, flow score, backtest score, and reasoning text for the active ticker.

---

### CIP-UI-007 — Stream Stats Bar
**Status**: ✅ LIVE  
A persistent stats bar showing real-time stream health: active symbols, ticks processed, classified trades, signals generated, and error count.

---

### CIP-UI-008 — Cipher SVG Logo
**Status**: ✅ LIVE  
A custom inline SVG logo (`CipherLogo.tsx`) with configurable size and optional tagline display. Used in the dashboard navigation header.

---

### CIP-UI-009 — Active Ticker Indicator
**Status**: ✅ LIVE  
When a ticker has been scanned, it is displayed as a highlighted badge in the dashboard nav bar for persistent context awareness during analysis.

---

## Trade Execution

### CIP-EXEC-001 — Tradier Order Placement
**Status**: 🔧 WIRED  
A `trade_executor.py` module is implemented with Tradier order placement logic. Not yet wired into the signal pipeline or exposed via UI. Intended for future one-click trade execution from signal recommendations.

---

## Infrastructure & DevOps

### CIP-INFRA-001 — Backend CI/CD (Railway)
**Status**: ✅ LIVE  
GitHub Actions workflow (`backend.yml`) automatically builds and deploys the FastAPI backend to Railway on push to `main`.

---

### CIP-INFRA-002 — Frontend CI/CD (Vercel)
**Status**: ✅ LIVE  
GitHub Actions workflow (`frontend.yml`) automatically builds and deploys the Next.js frontend to Vercel on push to `main`.

---

### CIP-INFRA-003 — Supabase Database Integration
**Status**: 🔧 WIRED  
Supabase (PostgreSQL) is configured with URL, anon key, and service role key in environment variables. Auth uses Supabase for user storage. Flow data persistence and query are planned but not yet implemented in routers.

---

### CIP-INFRA-004 — Redis Integration
**Status**: 📋 PLANNED  
`REDIS_URL` is defined in config. Redis is intended for caching flow events, rate limiting, and pub/sub scaling beyond the in-process async bus. Not yet integrated.

---

### CIP-INFRA-005 — CORS Configuration
**Status**: ✅ LIVE  
Backend CORS is configured via `ALLOWED_ORIGINS` environment variable (comma-separated). Supports multiple origins for local development and production frontend domains.

