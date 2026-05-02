# Grid Trading System — Full Audit

## System Architecture (Data Flow)

```
┌─────────────┐    ┌──────────────┐    ┌─────────────────┐
│  coin_scanner │───▶│  algo_picker  │───▶│ decision_supervisor │
│  (scan market)│    │  (rank/score) │    │  (sanity check)     │
└─────────────┘    └──────────────┘    └─────────────────┘
                                                │
                                                ▼
┌─────────────┐    ┌──────────────┐    ┌─────────────────┐
│  price_bus   │───▶│ dry_run_engine│───▶│ multi_grid_manager │
│  (WebSocket) │    │  (simulate)   │    │  (orchestrator)     │
└─────────────┘    └──────────────┘    └─────────────────┘
                                                │
                    ┌──────────────┐            │
                    │ trading_agent │◀───────────┘
                    │  (LLM calls)  │
                    └──────────────┘
                                                │
                    ┌──────────────┐            │
                    │wallet_tracker │◀───────────┘
                    │  (balance)    │
                    └──────────────┘
                                                │
                    ┌──────────────┐            │
                    │improvement_loop│◀──────────┘
                    │  (journal/DB) │
                    └──────────────┘
                                                │
                    ┌──────────────┐            │
                    │   grid_api    │◀──────────┘
                    │  (dashboard)  │
                    └──────────────┘
```

## Module-by-Module Analysis

### 1. config.py (104 lines)
**Role:** Central configuration — all magic numbers in one place.
**Issues:**
- Duplicate `MAX_TOTAL_WALLET_EXPOSURE_PCT` (lines 23 and 68) — second overrides first
- Hardcoded defaults mixed with env vars — inconsistent
- No validation — bad values silently accepted

### 2. coin_scanner.py (265 lines)
**Role:** Scans Bybit for tradeable coins, scores them by grid suitability.
**Inputs:** Bybit API (klines, tickers)
**Outputs:** List of `CoinScore` with grid_score, range, ATR, mean_reversion
**Issues:**
- Learning system exists but feedback loop is weak — scores don't change much
- Sector classification is basic (hardcoded dict)
- No caching — re-fetches all data every scan cycle

### 3. decision_supervisor.py (137 lines)
**Role:** Fast sanity gate — validates LLM/algo decisions before deployment.
**Checks:** Grid width (max 8%), leverage bounds, duplicate symbols
**Issues:**
- Grid width check is too strict (8% max) — filters out good volatile coins
- No historical performance check (doesn't look at past wins/losses per symbol)

### 4. trading_agent.py (548 lines)
**Role:** LLM-powered decision layer via NVIDIA NIM.
**Decision types:** Pre-trade (pick coin), Mid-trade (adjust), Close (exit), Post-trade (learn)
**Issues:**
- **429 rate limits** — NVIDIA NIM throttles with many concurrent grids
- Mid-trade checks every 120s — mostly returns "hold" (low value)
- Post-trade learning is recorded but never actually used to change behavior
- LLM calls add 2-5s latency per decision
- Fallback model (8B) gives worse decisions than primary (70B)

### 5. grid_engine.py (330 lines)
**Role:** Calculates grid levels (prices, quantities, sides).
**Inputs:** Symbol, upper/lower bounds, num_grids, leverage, order_size
**Outputs:** `GridState` with `GridLevel` list
**Issues:**
- ATR-based spacing (100x multiplier) can create very wide grids
- No dynamic level adjustment based on liquidity
- Grid levels are static once created — no rebalancing

### 6. dry_run_engine.py (546 lines)
**Role:** Simulates grid trading — fill detection, PnL tracking, close conditions.
**Key logic:**
- Fill detection: price crosses level → simulate fill
- Target: 2-4% of allocated margin
- Drawdown: 5% of allocated margin → hold (don't close)
- Multi-cycle: 1-3 cycles before final close
**Issues:**
- Fill detection uses `old_price > level.price >= price` — misses fills when price jumps over multiple levels
- Drawdown hold means losing positions sit forever until they recover
- No time-weighted PnL — a $0.01 profit in 30min counts same as $0.01 in 1min

### 7. adaptive_grid.py (590 lines)
**Role:** Adaptive features — spike detection, recentering, trailing, exposure cap.
**Issues:**
- Recentering disabled (was hurting PnL)
- Trailing disabled (was causing one-sided fills)
- Only spike detection and exposure cap are actually active
- Lots of dead code from disabled features

### 8. live_engine.py (460 lines)
**Role:** Real Bybit order placement for live trading.
**Issues:**
- Never actually used in current setup (DRY_RUN=true)
- Import errors fixed but untested in production
- No order cancellation logic for grid adjustments

### 9. portfolio_risk_monitor.py (254 lines)
**Role:** Risk gates — exposure limits, blacklist, emergency checks.
**Recent fix:** Now uses fills + 3 buffer (not all levels) for exposure calc.
**Issues:**
- Emergency check runs every 30s but only logs, doesn't auto-close
- Correlation groups defined but never checked during deployment
- No dynamic position sizing based on recent win rate

### 10. heartbeat_regulator.py (200 lines)
**Role:** Health checks — stale detection, deployment pausing.
**Recent fix:** 300s threshold, 10s pause, needs 2+ stale to pause.
**Issues:**
- Still force-closes stale grids after 10min — could be too aggressive
- No graduated response (warning → pause → close)

### 11. price_bus.py (259 lines)
**Role:** Single WebSocket connection, fans out price ticks to all grids.
**Issues:**
- Single connection — if it drops, ALL grids lose data
- No price validation (could receive garbage data)
- Queue size of 1 — old ticks dropped silently

### 12. wallet_tracker.py (246 lines)
**Role:** Tracks wallet balance, positions, exposure.
**Issues:**
- Balance updates only on close — not real-time
- Exposure % is calculated from positions, not actual margin used
- No fee tracking (Bybit charges fees on fills)

### 13. improvement_loop.py (526 lines)
**Role:** Records fills, cycles, learning to SQLite DB.
**Issues:**
- Learning is recorded but never consumed by the trading agent
- No analytics — just raw data storage
- DB schema missing useful columns (fee, slippage, market_regime)

### 14. multi_grid_manager.py (2558 lines)
**Role:** Main orchestrator — deployment, monitoring, closing.
**Issues:**
- **2558 lines** — too large, hard to maintain
- Deployment cycle picks max 5 coins per cycle — could be faster
- `_select_coins_algorithmically()` is decent but doesn't use learning data
- No priority queue — all coins treated equally
- Monitoring loop runs per-grid (good) but LLM checks are sequential (bad)

### 15. grid_api.py (848 lines)
**Role:** REST API for dashboard.
**Issues:**
- No WebSocket for real-time updates (polling only)
- Some endpoints return stale data (cached state)
- No authentication

## Critical Issues (Ranked by Impact)

### 1. 🔴 LLM Rate Limits (429 errors)
**Impact:** Mid-trade checks fail, fallback to 8B model (worse decisions)
**Cause:** Each grid makes 1-2 LLM calls per 120s. With 15+ grids = 15+ calls/120s
**Fix:** Batch LLM calls (one call for all grids), increase interval, or use local model

### 2. 🔴 Fill Detection Gaps
**Impact:** Missed fills when price jumps over multiple levels
**Cause:** Checks `old_price > level.price >= price` — only catches single-level crosses
**Fix:** Check ALL levels between old_price and new_price on each tick

### 3. 🟡 Learning Feedback Loop Broken
**Impact:** System doesn't learn from past trades
**Cause:** `improvement_loop` records data but `trading_agent` never reads it
**Fix:** Feed recent win rate, avg duration, best/worst symbols into LLM prompts

### 4. 🟡 No Fee Tracking
**Impact:** PnL calculations are off by 0.05-0.1% per fill
**Cause:** Fees not recorded in DB or factored into PnL
**Fix:** Add fee field to fills, subtract from realized PnL

### 5. 🟡 Drawdown Hold = Infinite Bag Holding
**Impact:** Losing positions sit forever, blocking slots
**Cause:** Drawdown triggers "hold until positive" — no time limit
**Fix:** Add max hold time (e.g., 2 hours), then force-close at smaller loss

### 6. 🟡 Dead Code Bloat
**Impact:** Hard to understand what's actually running
**Cause:** adaptive_grid features (recenter, trail, exp_sizing) all disabled
**Fix:** Remove or clearly mark disabled features

### 7. 🟢 No Price Validation
**Impact:** Could act on bad data (exchange glitch, WebSocket garbage)
**Cause:** price_bus accepts any price without sanity check
**Fix:** Reject prices that move >5% in 1 second

### 8. 🟢 Single WebSocket Connection
**Impact:** All grids fail if connection drops
**Cause:** One price_bus connection for everything
**Fix:** Add fallback connection or REST polling backup

## Improvement Priorities

| Priority | Issue | Effort | Impact |
|----------|-------|--------|--------|
| 1 | Fix fill detection gaps | Low | High |
| 2 | Batch LLM calls / reduce frequency | Medium | High |
| 3 | Wire learning into agent prompts | Medium | High |
| 4 | Add fee tracking | Low | Medium |
| 5 | Add drawdown time limit | Low | Medium |
| 6 | Remove dead code | Low | Low |
| 7 | Add price validation | Low | Low |
| 8 | Dual WebSocket connection | Medium | Low |

## What's Actually Working Well

- ✅ **Grid deployment pipeline** — scan → score → supervise → risk check → deploy
- ✅ **Exposure management** — now uses fills + buffer, not full grid capacity
- ✅ **Price bus** — single WebSocket, fan-out to all grids
- ✅ **Stagnation detection** — closes grids that aren't making progress
- ✅ **Multi-cycle support** — grids can run 1-3 cycles before closing
- ✅ **Wallet tracking** — balance updated on each close
- ✅ **DB recording** — fills and cycles now properly saved
- ✅ **API dashboard** — live stats, active grids, closed trades

## Key Metrics (Current Session)

- **Total cycles:** 15
- **Total fills:** 30
- **Win rate:** ~85%
- **Total PnL:** ~$7.18
- **Active grids:** 4-13 (varies)
- **Avg cycle duration:** 3-6 minutes
- **Avg fills per cycle:** 2-3
