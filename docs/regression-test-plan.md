# Cipher Regression and Performance Test Plan

## Automated coverage
- Auth registration, login, `/me`
- Protected route enforcement
- Flow scan response validation
- Stream stats response validation
- Simulation validation guardrails
- WebSocket real-time signal receipt
- Tradier session token: 401 returns `None` (no crash/infinite loop)
- Tradier session token: 200 returns correct `sessionid`
- Tradier session token: network exception returns `None`
- Tradier stream: 401 on stream POST triggers demo-mode fallback (not infinite retry)
- Tradier session token: live integration test (skipped in CI; run locally with `TRADIER_API_KEY` set)

## Manual regression checklist
- Login/logout flow in browser
- Dashboard redirect when unauthenticated
- Flow tab fetch and render
- Swarm tab run and render
- Signal feed updates live
- Stream stats bar updates
- Composite card loads correctly
- Frontend uses configured `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_WS_URL`
- Tradier stream: confirm Railway logs show `INFO tradier_stream` connecting (not 401 loop)

## Performance scenarios to add next
- Load test `/api/flow/scan` with concurrent authenticated users
- WebSocket fan-out test with 50+ concurrent subscribers
- Stream processor benchmark under 1k ticks/minute
- Frontend render profiling for 200-signal feed cap

## Running the Tradier live test locally
```bash
cd backend
TRADIER_API_KEY=<your_key> pytest tests/test_tradier_stream.py \
    -k test_live_tradier_session_token -v
```
This is the equivalent of:
```bash
curl -X POST https://api.tradier.com/v1/markets/events/session \
     -H 'Authorization: Bearer <your_key>' \
     -H 'Accept: application/json'
```
