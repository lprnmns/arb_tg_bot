# SDK & API Notes

- **Info** endpoints: `metaAndAssetCtxs`, `spotMeta`, `l2Book` to bootstrap assets and get orderbooks.
- **WS**: wss://api.hyperliquid.xyz/ws; subscriptions via `{"method":"subscribe","subscription":{"type":"..."}}`.
- **WS POST**: send signed exchange actions over the same WS with `{"method":"post","id":123,"request":{"type":"action","payload":{...}}}` (to cut RTT).
- **Fees**: Tier-0 Perp 0.045%/0.015%, Spot 0.070%/0.040%; use `userFees` to fetch user-specific discounts.
- **Funding**: 8h computed, paid **hourly** in 1/8th slices; optional adjustment can nudge thresholds.

These are backed by the references cited in the delivery message.
