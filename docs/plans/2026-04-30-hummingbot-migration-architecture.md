# Hummingbot Migration Architecture Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Rebuild the custom `grid-trader` multi-grid system on top of a Hummingbot-style execution framework so we keep the profitable scanner/risk/UI behavior while replacing fragile custom order/exchange lifecycle code with stable exchange connector primitives.

**Architecture:** Keep `grid-trader` as the portfolio brain, scanner, risk monitor, terminal UI, and funded-challenge policy layer. Replace or wrap the custom `GridEngine`/exchange execution layer with a Hummingbot execution adapter that can run in paper mode first, then Hyperliquid/Bybit/Binance perpetual live mode. Use one dense grid engine per token, max 10x leverage, 2% wallet exposure per token, cross-margin portfolio risk.

**Tech Stack:** Python 3.11, Hummingbot Docker (`hummingbot/hummingbot:latest`), Hummingbot connectors (`hyperliquid_perpetual`, `bybit_perpetual`, `binance_perpetual`), existing `grid-trader` scanner/risk/API/frontend, YAML configs, JSON signal bridge, pytest/unittest.

---

## Current System Findings

### Existing custom `grid-trader`

Root: `/home/forge1/.hermes/projects/grid-trader`

Important files:
- `multi_grid_manager.py` — current concurrent slot manager, scanner deploy loop, deterministic scanner fallback, state writer.
- `grid_engine.py` — custom Bybit order/grid calculation engine using ccxt.
- `dry_run_engine.py` — simulated fill engine.
- `coin_scanner.py` — Bybit scanner and grid candidate scoring.
- `portfolio_risk_monitor.py` — exposure cap, token profile, emergency risk checks.
- `wallet_tracker.py` — wallet/exposure reporting.
- `grid_api.py` — terminal API/state serving.
- `/tmp/grid_trader_state.json` — runtime dashboard state.

Recent runtime changes already applied:
- Active grid capacity now uses `MAX_CONCURRENT_GRIDS=50`.
- Deterministic scanner-ranked deployment is default (`USE_LLM_BRAIN=False`).
- Per-grid trade exposure cap is `2%`.
- Leverage cap is now `10x`.
- Live dry-run reached ~38–41 active grids.

Current weaknesses:
- Custom lifecycle code is large and brittle: manager, heartbeat, price bus, dry/live order logic, state writing, risk gates, cleanup all interact tightly.
- Custom exchange execution is Bybit-specific and uses direct ccxt calls.
- Order precision/position lifecycle behavior must be maintained manually.
- Stabilization risk grows as we move from dry-run to real money/cross-margin.

### Existing real Hummingbot setup

Root: `/home/forge1/.hummingbot`

Important files:
- `conf/conf_client.yml` — paper mode enabled.
- `conf/conf_perpetual_market_making_1.yml` — current Hyperliquid PMM config.
- `conf/conf_connector_hyperliquid_perpetual.yml` — Hyperliquid connector config.
- `scripts/hyperliquid_hummingbot_scanner.py` — scanner for Hyperliquid swap markets.
- `scripts/hummingbot_signal_bridge.py` — scanner → signals/config bridge.
- `data/signals.json` — latest scanner output.
- Docker container: `hummingbot-paper`, image `hummingbot/hummingbot:latest`, currently running.

Hummingbot container capabilities verified:
- Strategies include `perpetual_market_making`.
- Derivative connectors include:
  - `hyperliquid_perpetual`
  - `bybit_perpetual`
  - `binance_perpetual`
  - plus many others.

---

## Strategic Comparison

| Area | Custom `grid-trader` | Hummingbot Framework |
|---|---|---|
| Scanner | Strong; already finds many candidates and has learning | We can reuse our scanner or Hummingbot scanner scripts |
| Portfolio/risk | Strong; 2% per token, 10x cap, cross-margin concept | Needs custom outer risk layer; Hummingbot is strategy/execution-focused |
| Execution stability | Fragile; custom order lifecycle and precision | Stronger; battle-tested connectors/order tracking |
| Multi-token grids | Already works in dry-run, 38–41 active | Need orchestration: multiple configs/instances or custom script strategy |
| UI/terminal | Existing dashboard is tailored to user preference | Hummingbot CLI is not enough; keep our terminal/API |
| Exchange support | Currently Bybit-focused | Hyperliquid, Bybit, Binance perpetual native connectors |
| Live trading risk | Higher if custom engine has hidden bugs | Lower if Hummingbot handles exchange details, but integration still needs testing |

## Recommendation

Do **not** throw away `grid-trader`. Use it as the portfolio brain and terminal. Replace the fragile execution layer with a Hummingbot adapter in stages.

Best path:
1. **Phase 1:** Paper-mode Hummingbot adapter with Hyperliquid first.
2. **Phase 2:** Run Hummingbot-backed grids in parallel with current dry-run grid-trader, compare fills/PnL/state.
3. **Phase 3:** Move the terminal to display Hummingbot-backed grids.
4. **Phase 4:** Add Bybit/Binance connectors once the adapter interface is stable.
5. **Phase 5:** Only then consider live cross-margin.

Preferred exchange order:
1. **Hyperliquid** — simpler USDC perpetual universe, strong connector, good for portfolio-level perps.
2. **Bybit** — closest to current grid-trader scanner and symbol universe.
3. **Binance** — deep liquidity but heavier account/compliance/API constraints.

---

## Target Architecture

```text
                         ┌──────────────────────────────┐
                         │ React Grid Terminal           │
                         │ /api/state, slots, grid levels│
                         └──────────────┬───────────────┘
                                        │
                         ┌──────────────▼───────────────┐
                         │ grid_api.py / State Adapter   │
                         │ normalizes bot state          │
                         └──────────────┬───────────────┘
                                        │
┌──────────────────────┐  candidates   ┌▼────────────────────────────┐
│ coin_scanner.py       ├──────────────►│ Portfolio Orchestrator       │
│ or HL scanner         │               │ - 2% per token               │
└──────────────────────┘               │ - max 10x                    │
                                       │ - one dense grid per symbol   │
┌──────────────────────┐               │ - exposure + correlation     │
│ scanner_learning.py   ├──────────────►│ - no negative close policy    │
└──────────────────────┘               └──────────────┬──────────────┘
                                                       │ deploy/stop/status
                                       ┌───────────────▼───────────────┐
                                       │ Execution Adapter Interface    │
                                       │ GridExecutionAdapter           │
                                       └───────────────┬───────────────┘
                                                       │
                          ┌────────────────────────────▼────────────────────────────┐
                          │ HummingbotExecutionAdapter                              │
                          │ - writes YAML/scripts/signals                           │
                          │ - starts/stops Hummingbot worker(s)                     │
                          │ - reads orders/positions/trades                         │
                          │ - maps HB state to GridSlot-compatible state            │
                          └────────────────────────────┬────────────────────────────┘
                                                       │
                                       ┌───────────────▼───────────────┐
                                       │ Hummingbot Docker/Connector    │
                                       │ hyperliquid/bybit/binance perp │
                                       └───────────────────────────────┘
```

---

# Implementation Tasks

## Phase 0 — Safety Baseline

### Task 0.1: Freeze current custom grid-trader behavior in docs

**Objective:** Document the current working dry-run state before migration.

**Files:**
- Create: `docs/hummingbot_migration/current_grid_trader_baseline.md`

**Steps:**
1. Record current env caps:
   - `MAX_CONCURRENT_GRIDS=50`
   - `MAX_TRADE_WALLET_EXPOSURE_PCT=2.0`
   - `MAX_SAFE_LEVERAGE=10`
   - `MAX_DEPLOY_LEVERAGE=10`
   - `USE_LLM_BRAIN=False`
2. Record known runtime target: 38–41 active grids at ~80% total reserved margin.
3. Record user policy:
   - one dense grid per token
   - never close filled losing positions while PnL is negative
   - 2% max exposure per token
   - max 10x leverage
   - cross-margin portfolio view

**Verification:** Plan doc exists and can be read.

---

### Task 0.2: Add execution adapter interface

**Objective:** Decouple `multi_grid_manager.py` from the custom Bybit `GridEngine`.

**Files:**
- Create: `execution_adapters/base.py`
- Test: `tests/test_execution_adapter_contract.py`

**Implementation sketch:**

```python
from dataclasses import dataclass
from typing import Protocol, Optional

@dataclass
class GridDeployRequest:
    symbol: str
    lower: float
    upper: float
    num_grids: int
    leverage: int
    margin_per_level_usdt: float
    direction: str = "neutral"
    exchange: str = "hyperliquid_perpetual"
    cross_margin: bool = True

@dataclass
class GridExecutionState:
    grid_id: str
    symbol: str
    active: bool
    current_price: float = 0.0
    total_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fills: int = 0
    position_qty: float = 0.0
    avg_entry_price: float = 0.0
    leverage: int = 1
    grid_levels: list[dict] = None

class GridExecutionAdapter(Protocol):
    async def deploy_grid(self, request: GridDeployRequest) -> GridExecutionState: ...
    async def get_status(self, grid_id: str) -> GridExecutionState: ...
    async def stop_grid(self, grid_id: str, reason: str = "manual") -> GridExecutionState: ...
    async def close(self) -> None: ...
```

**Verification:** Unit test asserts a fake adapter can be used by the contract.

---

## Phase 1 — Real Hummingbot Paper Adapter

### Task 1.1: Create Hummingbot config generator

**Objective:** Generate Hummingbot YAML configs from our grid deploy requests.

**Files:**
- Create: `execution_adapters/hummingbot_config.py`
- Test: `tests/test_hummingbot_config_generator.py`

**Behavior:**
- Convert symbols:
  - Hyperliquid: `AAVE/USDC:USDC` → `AAVE-USDC:USDC` or connector-required format.
  - Bybit: `DOGE/USDT:USDT` → Bybit perpetual format expected by Hummingbot.
- Enforce leverage cap: `min(request.leverage, 10)`.
- Convert grid bounds into PMM spreads or script parameters.
- Write config under isolated path, not directly overwrite the existing one until tested.

**Verification:** YAML contains exchange, market, leverage <= 10, grid levels, order amount.

---

### Task 1.2: Implement paper-mode Hummingbot adapter skeleton

**Objective:** Deploy a grid request into Hummingbot paper mode without touching live funds.

**Files:**
- Create: `execution_adapters/hummingbot_adapter.py`
- Test: `tests/test_hummingbot_adapter_paper.py`

**Behavior:**
- Use `~/.hummingbot/conf/` and `~/.hummingbot/data/` paths.
- Verify `paper_trade_enabled: true` before deployment.
- Write signal/config for a requested grid.
- Return a `GridExecutionState` with `active=True` after config/write success.

**Safety check:** If paper mode is false, adapter refuses unless explicitly initialized with `allow_live=True`.

---

### Task 1.3: Build Hummingbot state reader

**Objective:** Read Hummingbot paper trades/config/signals and map them into our dashboard format.

**Files:**
- Create: `execution_adapters/hummingbot_state_reader.py`
- Test: `tests/test_hummingbot_state_reader.py`

**Inputs:**
- `~/.hummingbot/data/signals.json`
- Hummingbot logs/data files
- Later: Hummingbot Gateway/API or SQLite DB if needed

**Output:** `GridExecutionState` compatible with the terminal.

**Verification:** Test with fixture `signals.json` and fake paper trades.

---

## Phase 2 — Multi-Token Hummingbot Orchestration

### Task 2.1: Decide worker model: multi-container vs single script strategy

**Objective:** Choose the stable Hummingbot deployment topology.

**Options:**

A. **Multiple Hummingbot containers/configs**
- One Hummingbot worker per active token/grid.
- Pros: isolation, simple lifecycle, failures isolated.
- Cons: heavier resource use for 40+ grids.

B. **One Hummingbot script strategy controlling many markets**
- Custom Hummingbot script strategy receives candidates and manages orders across many markets.
- Pros: closer to our current portfolio engine; one process.
- Cons: requires more Hummingbot internal strategy coding.

**Recommendation:** Start with A for paper-mode proof, then move to B if resource overhead is too high.

**Deliverable:** Create `docs/hummingbot_migration/worker_model_decision.md`.

---

### Task 2.2: Implement `HummingbotPortfolioAdapter`

**Objective:** Manage many Hummingbot-backed grids from our scanner.

**Files:**
- Create: `execution_adapters/hummingbot_portfolio_adapter.py`
- Test: `tests/test_hummingbot_portfolio_adapter.py`

**Behavior:**
- Accept list of `GridDeployRequest`.
- Apply portfolio rules before deploy:
  - max 50 grids
  - max 2% reserved margin per token
  - max 80% total exposure
  - max 10x leverage
  - one grid per symbol
- Start/update Hummingbot worker configs.
- Return list of `GridExecutionState`.

---

## Phase 3 — Integrate with `multi_grid_manager.py`

### Task 3.1: Add adapter selection config

**Objective:** Let the system switch between `dry_run`, `custom_bybit`, and `hummingbot` execution without changing scanner/risk code.

**Files:**
- Modify: `config.py`
- Modify: `.env`
- Test: `tests/test_execution_mode_config.py`

**Config:**

```env
EXECUTION_BACKEND=hummingbot_paper
HUMMINGBOT_EXCHANGE=hyperliquid_perpetual
HUMMINGBOT_HOME=/home/forge1/.hummingbot
HUMMINGBOT_ALLOW_LIVE=false
```

---

### Task 3.2: Wire adapter into deployment path

**Objective:** Replace direct `DryRunEngine()` / `GridEngine()` construction with selected adapter.

**Files:**
- Modify: `multi_grid_manager.py`
- Test: `tests/test_multi_grid_hummingbot_adapter.py`

**Behavior:**
- Scanner still produces candidates.
- Risk monitor still approves/rejects.
- Manager creates `GridDeployRequest`.
- Adapter deploys.
- Slot stores adapter state.

**Important:** Keep existing dry-run backend working as fallback.

---

### Task 3.3: Preserve “do not close losing filled positions” policy

**Objective:** Ensure adapter close logic refuses to close negative filled positions unless emergency kill switch triggers.

**Files:**
- Modify: `trade_close_optimizer.py` or adapter close policy
- Test: `tests/test_hummingbot_no_negative_close.py`

**Expected:** If `fills > 0` and `total_pnl < 0`, normal stale/timeout close returns hold.

---

## Phase 4 — Terminal/UI Integration

### Task 4.1: Normalize Hummingbot state to existing `/api/state`

**Objective:** Make Hummingbot-backed grids appear exactly like current active grids.

**Files:**
- Modify: `grid_api.py`
- Modify: state writer path in `multi_grid_manager.py`
- Test: `tests/test_grid_api_hummingbot_state.py`

**Fields required:**
- symbol
- direction
- pnl
- fills
- leverage
- order size
- grid levels with buy/sell price points and filled/open status
- runtime
- close reason

---

### Task 4.2: Add backend exchange/backend label to UI

**Objective:** UI should show whether a grid is `dry_run`, `custom_bybit`, or `hummingbot/hyperliquid`.

**Files:**
- Frontend state adapter file if present
- React grid detail/sidebar components

**Verification:** Selected grid displays backend + exchange.

---

## Phase 5 — Exchange Choice and Live Readiness

### Task 5.1: Hyperliquid paper deployment test

**Objective:** Prove the Hummingbot adapter can deploy and monitor at least 3 paper-mode Hyperliquid grids.

**Command:**

```bash
cd /home/forge1/.hermes/projects/grid-trader
source venv/bin/activate
EXECUTION_BACKEND=hummingbot_paper HUMMINGBOT_EXCHANGE=hyperliquid_perpetual python3 multi_grid_manager.py
```

**Verification:**
- At least 3 active grids.
- All leverage <= 10x.
- Paper mode true.
- Dashboard shows grid levels.

---

### Task 5.2: Bybit connector feasibility test

**Objective:** Confirm Hummingbot Bybit perpetual connector supports the exact symbol set and account mode we need.

**Files:**
- Create: `scripts/check_hummingbot_bybit_connector.py`

**Checks:**
- Connector exists in container.
- Can load Bybit perpetual markets.
- Symbol format maps from `DOGE/USDT:USDT`.
- Supports leverage/cross margin in required mode.

---

### Task 5.3: Binance connector feasibility test

**Objective:** Confirm Binance perpetual connector is viable, but do not prioritize it unless Hyperliquid/Bybit are unsuitable.

**Reason:** Binance has strong liquidity but usually more account/compliance and API constraints.

---

## Final Acceptance Criteria

The migration is successful when:

1. Scanner deploys 30–40+ concurrent Hummingbot-backed paper grids.
2. Every grid respects:
   - max 10x leverage
   - max 2% wallet exposure per token
   - max 80% total exposure
   - one grid per symbol
3. Terminal shows detailed buy/sell grid levels per selected token.
4. The system survives restart without stale state confusion.
5. No normal close path closes a filled negative-PnL position.
6. Hummingbot paper logs and our dashboard state agree on symbols/orders/fills.
7. Live mode is blocked by default until an explicit `HUMMINGBOT_ALLOW_LIVE=true` flag is set.

---

## Important Implementation Notes

- Hummingbot’s built-in `perpetual_market_making` is not a perfect 1:1 clone of our custom grid. It can approximate grid behavior using spreads/levels, but for exact dense-grid behavior we may need a Hummingbot script strategy or a custom adapter that places grid orders through Hummingbot connectors.
- The existing `~/.hummingbot/scripts/hummingbot_signal_bridge.py` only updates one PMM config. It is useful as a proof of concept but not enough for 40 simultaneous grids.
- The safest production design is: our portfolio orchestrator + Hummingbot connector/executor, not raw Hummingbot CLI alone.
- Keep the current `grid-trader` running in dry-run during migration. Do not switch live funds until paper-mode parity is proven.
- Hyperliquid should be the first implementation target because the existing Hummingbot setup and scanner already use it.
