# Architecture Overview

- **Pair**: HYPE/USDC
- **Threshold**: 3 bps (maker-first); IOC on spikes (threshold + 7 bps).
- **Low-latency path**: HL **WebSocket** for `l2Book` (spot `@index`, perp `HYPE`). Edges computed on every tick.
- **Broadcast**: FastAPI `/ws/edges` to UI; payload includes server-side `latency_ms` and client calculates echo RTT.
- **Persistence**: Postgres tables `edges`, `trades`.
- **Alerts**: SMTP mail when edge ≥ threshold, with request/response and direction.
- **One-command start**: `docker compose up --build` (reads `.env`).

**Why WS and not REST?** Info endpoints (`metaAndAssetCtxs`, `spotMeta`, `l2Book`) exist over REST but constant polling is heavy; WS subscriptions provide steady low-latency updates and help stay within rate limits. (See SDK/API notes.)
