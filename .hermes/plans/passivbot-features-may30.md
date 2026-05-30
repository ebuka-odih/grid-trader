# Passivbot Feature Integration Plan

> **Goal:** Integrate passivbot's 4 core profit-preserving features into grid-trader.

**Architecture:** Features build on existing infrastructure — progressive sizing (grid_engine.py), deployment cycle (multi_grid_manager.py), SmartCloseEngine (grid_core.py), and live_engine.py.

**Order of implementation:** Fix progressive → DCA/double-down → Trailing TP → Unstuck

---

## Phase 1: Fix Progressive Sizing Direction

**Root cause:** Current formula INCREASES factor with distance from price (center=0.35×, edges=2.0×). It's backwards. Center fills happen 70% of the time and should be BIGGEST; edge fills happen 30% and should be smallest.

**Files:**
- Modify: `grid_engine.py:245` — one-line formula flip

```
# Current (WRONG): edges get bigger, center gets smaller
factor = progressive_min_factor + (progressive_max_factor - progressive_min_factor) * (dist ** progressive_curve_power)

# Fixed (CORRECT): center gets bigger, edges get smaller
factor = progressive_max_factor - (progressive_max_factor - progressive_min_factor) * (dist ** progressive_curve_power)
```

**Expected:** Center levels 2.0×, farthest edges 0.35× (reversed from current).

---

## Phase 2: DCA/Double-Down on Losing Positions

**Core idea from passivbot:** When a position fills and moves against you, DON'T just wait for other grid levels to fill — deploy a NEW, DOUBLED-SIZE follow-up grid at a wider spacing to pull the average entry price closer to current price.

**How passivbot does it:**
- `entry_grid_double_down_factor`: multiplier for each successive re-entry (>1.0 = DCA)
- `n_positions`: how many re-entries allowed per symbol (typically 3-7)
- `entry_grid_spacing_pct`: spacing between re-entry grids (% of price)
- Each re-entry recalculates ALL closing orders at the new average entry price

### Our simpler version (Python, no Rust orchestrator):

When a grid starts filling and position goes underwater:
1. Detect losing position → `_check_double_down_opportunity()`
2. Calculate doubled size: `new_qty = last_fill_qty * double_down_factor`
3. Deploy new grid BELOW (long) or ABOVE (short) the current price
4. Merge old and new fills into one average entry price
5. Recalculate TP at the new average → small bounce = overall profit

**Files:**
- Modify: `multi_grid_manager.py` — add `_check_double_down_opportunity()` after `_deployment_cycle()`
- Modify: `grid_core.py` — add `calculate_avg_entry()` merger function
- Modify: `config.py` — add env vars
- Modify: `token_profiles.json` — add per-token double-down config

**New env vars:**
```
DOUBLE_DOWN_ENABLED=true
DOUBLE_DOWN_FACTOR=1.8         # 1.8x size per re-entry (passivbot default: ~2.0)
DOUBLE_DOWN_MAX_ENTRIES=3      # max re-entries per symbol (passivbot: n_positions)
DOUBLE_DOWN_SPACING_PCT=1.2    # spacing between re-entries as % of price
DOUBLE_DOWN_MIN_LOSS_PCT=1.5   # only trigger if loss > this %
DOUBLE_DOWN_MAX_LOSS_PCT=8.0   # stop doubling beyond this loss %
```

---

## Phase 3: Trailing Take-Profit (Profit Lock-In)

**Core idea:** When position goes into profit, instead of waiting for fixed TP%, trail the close behind the peak. If price retraces X% from the peak, close immediately. Prevents round-trips.

**NOTE:** Grid-trader already has `trailing_stop_enabled` in SmartCloseConfig, but it's for **LOSS positions** (trailing stop-loss tightening over time). This feature is the OPPOSITE — trailing profit lock for WINNING positions.

**Passivbot's model:**
- `close_trailing_threshold_pct`: price must move THIS far into profit before trailing activates (e.g., 0.5%)
- `close_trailing_retracement_pct`: retrace from peak that triggers close (e.g., 0.2%)
- `close_trailing_grid_ratio`: what % of closes are trailing vs grid (e.g., 0.5 = 50/50)

### Implementation:

1. In `grid_core.py` SmartCloseEngine, add `_check_trailing_profit()`:
   - Track peak price since position opened
   - If current profit > `trailing_profit_threshold_pct` of margin, activate trailing
   - If price retraces `trailing_profit_retracement_pct` from peak, trigger close

2. Wire into `check_close_conditions()` as a new close reason

**New env vars:**
```
TRAILING_PROFIT_ENABLED=true
TRAILING_PROFIT_THRESHOLD_PCT=0.5   # activate trailing after 0.5% profit
TRAILING_PROFIT_RETRACEMENT_PCT=0.2  # close when retraced 0.2% from peak
```

**Files:**
- Modify: `grid_core.py` — add trailing profit logic to SmartCloseEngine
- Modify: `config.py` — add env vars
- Modify: `grid_engine.py` — track peak price in GridState or use existing

---

## Phase 4: Unstuck Mechanism (Gradual Partial Closes)

**Core idea:** Replace the current 18% hard-floor-with-some-smart-close with passivbot-style gradual unsticking that respects a loss allowance budget.

**Current state:** 
- SmartCloseEngine evaluates losing positions with time decay, trailing stop, momentum, imbalance, recovery probability
- But ultimate fallback is hard floor at MAX_DRAWDOWN_PCT

**Passivbot's model:**
- `unstuck_loss_allowance_pct`: % of total_wallet_exposure that can be burned on unstuck (e.g., 4% × TWEL)
- `unstuck_threshold`: WE/effective_WE ratio that triggers unstuck (e.g., 1.5 — position is 1.5× the allowed wallet exposure)
- `unstuck_close_pct`: how much of the position to close per unstuck tick (e.g., 0.01 = 1%)
- `unstuck_ema_dist`: EMA band distance for price-based unstuck trigger

### Our adaptation:

The passivbot unstuck mechanism is tightly coupled to its Rust orchestrator. Our lighter adaptation:

1. **Track peak balance** per side (long/short) in `wallet_tracker.py` or a new `unstuck_tracker.py`
2. **Loss allowance budget**: `allowance = unstuck_loss_allowance_pct * total_wallet_exposure_limit * wallet_balance`
   - This is the MAX total loss we're willing to realize via unsticking
3. **Unstuck eligibility**: a position qualifies if it has been underwater for > `UNSTUCK_MIN_AGE_MINUTES` and loss > `UNSTUCK_MIN_LOSS_PCT`
4. **Priority**: sort eligible positions by `abs(position_loss) / allowance` ASCENDING (closest to breakeven first)
5. **Execution**: close a small fraction (`unstuck_close_pct` = 5-10%) of the position, deduct from allowance budget
6. **Gating**: if allowance is exhausted, no more unstucking — positions either recover or hit hard floor

**Key difference from current SmartCloseEngine:** 
- SmartCloseEngine decides "close or don't close" based on position state
- Unstuck mechanism manages "how MUCH to close" based on budget availability
- They work TOGETHER: SmartCloseEngine identifies candidates, unstuck mechanism doses the closes

**New files:**
- Create: `unstuck_tracker.py` — budget management + peak tracking

**Files:**
- Modify: `multi_grid_manager.py` — wire unstuck into deployment/close cycle
- Modify: `config.py` — add env vars
- Modify: `grid_core.py` — add unstuck close reason

**New env vars:**
```
UNSTUCK_ENABLED=true
UNSTUCK_LOSS_ALLOWANCE_PCT=4.0      # 4% of TWEL budget for unstuck
UNSTUCK_MIN_LOSS_PCT=2.0            # only unstuck positions at >2% loss
UNSTUCK_MIN_AGE_MINUTES=10          # must be underwater at least 10 min
UNSTUCK_CLOSE_FRACTION=0.08         # close 8% of position per unstuck tick
UNSTUCK_COOLDOWN_MINUTES=5          # wait between unstuck actions
```

---

## Bite-Sized Tasks

### Task 1: Fix progressive sizing formula
- **File:** `grid_engine.py:245`
- **Change:** One-line formula flip
- **Test:** `python3 -c "from grid_engine import GridEngine; g=GridEngine(); r=g.calculate_grid_levels('SOLUSDT',170,150,12,160,35,0.5,progressive_sizing_enabled=True,progressive_min_factor=0.35,progressive_max_factor=2.0,progressive_curve_power=1.5); buy_levels=[l for l in r.grid_levels if l.side=='Buy']; print('Center buy qty:', buy_levels[-1].qty, 'Edge buy qty:', buy_levels[0].qty); assert buy_levels[-1].qty > buy_levels[0].qty, 'FAIL: center should be bigger than edge'"`

### Task 2: Add config vars for double-down
- **Files:** `config.py`
- Add `DOUBLE_DOWN_ENABLED`, `DOUBLE_DOWN_FACTOR`, `DOUBLE_DOWN_MAX_ENTRIES`, etc.

### Task 3: Implement `calculate_avg_entry()` in grid_core.py
- Merge old + new fills into weighted average entry price

### Task 4: Implement `_check_double_down_opportunity()` in multi_grid_manager.py
- Detect losing positions, calculate doubled size, trigger deploy

### Task 5: Add trailing profit close to SmartCloseEngine
- Track peak price, activate trailing, trigger close on retrace

### Task 6: Create `unstuck_tracker.py` with budget management
- Track peak balance, manage loss allowance budget

### Task 7: Wire unstuck into deployment/close cycle
- Integrate with SmartCloseEngine, dose partial closes

### Task 8: Update token_profiles.json
- Add double-down and unstuck configs to default_token_profile

### Task 9: Integration test with dry-run
- Verify all 4 features work together in dry-run mode
