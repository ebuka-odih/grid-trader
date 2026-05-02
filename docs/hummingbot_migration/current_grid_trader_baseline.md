# Current Grid-Trader Baseline Before Hummingbot Migration

This document freezes the known working custom `grid-trader` behavior before introducing the Hummingbot execution adapter. The existing runtime should keep running while this migration is implemented; do not stop or replace it until paper-mode parity is proven.

## Current dry-run policy caps

- `MAX_CONCURRENT_GRIDS=50` target capacity for portfolio scanner deployment.
- `MAX_TRADE_WALLET_EXPOSURE_PCT=2.0` per symbol/grid reserved-margin cap.
- `MAX_SAFE_LEVERAGE=10` desired live/cross-margin leverage ceiling.
- `MAX_DEPLOY_LEVERAGE=10` desired deployment leverage ceiling.
- `USE_LLM_BRAIN=False` deterministic scanner-ranked deployment is the stable default.

## Runtime target

- Known target: roughly 38–41 active grids with about 80% total reserved margin in dry-run.
- One active dense grid engine per symbol.
- Each symbol grid should internally use 10–20 levels, depending on wallet budget and market conditions.

## User trading policy

- One dense grid per token/symbol.
- Maximum 2% wallet exposure per trade/grid entry group.
- Maximum 10x leverage for cross-margin grid trading.
- Cross-margin portfolio view: all positions share one wallet, so correlation and total exposure matter.
- Do not close filled losing positions during normal lifecycle/stale cleanup while PnL is negative; hold until profitable, unless an emergency kill/risk condition overrides.

## Migration boundary

Keep `grid-trader` as:

- scanner/ranker
- portfolio brain
- risk-policy layer
- terminal/API state source
- learning/statistics layer

Move or wrap only the fragile execution layer behind an adapter:

- order/config generation
- exchange connector details
- precision/order lifecycle
- paper/live safety checks
- Hummingbot state ingestion

## First Hummingbot target

- Exchange: `hyperliquid_perpetual`
- Mode: paper only by default
- Live mode gate: refuse unless `HUMMINGBOT_ALLOW_LIVE=true` / adapter `allow_live=True`
