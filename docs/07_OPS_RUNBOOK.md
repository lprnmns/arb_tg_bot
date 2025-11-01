# Ops / Runbook

1. Fill `.env` (wallets, SMTP). Keep `DRY_RUN=true` first.
2. `docker compose up --build`
3. Verify UI shows live edges. You should receive emails when edge ≥ threshold.
4. When satisfied, set `DRY_RUN=false` to enable WS-POST execution and dead-man scheduling; adjust `DEADMAN_SECONDS` if you need a longer safety window.
5. Monitor `/scanner` and DB tables. Apply rate cap and adjust threshold if fills are scarce.
