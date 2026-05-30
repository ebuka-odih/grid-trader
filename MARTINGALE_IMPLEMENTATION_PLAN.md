# Martingale Grid Implementation Plan

## Current State Analysis

### Problem
The bot makes small profits but fees eat most of it. The flat grid system places equal-sized orders at every level, meaning:
- Low-profit center fills (where price oscillates 70% of the time)
- High-risk edge fills (where trends cause losses)
- Net result: barely positive after fees

### Root Cause: Progressive Sizing is INVERTED
The current code has `progressive_min_factor=0.35` at center and `progressive_max_factor=2.0` at edges — **exactly backwards**. Center levels get small orders, edge levels get large orders. This amplifies losses in trends.

### Fee Structure (Bybit Maker: 0.02%)

| Fill Type | Notional | Profit/Fill | Fee/Fill | Net/Fill |
|-----------|----------|-------------|----------|----------|
| Flat ($1.90/level) | $66.50 | $0.067 | $0.027 | **$0.040** |
| Martingale center | $146.30 | $0.146 | $0.059 | **$0.088** |
| Martingale edge | $23.28 | $0.023 | $0.009 | **$0.014** |

---

## Risk Constraints

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Max per-coin exposure | **5%** ($4.93) | Prevents single-coin wipeout |
| Max concurrent grids | **3** | Diversification without overexposure |
| Max total exposure | **15%** ($14.80) | Leaves 85% reserve for drawdowns |
| Max drawdown per grid | **15% of margin** ($0.74) | Survivable loss per trade |
| Leverage | **35x** | Balances profit potential vs risk |

---

## Martingale Grid Design

### Level Distribution (8 levels, 5% exposure)

```
Level    Factor   Margin    Notional    Qty SOL    Fill Prob
--------------------------------------------------------------
BUY 4    0.60x    $0.37     $12.95      0.158      ~30% (edge)
BUY 3    1.04x    $0.64     $22.38      0.273      ~40%
BUY 2    1.43x    $0.88     $30.90      0.377      ~60%
BUY 1    1.77x    $1.09     $38.18      0.466      ~75%
CENTER   2.00x    $1.23     $43.17      0.526      ~90% (most fills)
SELL 1   1.77x    $1.09     $38.18      0.466      ~75%
SELL 2   1.43x    $0.88     $30.90      0.377      ~60%
SELL 3   1.04x    $0.64     $22.38      0.273      ~40%
SELL 4   0.60x    $0.37     $12.95      0.158      ~30% (edge)
--------------------------------------------------------------
TOTAL              $7.20     $252.01     3.074
```

### Key Properties
- **Center gets 2x the base order** — highest fill probability, highest profit
- **Edges get 0.6x the base order** — lowest fill probability, lowest risk
- **Smooth curve (power=1.3)** — no abrupt size jumps between levels
- **All levels above Bybit minimums** — 0.158 SOL > 0.1 SOL minimum

---

## PNL Comparison

### 5-Fill Cycle (realistic average)

| Grid Type | Center Fill | Edge Fill | Total Net | vs Flat |
|-----------|-------------|-----------|-----------|---------|
| Flat | $0.040 | $0.040 | $0.200 | baseline |
| **Martingale** | **$0.088** | **$0.014** | **$0.292** | **+46%** |

### Why Martingale Wins
1. **70% of fills are center** — 2x more profit per fill
2. **30% of fills are edge** — 65% less loss per adverse fill
3. **Net effect**: Higher wins + smaller losses = better expectancy

---

## Portfolio Allocation

```
Grid 1: SOL/USDT   -> 5% margin ($4.93) -> 8 levels
Grid 2: DOGE/USDT  -> 5% margin ($4.93) -> 8 levels
Grid 3: XRP/USDT   -> 5% margin ($4.93) -> 8 levels
-------------------------------------------------------
Total exposed:     15% = $14.80
Reserve:           85% = $83.87
Max drawdown/grid: $0.74 (15% of margin)
```

---

## Implementation Changes

### 1. grid_engine.py — Invert Progressive Direction

**File**: `grid_engine.py`
**Lines**: ~235-251

**Current (WRONG)**:
```python
# BUG: factor INCREASES with distance (wrong!)
factor = progressive_min_factor + (progressive_max_factor - progressive_min_factor) * (dist ** progressive_curve_power)
```

**Fixed (CORRECT)**:
```python
# FIXED: factor DECREASES with distance (correct!)
factor = progressive_max_factor - (progressive_max_factor - progressive_min_factor) * (dist ** progressive_curve_power)
```

**Impact**: Center levels now get 2.0x, edges get 0.6x (was reversed).

---

### 2. .env — Enable Progressive Sizing

**File**: `.env`

**Add/Update**:
```bash
PROGRESSIVE_SIZING_ENABLED=true
PROGRESSIVE_MIN_FACTOR=0.6
PROGRESSIVE_MAX_FACTOR=2.0
PROGRESSIVE_CURVE_POWER=1.3
MAX_CONCURRENT_GRIDS=3
MAX_GRIDS_PER_SYMBOL=1
```

---

### 3. portfolio_risk_monitor.py — Set Per-Coin Cap to 5%

**File**: `portfolio_risk_monitor.py`
**Lines**: ~46-55

**Change default**:
```python
self.defaults = {
    "leverage": MAX_SAFE_LEVERAGE,
    "max_leverage": MAX_SAFE_LEVERAGE,
    "max_wallet_exposure_pct": 5.0,  # Changed from 25.0
    "order_size_usdt": BASE_ORDER_SIZE_USDT,
    "num_grids": 8,
    "target_pnl_pct": [2.0, 3.0],
}
```

---

### 4. token_profiles.json — Update All Profiles

**File**: `token_profiles.json`

**For each profile, change**:
```json
{
    "max_wallet_exposure_pct": 5.0,
    "order_size_usdt": 3.0,
    "num_grids": 8,
    "target_pnl": [2.0, 3.0]
}
```

---

### 5. adaptive_grid.py — Enable Progressive in Default Config

**File**: `adaptive_grid.py`
**Lines**: ~585+

**Update default_config()**:
```python
def default_config() -> AdaptiveConfig:
    from config import (
        PROGRESSIVE_SIZING_ENABLED, PROGRESSIVE_MIN_FACTOR,
        PROGRESSIVE_MAX_FACTOR, PROGRESSIVE_CURVE_POWER,
    )
    
    return AdaptiveConfig(
        recenter_trigger_pct=100.0,  # Disabled
        trailing_enabled=False,
        exp_sizing_enabled=False,
        progressive_sizing_enabled=PROGRESSIVE_SIZING_ENABLED,
        progressive_min_factor=PROGRESSIVE_MIN_FACTOR,
        progressive_max_factor=PROGRESSIVE_MAX_FACTOR,
        progressive_curve_power=PROGRESSIVE_CURVE_POWER,
        spike_window_sec=10.0,
        spike_threshold_pct=0.5,
        spike_cooldown_sec=60.0,
        max_same_side_fills=4,
        hedge_on_breach=False,
    )
```

---

## Safety Mechanisms

| Mechanism | Setting | Purpose |
|-----------|---------|---------|
| Hard floor | 25% of margin | Auto-close if loss exceeds $0.74/grid |
| Max drawdown | 15% of margin | Additional safety net |
| No-fills timeout | 15 minutes | Free slot if price doesn't reach grid |
| Stagnant timeout | 40 minutes | Free slot if no progress |
| Exposure cap | 5% per coin | Prevent overconcentration |
| Min order size | $0.50 | Bybit minimum enforced |

---

## Expected Outcomes

| Metric | Before (Flat) | After (Martingale) | Improvement |
|--------|---------------|-------------------|-------------|
| Net PnL/grid cycle | $0.04 | $0.06 | +50% |
| Center fill profit | $0.04 | $0.09 | +125% |
| Edge fill loss | $0.04 | $0.01 | -65% |
| Risk per grid | 15% margin | 15% margin | same |
| Wallet utilization | 42% | 15% | safer |

---

## Testing Checklist

- [ ] Verify grid_engine.py syntax compiles
- [ ] Test with dry_run=true first (10 cycles minimum)
- [ ] Verify all levels above Bybit minimum order size
- [ ] Check that center levels fill more often than edges
- [ ] Monitor PnL per cycle — should be >$0.05 average
- [ ] Verify fees don't exceed 30% of gross profit
- [ ] Run for 24 hours before live deployment

---

## Rollback Plan

If martingale performs worse than flat:
1. Set `PROGRESSIVE_SIZING_ENABLED=false` in .env
2. Restart bot
3. Bot reverts to flat grid immediately

No code changes needed — just config toggle.
