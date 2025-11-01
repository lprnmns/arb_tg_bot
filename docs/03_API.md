# API

- `GET /health` — status.
- `GET /scanner` — last 200 edges (JSON).
- `WS /ws/edges` — pushes live payloads:
```json
{
  "ts":"...",
  "base":"HYPE",
  "spot_index": 107,
  "edge_ps_mm_bps": 3.12,
  "edge_sp_mm_bps": 1.08,
  "mid_ref": 0.1234,
  "latency_ms": 14,
  "threshold_bps": 3
}
```
