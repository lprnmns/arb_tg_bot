# Execution & Risk

- **Maker-first**: we aim to quote/hedge as maker; use IOC only when a spike crosses `threshold + SPIKE_EXTRA_BPS_FOR_IOC`.
- **Rate cap**: ≤ `MAX_TRADES_PER_MIN_PER_PAIR` (default 3).
- **Min notional**: $10 per order.
- **Dead-man's switch**: live runs send `/exchange:scheduleCancel` over WS-POST after each maker clip; configure delay via `DEADMAN_SECONDS` (default 5s).
- **Neutrality**: keep spot–perp delta neutral; if one leg fails, auto-flatten.
