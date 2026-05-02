# Grid Engine Core Extraction Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Extract shared trading logic from `dry_run_engine.py` and `live_engine.py` into a reusable `grid_core.py` module so both dry-run and live engines share the same position management, PnL calculation, and risk checks.

**Architecture:** Both `DryRunEngine` and `LiveEngine` currently duplicate position tracking, fill processing, PnL calculation, target/drawdown checks, and cycle management. Extract these into `grid_core.py` — a pure-logic module with no exchange dependencies. Both engines import from it.

**Tech Stack:** Python 3.11, dataclasses, no external deps for core module.

---

## Task 1: Create `grid_core.py` with shared dataclasses

**Objective:** Define the core data structures that both engines share.

**Files:**
- Create: `grid_core.py`

**Step 1: Write failing test**

```python
# tests/test_grid_core.py
from grid_core import GridPosition, GridConfig, PnLResult

def test_grid_position_defaults():
    pos = GridPosition()
    assert pos.qty == 0.0
    assert pos.side == ""
    assert pos.entry_price == 0.0

def test_grid_config_from_env():
    cfg = GridConfig()
    assert cfg.target_pnl_pct_low > 0
    assert cfg.max_drawdown_pct > 0
    assert cfg.base_order_size > 0

def test_pnl_result_fields():
    r = PnLResult(realized=1.0, unrealized=0.5, total=1.5)
    assert r.total == 1.5
```

**Step 2: Run test to verify failure**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py -v`
Expected: FAIL — "ModuleNotFoundError: No module named 'grid_core'"

**Step 3: Write minimal implementation**

```python
# grid_core.py
"""
Grid Core — shared trading logic for dry-run and live engines.

No exchange dependencies. Pure position/PnL/risk math.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class GridConfig:
    """Shared grid configuration — reads from env with sane defaults."""
    target_pnl_pct_low: float = float(os.getenv("TARGET_PNL_PCT_LOW", "0.3"))
    target_pnl_pct_high: float = float(os.getenv("TARGET_PNL_PCT_HIGH", "1.0"))
    max_drawdown_pct: float = float(os.getenv("MAX_DRAWDOWN_PCT", "8.0"))
    base_order_size: float = float(os.getenv("BASE_ORDER_SIZE_USDT", "10.0"))
    default_leverage: int = int(os.getenv("DEFAULT_LEVERAGE", "10"))
    default_num_grids: int = int(os.getenv("DEFAULT_NUM_GRIDS", "20"))


@dataclass
class GridPosition:
    """Tracks open position state for a single grid."""
    qty: float = 0.0
    side: str = ""  # "Buy" or "Sell"
    entry_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class PnLResult:
    """Snapshot of PnL at a point in time."""
    realized: float
    unrealized: float
    total: float


@dataclass
class CycleState:
    """Tracks multi-cycle grid state."""
    cycles_completed: int = 0
    max_cycles: int = 1
    cumulative_pnl: float = 0.0
```

**Step 4: Run test to verify pass**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py -v`
Expected: 3 passed

**Step 5: Commit**

```bash
git add grid_core.py tests/test_grid_core.py
git commit -m "feat: add grid_core.py with shared dataclasses"
```

---

## Task 2: Extract position management into `grid_core.py`

**Objective:** Move fill processing (open/add/close position) from both engines into shared functions.

**Files:**
- Modify: `grid_core.py`
- Modify: `tests/test_grid_core.py`

**Step 1: Write failing test**

```python
# Add to tests/test_grid_core.py
from grid_core import GridPosition, process_fill

def test_process_fill_open_long():
    pos = GridPosition()
    process_fill(pos, "Buy", 100.0, 0.5)
    assert pos.side == "Buy"
    assert pos.qty == 0.5
    assert pos.entry_price == 100.0

def test_process_fill_add_to_long():
    pos = GridPosition(qty=0.5, side="Buy", entry_price=100.0)
    process_fill(pos, "Buy", 110.0, 0.5)
    assert pos.qty == 1.0
    assert pos.entry_price == 105.0  # average

def test_process_fill_close_long_with_profit():
    pos = GridPosition(qty=0.5, side="Buy", entry_price=100.0)
    process_fill(pos, "Sell", 110.0, 0.5)
    assert pos.qty == 0.0
    assert pos.side == ""
    assert pos.realized_pnl == 5.0  # (110-100)*0.5

def test_process_fill_partial_close():
    pos = GridPosition(qty=1.0, side="Buy", entry_price=100.0)
    process_fill(pos, "Sell", 110.0, 0.3)
    assert pos.qty == 0.7
    assert pos.realized_pnl == 3.0  # (110-100)*0.3

def test_process_fill_open_short():
    pos = GridPosition()
    process_fill(pos, "Sell", 200.0, 0.2)
    assert pos.side == "Sell"
    assert pos.qty == 0.2
    assert pos.entry_price == 200.0

def test_process_fill_close_short_with_profit():
    pos = GridPosition(qty=0.5, side="Sell", entry_price=200.0)
    process_fill(pos, "Buy", 180.0, 0.5)
    assert pos.qty == 0.0
    assert pos.realized_pnl == 10.0  # (200-180)*0.5
```

**Step 2: Run test to verify failure**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py::test_process_fill_open_long -v`
Expected: FAIL — "cannot import name 'process_fill'"

**Step 3: Write implementation**

```python
# Add to grid_core.py

def process_fill(pos: GridPosition, side: str, price: float, qty: float) -> None:
    """Update position after a fill. Handles open/add/close/partial-close.
    
    This is the EXACT logic from DryRunEngine._simulate_fill and LiveEngine._update_position,
    unified into one function.
    """
    if pos.qty > 0 and pos.side != side:
        # Closing trade — realize PnL
        close_qty = min(qty, pos.qty)
        if pos.side == "Buy":
            pos.realized_pnl += (price - pos.entry_price) * close_qty
        else:
            pos.realized_pnl += (pos.entry_price - price) * close_qty
        
        pos.qty -= close_qty
        if pos.qty <= 0:
            pos.qty = 0.0
            pos.entry_price = 0.0
            pos.side = ""
    else:
        # Opening or adding to position
        if pos.qty > 0 and pos.side == side:
            # Average entry
            total_cost = pos.entry_price * pos.qty + price * qty
            pos.qty += qty
            pos.entry_price = total_cost / pos.qty
        else:
            # Fresh position
            pos.qty = qty
            pos.entry_price = price
            pos.side = side


def update_unrealized_pnl(pos: GridPosition, price: float) -> None:
    """Update unrealized PnL based on current price."""
    if pos.qty > 0 and pos.entry_price > 0:
        if pos.side == "Buy":
            pos.unrealized_pnl = (price - pos.entry_price) * pos.qty
        else:
            pos.unrealized_pnl = (pos.entry_price - price) * pos.qty
    else:
        pos.unrealized_pnl = 0.0


def get_pnl(pos: GridPosition) -> PnLResult:
    """Get current PnL snapshot."""
    return PnLResult(
        realized=pos.realized_pnl,
        unrealized=pos.unrealized_pnl,
        total=pos.realized_pnl + pos.unrealized_pnl,
    )
```

**Step 4: Run test to verify pass**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py -v`
Expected: 9 passed

**Step 5: Commit**

```bash
git add grid_core.py tests/test_grid_core.py
git commit -m "feat: extract position management into grid_core.process_fill"
```

---

## Task 3: Extract target/drawdown checks into `grid_core.py`

**Objective:** Move PnL target and drawdown limit calculations into shared functions.

**Files:**
- Modify: `grid_core.py`
- Modify: `tests/test_grid_core.py`

**Step 1: Write failing test**

```python
# Add to tests/test_grid_core.py
from grid_core import GridConfig, check_target_hit, check_drawdown_breach

def test_check_target_hit():
    cfg = GridConfig(target_pnl_pct_low=0.3, base_order_size=10.0)
    # 20 levels * $10 = $200 allocated margin, 0.3% = $0.60 target
    assert check_target_hit(total_pnl=0.7, fills_count=3, allocated_margin=200.0, cfg=cfg) == True
    assert check_target_hit(total_pnl=0.5, fills_count=3, allocated_margin=200.0, cfg=cfg) == False
    assert check_target_hit(total_pnl=0.7, fills_count=1, allocated_margin=200.0, cfg=cfg) == False  # min 2 fills

def test_check_drawdown_breach():
    cfg = GridConfig(max_drawdown_pct=8.0)
    # $200 allocated * 8% = $16 limit
    assert check_drawdown_breach(total_pnl=-17.0, allocated_margin=200.0, cfg=cfg) == True
    assert check_drawdown_breach(total_pnl=-15.0, allocated_margin=200.0, cfg=cfg) == False
    assert check_drawdown_breach(total_pnl=5.0, allocated_margin=200.0, cfg=cfg) == False  # positive
```

**Step 2: Run test to verify failure**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py::test_check_target_hit -v`
Expected: FAIL

**Step 3: Write implementation**

```python
# Add to grid_core.py

def allocated_margin(order_size_usdt: float, num_grids: int) -> float:
    """Calculate total margin allocated to a grid."""
    return order_size_usdt * num_grids


def target_pnl_usdt(allocated_margin: float, target_pct: float) -> float:
    """Calculate target PnL in USDT from allocated margin and percentage."""
    return allocated_margin * target_pct / 100.0


def drawdown_limit_usdt(allocated_margin: float, max_dd_pct: float) -> float:
    """Calculate drawdown limit in USDT."""
    return allocated_margin * max_dd_pct / 100.0


def check_target_hit(total_pnl: float, fills_count: int, allocated_margin: float, cfg: GridConfig) -> bool:
    """Check if PnL target is hit. Requires minimum 2 fills."""
    if fills_count < 2:
        return False
    target = target_pnl_usdt(allocated_margin, cfg.target_pnl_pct_low)
    return total_pnl >= target


def check_drawdown_breach(total_pnl: float, allocated_margin: float, cfg: GridConfig) -> bool:
    """Check if drawdown limit is breached."""
    if total_pnl >= 0:
        return False
    limit = drawdown_limit_usdt(allocated_margin, cfg.max_drawdown_pct)
    return abs(total_pnl) > limit
```

**Step 4: Run test to verify pass**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py -v`
Expected: 12 passed

**Step 5: Commit**

```bash
git add grid_core.py tests/test_grid_core.py
git commit -m "feat: extract target/drawdown checks into grid_core"
```

---

## Task 4: Extract cycle reset logic into `grid_core.py`

**Objective:** Move grid reset-for-next-cycle logic into a shared function.

**Files:**
- Modify: `grid_core.py`
- Modify: `tests/test_grid_core.py`

**Step 1: Write failing test**

```python
# Add to tests/test_grid_core.py
from grid_core import GridPosition, CycleState, reset_for_next_cycle

def test_reset_for_next_cycle():
    pos = GridPosition(qty=0.5, side="Buy", entry_price=100.0, realized_pnl=5.0, unrealized_pnl=1.0)
    cycle = CycleState(cycles_completed=0, max_cycles=3, cumulative_pnl=0.0)
    
    reset_for_next_cycle(pos, cycle)
    
    assert pos.qty == 0.0
    assert pos.side == ""
    assert pos.entry_price == 0.0
    assert pos.unrealized_pnl == 0.0
    assert pos.realized_pnl == 5.0  # preserved in cumulative
    assert cycle.cycles_completed == 1
    assert cycle.cumulative_pnl == 5.0
```

**Step 2: Run test to verify failure**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py::test_reset_for_next_cycle -v`
Expected: FAIL

**Step 3: Write implementation**

```python
# Add to grid_core.py

def reset_for_next_cycle(pos: GridPosition, cycle: CycleState) -> None:
    """Reset position state for next trading cycle, preserving cumulative PnL."""
    cycle.cumulative_pnl += pos.realized_pnl
    cycle.cycles_completed += 1
    
    pos.qty = 0.0
    pos.side = ""
    pos.entry_price = 0.0
    pos.unrealized_pnl = 0.0
    # realized_pnl preserved — it's cumulative within the engine's session
```

**Step 4: Run test to verify pass**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py -v`
Expected: 13 passed

**Step 5: Commit**

```bash
git add grid_core.py tests/test_grid_core.py
git commit -m "feat: extract cycle reset into grid_core.reset_for_next_cycle"
```

---

## Task 5: Refactor `DryRunEngine` to use `grid_core`

**Objective:** Replace duplicated logic in `dry_run_engine.py` with calls to `grid_core` functions.

**Files:**
- Modify: `dry_run_engine.py`
- Modify: `tests/test_grid_core.py` (add integration test)

**Step 1: Write failing test**

```python
# Add to tests/test_grid_core.py
def test_dry_run_uses_grid_core():
    """Verify DryRunEngine imports and uses grid_core functions."""
    from dry_run_engine import DryRunEngine
    engine = DryRunEngine()
    # If grid_core integration works, engine should have config
    assert hasattr(engine, '_config')
```

**Step 2: Run test to verify failure**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py::test_dry_run_uses_grid_core -v`
Expected: FAIL (no `_config` attribute)

**Step 3: Refactor DryRunEngine**

Changes to `dry_run_engine.py`:
1. Import `GridConfig, GridPosition, process_fill, update_unrealized_pnl, get_pnl, check_target_hit, check_drawdown_breach, reset_for_next_cycle, allocated_margin` from `grid_core`
2. Replace `DryRunState.position_qty/position_side/entry_price/realized_pnl/unrealized_pnl` with a `GridPosition` instance
3. Replace `_simulate_fill` body with call to `process_fill`
4. Replace `_update_unrealized_pnl` with call to `update_unrealized_pnl`
5. Replace target/drawdown checks in `on_price_update` with `check_target_hit` / `check_drawdown_breach`
6. Replace `_reset_grid_for_next_cycle` internals with `reset_for_next_cycle`
7. Keep `DryRunState` but delegate position tracking to `GridPosition`

**Step 4: Run ALL existing tests**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/ -v --tb=short`
Expected: All existing tests still pass (no behavior change)

**Step 5: Commit**

```bash
git add dry_run_engine.py
git commit -m "refactor: DryRunEngine uses grid_core for position/PnL/risk logic"
```

---

## Task 6: Refactor `LiveEngine` to use `grid_core`

**Objective:** Replace duplicated logic in `live_engine.py` with calls to `grid_core` functions.

**Files:**
- Modify: `live_engine.py`

**Step 1: Write failing test**

```python
# Add to tests/test_grid_core.py
def test_live_engine_uses_grid_core():
    """Verify LiveEngine imports and uses grid_core functions."""
    from live_engine import LiveEngine
    engine = LiveEngine()
    assert hasattr(engine, '_position')
```

**Step 2: Run test to verify failure**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py::test_live_engine_uses_grid_core -v`
Expected: FAIL

**Step 3: Refactor LiveEngine**

Changes to `live_engine.py`:
1. Import from `grid_core`
2. Replace `LiveState.position_qty/position_side/entry_price/realized_pnl/unrealized_pnl` with `GridPosition`
3. Replace `_update_position` with call to `process_fill`
4. Replace `_update_unrealized_pnl` with `update_unrealized_pnl`
5. Replace target/drawdown checks with `check_target_hit` / `check_drawdown_breach`
6. Keep `LiveState` but delegate position tracking to `GridPosition`

**Step 4: Run ALL existing tests**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/ -v --tb=short`
Expected: All existing tests still pass

**Step 5: Commit**

```bash
git add live_engine.py
git commit -m "refactor: LiveEngine uses grid_core for position/PnL/risk logic"
```

---

## Task 7: Wire `grid_core.GridConfig` into `multi_grid_manager`

**Objective:** Replace hardcoded config imports in multi_grid_manager with `GridConfig`.

**Files:**
- Modify: `multi_grid_manager.py`

**Step 1: Write failing test**

```python
# Add to tests/test_grid_core.py
def test_multi_grid_manager_uses_grid_config():
    """Verify multi_grid_manager imports GridConfig."""
    import importlib
    mod = importlib.import_module("multi_grid_manager")
    # Should import GridConfig somewhere
    source = open("multi_grid_manager.py").read()
    assert "from grid_core import" in source or "import grid_core" in source
```

**Step 2: Run test to verify failure**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/test_grid_core.py::test_multi_grid_manager_uses_grid_config -v`
Expected: FAIL

**Step 3: Update multi_grid_manager.py**

Replace scattered config imports:
```python
# Before:
from config import TARGET_PNL_LOW, TARGET_PNL_PCT_LOW, MAX_DRAWDOWN_PCT, ...

# After:
from grid_core import GridConfig, process_fill, update_unrealized_pnl, check_target_hit
```

**Step 4: Run ALL existing tests**

Run: `cd ~/.hermes/projects/grid-trader && python -m pytest tests/ -v --tb=short`
Expected: All existing tests still pass

**Step 5: Commit**

```bash
git add multi_grid_manager.py
git commit -m "refactor: multi_grid_manager uses grid_core.GridConfig"
```

---

## Verification Checklist

- [ ] `grid_core.py` has zero exchange dependencies (no ccxt, no websockets)
- [ ] `DryRunEngine` delegates position/PnL/risk to `grid_core`
- [ ] `LiveEngine` delegates position/PnL/risk to `grid_core`
- [ ] All existing tests pass (no behavior change)
- [ ] `grid_core` tests cover: fill processing, PnL calc, target/drawdown, cycle reset
- [ ] Import paths updated in all files that reference the extracted functions
