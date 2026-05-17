# Live Trading Runbook

This document is the procedure for taking grid-trader from `DRY_RUN=true`
to real-money trading on Bybit (extensible to any CCXT-supported venue).

> **Active live execution path:** `live_engine.LiveEngine` → CCXT (`ccxt.bybit`).
> The Hummingbot adapter under `execution_adapters/` is **legacy and not used**.

---

## Pre-flight — must be true before flipping `DRY_RUN=false`

- [ ] **Bybit API keys verified mainnet** — `BYBIT_API_KEY` / `BYBIT_API_SECRET`
      in `.env` correspond to the mainnet account you intend to fund.
      `TRADING_MODE=mainnet`.
- [ ] **API permissions** — the keys must have **Contract Trade R/W**.
      Withdrawals MUST be disabled. IP whitelist set to the server's IP.
- [ ] **Account funded** with a known balance (e.g. $50–100 to start).
      Mode: cross margin, USDT collateral.
- [ ] **Telegram alerts wired** — set `TELEGRAM_BOT_TOKEN` and
      `TELEGRAM_CHAT_ID` so flatten failures and scale-out events page you.
- [ ] **Live-mini caps applied** — see the LIVE-MINI PROFILE block in `.env`.
      Uncomment those lines and verify the *effective runtime* by importing
      `runtime_config` before `config` inside the container, because
      `/data/runtime_config.json` overrides `.env` at startup.
- [ ] **Backup current dry-run state** — `cp /var/lib/docker/volumes/.../multi_grid_trades.db /tmp/dryrun-trades.db.bak`
      (or via the Docker volume) so dry-run history isn't mixed with live.

Verification helper:

```sh
docker exec grid-trader python3 - <<'PY'
import runtime_config
import config
print({
    'DRY_RUN': config.DRY_RUN,
    'BASE_ORDER_SIZE_USDT': config.BASE_ORDER_SIZE_USDT,
    'MAX_CONCURRENT_GRIDS': config.MAX_CONCURRENT_GRIDS,
    'DEFAULT_LEVERAGE': config.DEFAULT_LEVERAGE,
    'MIN_SAFE_LEVERAGE': config.MIN_SAFE_LEVERAGE,
    'MAX_SAFE_LEVERAGE': config.MAX_SAFE_LEVERAGE,
    'MIN_DEPLOY_LEVERAGE': config.MIN_DEPLOY_LEVERAGE,
    'MAX_DEPLOY_LEVERAGE': config.MAX_DEPLOY_LEVERAGE,
    'MAX_SCANNER_LEVERAGE': config.MAX_SCANNER_LEVERAGE,
})
PY
```
- [ ] **Reset session runtime** for a clean start: `GRID_TRADER_RESET_RUNTIME=1` once.

## What changes when `DRY_RUN=false`

Code path differences vs the dry-run engine:

| Action | Dry-run | Live |
|---|---|---|
| Place grid limit orders | simulated in-memory | `exchange.create_limit_order` |
| Set leverage | no-op | `exchange.set_leverage` per symbol |
| Fill detection | price-cross simulator | WebSocket fill stream |
| Position sync | computed locally | `exchange.fetch_positions` every 10s |
| Cancel grid | clears in-memory levels | `exchange.cancel_order` per level |
| Scale-out (PARTIAL_CLOSE) | `perform_partial_close` in-memory | `close_position(reduceOnly=True)` market |
| Final close | sets `is_active=False` | **cancel all grid orders + market-close remaining position** + `is_active=False` |

The final close path (`_flatten_and_cancel`) is the most critical change. Any
exception there triggers a Telegram alert with `🚨 FLATTEN FAILED` — if you
see this, manually verify on Bybit that the position is closed.

## Live-mini profile (first 24 hours)

```
DRY_RUN=false
BASE_ORDER_SIZE_USDT=0.5
MAX_CONCURRENT_GRIDS=5
DEFAULT_LEVERAGE=30
MIN_SAFE_LEVERAGE=30
MAX_SAFE_LEVERAGE=50
MIN_DEPLOY_LEVERAGE=30
MAX_DEPLOY_LEVERAGE=50
MAX_SCANNER_LEVERAGE=50
HARD_FLOOR_MAX_PCT=25
TARGET_WALLET_EXPOSURE_PCT=20
```

Rationale: ~$10 max exposure across 5 small grids. Worst-case loss if all
five hit the 25% margin floor simultaneously ≈ $2.50. Real fills, real
slippage, real exchange-side rejections — at a stake size you can write
off as a coffee.

## Monitoring during live-mini

Watch the dashboard log panel and Telegram for:

- **🚀 Grid #N deployed** — confirm symbol/leverage/size match your caps.
- **🔴 LIVE FILL** — should appear when WS reports a fill.
- **🔴 LIVE FLATTEN: closed ...** — every grid close MUST log this with
  an order_id. If a final close has no FLATTEN line, **the position is
  still open on the exchange** — investigate immediately.
- **⚖️ LIVE SCALE-OUT** — first hard-floor breach. Should match a Bybit
  reduce-only market order.
- **🚨 FLATTEN FAILED / FLATTEN ERROR** — paged via Telegram. Stop the
  bot, manually flatten on Bybit, debug.

After the first hour, run:

```sh
docker exec grid-trader python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/data/multi_grid_trades.db')
rows = c.execute("""
  SELECT close_reason, COUNT(*) n, ROUND(SUM(total_pnl),4) sum_pnl
  FROM grid_cycles WHERE closed_at > datetime('now', '-1 hour')
  GROUP BY close_reason ORDER BY n DESC
""").fetchall()
for r in rows: print(r)
PY
```

The reason mix should be similar to dry-run (mostly `target_hit`, occasional
`drawdown` or `partial_close`). If you see disproportionate `timeout` or
strange new reasons, halt and diagnose.

## Pre/post-fix behavior snapshots

To preserve evidence of whether a change helped or hurt, capture a structured
snapshot *before* and *after* the update. This does **not** touch the DB or bot
runtime; it only reads the API, effective config, git state, and SQLite trade
history.

```sh
python3 scripts/behavior_snapshots.py capture \
  --label pre_fix \
  --notes "before leverage/risk tweak"

python3 scripts/behavior_snapshots.py capture \
  --label post_fix \
  --notes "30-50x leverage band live"
```

Snapshots are written to:

```text
analysis/behavior_snapshots/
```

To compare two saved snapshots:

```sh
python3 scripts/behavior_snapshots.py compare \
  analysis/behavior_snapshots/<before>.json \
  analysis/behavior_snapshots/<after>.json
```

Useful options:

- `--since 2026-05-07T00:00:00Z` — only include trades closed after a cutoff
- `--until 2026-05-07T12:00:00Z` — only include trades closed before a cutoff
- `--db /path/to/multi_grid_trades.db` — force a specific DB file

This gives you a durable pre/post record of:

- win rate
- total/average PnL
- best/worst trade
- average fills and duration
- close-reason mix
- top symbols
- leverage breakdown
- active-slot and wallet snapshot at capture time

## Scale-up plan

Each step requires the prior step to run **clean for ≥24 hours** with a
PnL trajectory consistent with dry-run.

Keep leverage inside the canonical **30–50x** runtime band and scale
primarily with order size and concurrency:

| Step | `BASE_ORDER_SIZE_USDT` | `MAX_CONCURRENT_GRIDS` | `DEFAULT_LEVERAGE` |
|---|---|---|---|
| 1 — live-mini | 0.5 | 5 | 30 |
| 2 — small | 1.0 | 10 | 30 |
| 3 — medium | 2.0 | 20 | 30 |
| 4 — full | 5.0 | 40 | 30 |

Only raise leverage above the default if the runtime profile explicitly
calls for it and the effective imported config verifies within the same
30–50x band.

Scale-down at any sign of trouble — never scale up after a bad day to
"average down".

## Emergency halt

```sh
ssh forge1@<host>
docker stop grid-trader            # immediately stops new orders
# Manually flatten any remaining positions on Bybit UI
docker exec -it grid-trader bash    # or restart in dry-run after fix
```

Then before restarting:
- Set `DRY_RUN=true` in `.env`
- Investigate logs: `docker logs grid-trader --since 1h | grep -E '🚨|FLATTEN|ERROR'`
- Only flip back to live after the issue is understood and fixed.

## Adding a new exchange (e.g. Binance)

1. In `grid_engine.py`, replace `ccxt.bybit({...})` with `ccxt.binance({...})`
   (or pick at runtime via env). The CCXT unified interface keeps every other
   call identical (`create_limit_order`, `cancel_order`, `set_leverage`,
   `fetch_positions`, `create_market_order`).
2. Verify the venue exposes `defaultType: linear` (or equivalent) for USDT
   perps and supports `reduceOnly` on market orders.
3. Re-run the live-mini rehearsal on the new venue before scaling.
