# API provider investigation — archived

This file is a non-operational historical marker.

The original provider comparison was performed before the migration to
Binance Futures and has intentionally been removed because its commands,
dependency versions, provider recommendations, and deployment assumptions
are no longer valid.

Current source of truth:

- `README.md` — runtime behavior and configuration;
- `ANALYSIS_AND_DEPLOY.md` — Vultr Ubuntu 24.04 deployment and operations;
- `requirements.txt` — pinned dependency versions;
- `check_api.py` and `check_exchanges.py` — Binance-only connectivity checks.

The current application uses only public Binance USDT-M Futures endpoints.
