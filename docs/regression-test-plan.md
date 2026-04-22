
# Cipher Regression and Performance Test Plan

## Automated coverage
- Auth registration, login, `/me`
- Protected route enforcement
- Flow scan response validation
- Stream stats response validation
- Simulation validation guardrails
- WebSocket real-time signal receipt

## Manual regression checklist
- Login/logout flow in browser
- Dashboard redirect when unauthenticated
- Flow tab fetch and render
- Swarm tab run and render
- Signal feed updates live
- Stream stats bar updates
- Composite card loads correctly
- Frontend uses configured `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_WS_URL`

## Performance scenarios to add next
- Load test `/api/flow/scan` with concurrent authenticated users
- WebSocket fan-out test with 50+ concurrent subscribers
- Stream processor benchmark under 1k ticks/minute
- Frontend render profiling for 200-signal feed cap
