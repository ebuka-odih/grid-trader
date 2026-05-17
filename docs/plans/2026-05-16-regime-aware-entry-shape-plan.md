# Regime-Aware Entry Shape and Asymmetric Grid Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace the current mostly neutral, ATR-centered symmetric entry flow with a regime-aware deployment pipeline that ranks symbols, gates entry quality, builds structure-aware bounds, shapes ladders asymmetrically by regime, escalates risk around deep fill counts, and persists enough metadata to learn which entry geometries actually work.

**Architecture:** Keep the bot pure-logic and deterministic in the live loop. Extend the scanner to emit richer market-shape features, add a dedicated entry-shape planner/gate before deployment, teach `GridEngine` how to build non-uniform ladders without changing recovery-first close ownership, and persist setup-shape metadata in the journal/API so backtests and live audits can correlate shape with outcomes.

**Tech Stack:** Python, pandas, numpy, SQLite/SQLAlchemy, existing `coin_scanner.py` / `grid_engine.py` / `multi_grid_manager.py` / `improvement_loop.py` / pytest.

---

## Phase 0 — Success criteria and guardrails

### Task 0.1: Lock the acceptance criteria in a doc comment block

**Objective:** Define what “better entry shape” means before changing code.

**Files:**
- Modify: `docs/plans/2026-05-16-regime-aware-entry-shape-plan.md`
- Reference: `grid-trader-development/references/2026-05-16-entry-shape-audit.md`

**Step 1: Capture explicit success criteria**

Add this checklist to the implementation tracking notes when executing:

```markdown
Success criteria:
- scanner output includes regime + entry-shape metrics, not just ATR/range/MR
- deployment can reject a high-score symbol if entry location is poor
- bounds are no longer always `price ± 2*ATR`
- ladder spacing/density can differ by regime while preserving two-sided execution
- `fills >= 5` is visible to deployment/risk escalation logic
- setup-shape metadata is stored in DB and surfaced through API/state
- no LLM is added to the bot loop
- recovery-first close ownership remains with explicit engine/risk rules
```

**Step 2: Verify the scope stays inside user constraints**

Checklist:
- no DB deletion
- no live runtime side effects during planning
- no direct-close path added from simple imbalance alone
- direction remains metadata/planning input unless explicitly made executable by tests and spec

**Step 3: Commit later during implementation**

```bash
git add docs/plans/2026-05-16-regime-aware-entry-shape-plan.md
git commit -m "docs: add regime-aware entry-shape implementation plan"
```

---

## Phase 1 — Add market-shape data structures

### Task 1.1: Introduce scanner-side shape dataclasses

**Objective:** Give the scanner a typed way to describe regime, structure, and entry quality.

**Files:**
- Modify: `coin_scanner.py`
- Test: `tests/test_scanner_learning.py`
- Create: `tests/test_entry_shape_planner.py`

**Step 1: Write failing dataclass test**

Add a test asserting scanner results can carry shape metadata:

```python
from coin_scanner import CoinScore

def test_coin_score_supports_entry_shape_fields():
    score = CoinScore(
        symbol="TEST/USDT:USDT",
        price=100.0,
        high_24h=110.0,
        low_24h=90.0,
        volume_24h_usdt=1_000_000.0,
        atr_pct=1.2,
        range_pct=8.0,
        mean_reversion_score=0.72,
        grid_score=0.84,
        suggested_upper=108.0,
        suggested_lower=94.0,
        suggested_grids=12,
        suggested_leverage=10,
        trend_direction="neutral",
        market_regime="ranging",
        entry_quality_score=0.81,
        range_position=0.35,
        vwap_distance_pct=-0.4,
        pullback_depth_pct=1.1,
        slope_score=0.02,
        acceleration_score=-0.01,
    )
    assert score.market_regime == "ranging"
    assert score.entry_quality_score > 0.8
```

**Step 2: Run the targeted test**

Run:

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: FAIL because the new fields do not exist yet.

**Step 3: Add minimal fields to `CoinScore`**

Extend `CoinScore` with copy-pasteable fields like:

```python
market_regime: str = "ranging"
entry_quality_score: float = 0.0
range_position: float = 0.5
vwap_distance_pct: float = 0.0
pullback_depth_pct: float = 0.0
slope_score: float = 0.0
acceleration_score: float = 0.0
entry_shape_notes: str = ""
```

**Step 4: Re-run tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add coin_scanner.py tests/test_entry_shape_planner.py
git commit -m "feat: add scanner entry-shape metadata fields"
```

### Task 1.2: Add reusable scanner helpers for market-shape metrics

**Objective:** Extract deterministic calculations for range position, VWAP distance, slope, and pullback depth.

**Files:**
- Modify: `coin_scanner.py`
- Test: `tests/test_entry_shape_planner.py`

**Step 1: Write failing helper tests**

Add tests like:

```python
import pandas as pd
from coin_scanner import CoinScanner

def test_range_position_is_normalized_between_zero_and_one():
    df = pd.DataFrame({
        "high": [110, 111, 112],
        "low": [90, 91, 92],
        "close": [95, 100, 105],
        "volume": [10, 10, 10],
    })
    scanner = CoinScanner.__new__(CoinScanner)
    value = scanner._range_position(current_price=100.0, high_lookback=112.0, low_lookback=90.0)
    assert 0.0 <= value <= 1.0
```

**Step 2: Run test to confirm failure**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: FAIL because helper methods are missing.

**Step 3: Implement minimal helpers in `coin_scanner.py`**

Add methods:

```python
def _range_position(self, current_price: float, high_lookback: float, low_lookback: float) -> float:
    width = max(high_lookback - low_lookback, 1e-9)
    return min(1.0, max(0.0, (current_price - low_lookback) / width))

def _rolling_vwap(self, df: pd.DataFrame) -> float:
    volume = df["volume"].clip(lower=0)
    denom = float(volume.sum()) or 1.0
    return float((df["close"] * volume).sum() / denom)

def _vwap_distance_pct(self, current_price: float, vwap_price: float) -> float:
    base = max(abs(vwap_price), 1e-9)
    return ((current_price - vwap_price) / base) * 100.0

def _pullback_depth_pct(self, current_price: float, swing_extreme: float) -> float:
    base = max(abs(swing_extreme), 1e-9)
    return abs(current_price - swing_extreme) / base * 100.0
```

**Step 4: Re-run test**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add coin_scanner.py tests/test_entry_shape_planner.py
git commit -m "feat: add scanner market-shape helper metrics"
```

---

## Phase 2 — Classify regime and entry quality separately from symbol ranking

### Task 2.1: Make regime classification explicit instead of implied

**Objective:** Stop overloading `trend_direction` as the only market-shape signal.

**Files:**
- Modify: `coin_scanner.py`
- Test: `tests/test_entry_shape_planner.py`

**Step 1: Write failing regime tests**

Add tests for at least:
- ranging
- trending_up
- trending_down
- volatile

Example:

```python
def test_regime_classifier_prefers_ranging_for_high_mr_series():
    scanner = CoinScanner.__new__(CoinScanner)
    regime = scanner._classify_market_regime(
        mr_score=0.82,
        slope=0.0001,
        atr_pct=1.2,
        range_position=0.45,
    )
    assert regime == "ranging"
```

**Step 2: Run targeted tests**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: FAIL because classifier is missing.

**Step 3: Implement `_classify_market_regime()`**

Add logic along these lines:

```python
def _classify_market_regime(self, mr_score: float, slope: float, atr_pct: float, range_position: float) -> str:
    if atr_pct >= 3.0:
        return "volatile"
    if mr_score >= 0.65:
        return "ranging"
    if slope >= 0.0003:
        return "trending_up"
    if slope <= -0.0003:
        return "trending_down"
    return "ranging"
```

**Step 4: Re-run tests**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add coin_scanner.py tests/test_entry_shape_planner.py
git commit -m "feat: add explicit market regime classifier"
```

### Task 2.2: Add an entry-quality score that can reject bad locations even for good symbols

**Objective:** Split “good symbol” from “good entry now”.

**Files:**
- Modify: `coin_scanner.py`
- Create: `entry_shape_planner.py`
- Test: `tests/test_entry_shape_planner.py`

**Step 1: Write failing tests for entry-quality score**

```python
def test_entry_quality_penalizes_ranging_coin_at_mid_chop_without_edge():
    score = compute_entry_quality(
        market_regime="ranging",
        range_position=0.50,
        vwap_distance_pct=0.02,
        pullback_depth_pct=0.10,
        atr_pct=1.0,
    )
    assert score < 0.5
```

**Step 2: Run tests**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: FAIL because entry scoring does not exist.

**Step 3: Implement entry-quality scoring in `entry_shape_planner.py`**

Create a pure-logic module with functions like:

```python
def compute_entry_quality(*, market_regime: str, range_position: float, vwap_distance_pct: float, pullback_depth_pct: float, atr_pct: float) -> float:
    score = 0.0
    if market_regime == "ranging":
        edge_bonus = abs(range_position - 0.5) * 2.0
        score += min(1.0, edge_bonus) * 0.5
        score += min(1.0, abs(vwap_distance_pct) / 1.0) * 0.3
        score += min(1.0, pullback_depth_pct / max(atr_pct, 1e-9)) * 0.2
    elif market_regime in {"trending_up", "trending_down"}:
        score += min(1.0, pullback_depth_pct / max(atr_pct, 1e-9)) * 0.5
        score += (1.0 - min(1.0, abs(range_position - 0.5))) * 0.2
        score += min(1.0, abs(vwap_distance_pct) / 1.5) * 0.3
    else:
        score += 0.25
    return round(min(1.0, max(0.0, score)), 4)
```

**Step 4: Re-run tests**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add entry_shape_planner.py coin_scanner.py tests/test_entry_shape_planner.py
git commit -m "feat: add entry-quality scoring separate from ranking"
```

---

## Phase 3 — Replace `price ± 2*ATR` bounds with structure-aware planning

### Task 3.1: Add a pure planner that outputs regime-aware bounds and ladder shape

**Objective:** Centralize bound and ladder planning in a testable pure-logic module.

**Files:**
- Create: `entry_shape_planner.py`
- Test: `tests/test_entry_shape_planner.py`

**Step 1: Write failing planner contract tests**

Add tests like:

```python
from entry_shape_planner import plan_entry_shape

def test_ranging_plan_anchors_bounds_to_range_edges_not_raw_atr_box():
    plan = plan_entry_shape(
        current_price=100.0,
        market_regime="ranging",
        atr=2.0,
        swing_high=108.0,
        swing_low=94.0,
        range_position=0.25,
        vwap_price=99.0,
        pullback_depth_pct=1.5,
    )
    assert plan.lower <= 95.0
    assert plan.upper >= 106.0
    assert plan.template_name == "range_reversion"
```

**Step 2: Run targeted test**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: FAIL because planner contract does not exist.

**Step 3: Implement typed planner output**

Create dataclass:

```python
@dataclass
class EntryShapePlan:
    template_name: str
    market_regime: str
    lower: float
    upper: float
    num_grids: int
    spacing_mode: str
    buy_density_bias: float
    sell_density_bias: float
    notes: str
```

Add `plan_entry_shape(...)` with first-pass templates:
- `range_reversion`
- `uptrend_pullback`
- `downtrend_pullback`
- `volatile_defensive`

**Step 4: Re-run tests**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add entry_shape_planner.py tests/test_entry_shape_planner.py
git commit -m "feat: add regime-aware entry-shape planner"
```

### Task 3.2: Wire planner output into scanner suggestions

**Objective:** Make `CoinScore.suggested_upper/lower/grids` come from the planner instead of fixed ATR boxes.

**Files:**
- Modify: `coin_scanner.py`
- Test: `tests/test_entry_shape_planner.py`

**Step 1: Write failing integration test**

```python
def test_score_coin_uses_entry_shape_plan_for_suggested_bounds():
    # monkeypatch planner to return deterministic bounds
    # assert CoinScore reflects planner output, not raw price ± 2*ATR
    ...
```

**Step 2: Run targeted test**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: FAIL because `_score_coin()` still hardcodes ATR box bounds.

**Step 3: Update `_score_coin()` flow**

Refactor to:
1. compute ATR + MR + slope + VWAP metrics
2. classify regime
3. compute entry quality
4. call `plan_entry_shape(...)`
5. use plan bounds / grids / notes in `CoinScore`

Minimal sketch:

```python
plan = plan_entry_shape(
    current_price=price,
    market_regime=market_regime,
    atr=atr,
    swing_high=float(df["high"].tail(32).max()),
    swing_low=float(df["low"].tail(32).min()),
    range_position=range_position,
    vwap_price=vwap_price,
    pullback_depth_pct=pullback_depth_pct,
)
suggested_upper = round(plan.upper, 4)
suggested_lower = round(plan.lower, 4)
suggested_grids = plan.num_grids
```

**Step 4: Re-run tests**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_planner.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add coin_scanner.py entry_shape_planner.py tests/test_entry_shape_planner.py
git commit -m "feat: drive scanner bounds from entry-shape planner"
```

---

## Phase 4 — Gate deployment on entry quality before risk/deploy

### Task 4.1: Add deterministic entry-gate review to decision supervision

**Objective:** Reject bad current locations even when the symbol itself scored well.

**Files:**
- Modify: `decision_supervisor.py`
- Modify: `tests/test_decision_supervisor.py`
- Modify: `coin_scanner.py`

**Step 1: Write failing decision supervisor tests**

Add tests such as:

```python
def test_low_entry_quality_coin_is_rejected_even_if_confidence_is_high():
    result = DecisionSupervisor().review_pre_trade_decision(
        decision=decision(),
        coin_score=coin(entry_quality_score=0.22, market_regime="ranging"),
        token_profile={"leverage": 50, "max_leverage": 50, "min_confidence": 0.6, "min_entry_quality_score": 0.5},
        active_symbols=set(),
    )
    assert not result.approved
    assert any("entry quality" in reason.lower() for reason in result.reasons)
```

**Step 2: Run targeted tests**

```bash
PYTHONPATH=. pytest tests/test_decision_supervisor.py -q
```

Expected: FAIL because `entry_quality_score` is not validated.

**Step 3: Add validation branch to `DecisionSupervisor.review_pre_trade_decision()`**

Add:

```python
min_entry_quality = float(token_profile.get("min_entry_quality_score", 0.45))
if getattr(coin_score, "entry_quality_score", 0.0) < min_entry_quality:
    reasons.append(
        f"Entry quality {coin_score.entry_quality_score:.2f} below minimum {min_entry_quality:.2f}"
    )
```

Also add warnings tying regime and location together when relevant.

**Step 4: Re-run tests**

```bash
PYTHONPATH=. pytest tests/test_decision_supervisor.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add decision_supervisor.py tests/test_decision_supervisor.py coin_scanner.py
git commit -m "feat: gate deployment on scanner entry quality"
```

### Task 4.2: Add manager-side rejection logging for entry-shape reasons

**Objective:** Make rejected entry geometry visible in logs and API state.

**Files:**
- Modify: `multi_grid_manager.py`
- Modify: `grid_api.py`
- Test: `tests/test_grid_api_state.py`

**Step 1: Write failing state test**

Assert scanner candidates or rejected candidates expose entry-quality context.

**Step 2: Run targeted test**

```bash
PYTHONPATH=. pytest tests/test_grid_api_state.py -q
```

Expected: FAIL because the API state does not include entry-shape metadata.

**Step 3: Add rejected-reason metadata to pushed API state**

Expose fields like:
- `market_regime`
- `entry_quality_score`
- `range_position`
- `entry_shape_notes`
- `decision_rejection_reasons`

**Step 4: Re-run tests**

```bash
PYTHONPATH=. pytest tests/test_grid_api_state.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add multi_grid_manager.py grid_api.py tests/test_grid_api_state.py
git commit -m "feat: expose entry-shape gate reasons in api state"
```

---

## Phase 5 — Build asymmetric ladders without breaking two-sided recovery-first execution

### Task 5.1: Add ladder-shape inputs to `GridEngine.calculate_grid_levels()`

**Objective:** Let the engine vary spacing/density while keeping buys below and sells above price.

**Files:**
- Modify: `grid_engine.py`
- Modify: `tests/test_asymmetric_grid.py`

**Step 1: Write failing asymmetric ladder tests**

Add tests for:
- symmetric ranging ladder
- denser buy side in `uptrend_pullback`
- denser sell side in `downtrend_pullback`
- no level crosses the current price incorrectly

Example:

```python
def test_uptrend_pullback_template_creates_more_buy_levels_below_price_than_sell_levels_above():
    grid = GridEngine().calculate_grid_levels(
        symbol="TEST/USDT:USDT",
        upper=110.0,
        lower=90.0,
        num_grids=10,
        current_price=100.0,
        leverage=10,
        order_size_usdt=1.0,
        spacing_mode="asymmetric",
        buy_density_bias=1.6,
        sell_density_bias=0.8,
    )
    below = [l for l in grid.grid_levels if l.price < 100.0]
    above = [l for l in grid.grid_levels if l.price > 100.0]
    assert len(below) > len(above)
    assert all(l.side == "Buy" for l in below)
    assert all(l.side == "Sell" for l in above)
```

**Step 2: Run targeted tests**

```bash
PYTHONPATH=. pytest tests/test_asymmetric_grid.py -q
```

Expected: FAIL because the engine only supports uniform spacing.

**Step 3: Implement spacing inputs minimally**

Extend signature:

```python
def calculate_grid_levels(..., spacing_mode: str = "symmetric", buy_density_bias: float = 1.0, sell_density_bias: float = 1.0, exp_sizing_gamma: float = 0.0):
```

For first-pass asymmetry, generate normalized below/above fractions separately rather than with a single `step`.

**Step 4: Preserve execution invariants**

Verify implementation keeps:
- all levels strictly inside `lower..upper`
- buys only below `current_price`
- sells only above `current_price`
- no one-sided executable ladder

**Step 5: Re-run tests**

```bash
PYTHONPATH=. pytest tests/test_asymmetric_grid.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add grid_engine.py tests/test_asymmetric_grid.py
git commit -m "feat: add asymmetric ladder spacing to grid engine"
```

### Task 5.2: Feed planner ladder settings into engine deployment paths

**Objective:** Ensure scanner/manager/engine all use the same template parameters.

**Files:**
- Modify: `coin_scanner.py`
- Modify: `grid_engine.py`
- Modify: `dry_run_engine.py`
- Modify: `live_engine.py`
- Modify: `agentic_orchestrator.py`
- Test: `tests/test_asymmetric_grid.py`

**Step 1: Write failing integration test**

Assert planner-produced biases flow into `calculate_grid_levels()` during deployment.

**Step 2: Run tests**

```bash
PYTHONPATH=. pytest tests/test_asymmetric_grid.py -q
```

Expected: FAIL because the new planner fields are not threaded through.

**Step 3: Thread planner params through `CoinScore` and deployment calls**

Suggested additional fields on `CoinScore`:

```python
spacing_mode: str = "symmetric"
buy_density_bias: float = 1.0
sell_density_bias: float = 1.0
template_name: str = "range_reversion"
```

Use them in each engine call site.

**Step 4: Re-run tests**

```bash
PYTHONPATH=. pytest tests/test_asymmetric_grid.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add coin_scanner.py grid_engine.py dry_run_engine.py live_engine.py agentic_orchestrator.py tests/test_asymmetric_grid.py
git commit -m "feat: wire planner ladder settings through deployment paths"
```

---

## Phase 6 — Escalate deployment/risk behavior around deep fill counts

### Task 6.1: Add fill-danger thresholds to runtime metadata and policy checks

**Objective:** Make the known danger zone (`fills >= 5`) a first-class signal.

**Files:**
- Modify: `grid_core.py`
- Modify: `multi_grid_manager.py`
- Modify: `rule_agent.py`
- Test: `tests/test_smart_close_e2e.py`
- Test: `tests/test_no_negative_position_closes.py`

**Step 1: Write failing tests**

Cover at least:
- warning metadata when fills hit 5
- no direct loss close from imbalance alone
- tighter deployment/monitoring behavior once the danger threshold is crossed

**Step 2: Run targeted tests**

```bash
PYTHONPATH=. pytest tests/test_smart_close_e2e.py tests/test_no_negative_position_closes.py -q
```

Expected: FAIL because no explicit fill-danger threshold exists.

**Step 3: Add deterministic threshold fields**

Examples:

```python
DANGER_FILL_COUNT = 5
SEVERE_FILL_COUNT = 8
```

Use them for:
- telemetry
- warnings
- optional tightening/freeze behavior
- never as a backdoor for premature red closes that violate recovery-first policy

**Step 4: Re-run tests**

```bash
PYTHONPATH=. pytest tests/test_smart_close_e2e.py tests/test_no_negative_position_closes.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add grid_core.py multi_grid_manager.py rule_agent.py tests/test_smart_close_e2e.py tests/test_no_negative_position_closes.py
git commit -m "feat: add deep-fill danger thresholds to runtime policy"
```

---

## Phase 7 — Persist setup-shape metadata to DB without deleting history

### Task 7.1: Extend `grid_cycles` with entry-shape columns

**Objective:** Persist enough setup context to explain future wins/losses.

**Files:**
- Modify: `improvement_loop.py`
- Modify: `multi_grid_manager.py`
- Modify: `agentic_orchestrator.py`
- Test: `tests/test_wallet_restore.py`
- Create: `tests/test_improvement_loop_entry_shape.py`

**Step 1: Write failing migration/persistence tests**

Test that `Base.metadata.create_all()` plus migration adds new columns without deleting DB history.

**Step 2: Run targeted tests**

```bash
PYTHONPATH=. pytest tests/test_improvement_loop_entry_shape.py -q
```

Expected: FAIL because new columns do not exist.

**Step 3: Add nullable columns to `GridCycleRecord`**

Suggested columns:

```python
template_name = Column(String(50), default="range_reversion")
market_regime = Column(String(20), default="ranging")
entry_quality_score = Column(Float, default=0.0)
range_position = Column(Float, default=0.5)
vwap_distance_pct = Column(Float, default=0.0)
pullback_depth_pct = Column(Float, default=0.0)
fill_danger_threshold = Column(Integer, default=5)
entry_shape_notes = Column(Text, default="")
```

Update:
- `record_cycle_start(...)`
- `record_cycle_close(...)` only if close-time shape fields need copying

**Step 4: Verify migration path stays additive**

Run:

```bash
PYTHONPATH=. pytest tests/test_improvement_loop_entry_shape.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add improvement_loop.py multi_grid_manager.py agentic_orchestrator.py tests/test_improvement_loop_entry_shape.py
git commit -m "feat: persist entry-shape metadata in grid cycle journal"
```

### Task 7.2: Surface setup-shape metadata through API/state

**Objective:** Make live audits possible without opening the DB first.

**Files:**
- Modify: `grid_api.py`
- Modify: `tests/test_grid_api_state.py`

**Step 1: Write failing API serialization tests**

Assert active slots / completed trades include the new metadata.

**Step 2: Run targeted test**

```bash
PYTHONPATH=. pytest tests/test_grid_api_state.py -q
```

Expected: FAIL because fields are not surfaced.

**Step 3: Add serialization fields**

Expose:
- `template_name`
- `market_regime`
- `entry_quality_score`
- `range_position`
- `vwap_distance_pct`
- `pullback_depth_pct`
- `entry_shape_notes`

**Step 4: Re-run tests**

```bash
PYTHONPATH=. pytest tests/test_grid_api_state.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add grid_api.py tests/test_grid_api_state.py
git commit -m "feat: expose entry-shape metadata in api responses"
```

---

## Phase 8 — Backtest and learning validation

### Task 8.1: Add backtest comparison for entry-shape templates

**Objective:** Verify this improves the known failure mode instead of just making logic more complex.

**Files:**
- Modify: `backtest_engine.py`
- Modify: `backtest_v4.py`
- Modify: `stress_test.py`
- Create: `tests/test_entry_shape_backtest_smoke.py`

**Step 1: Write failing smoke test**

Assert the backtest runner can accept an entry-shape mode and return shape-aware stats.

**Step 2: Run test**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_backtest_smoke.py -q
```

Expected: FAIL because no entry-shape comparison mode exists.

**Step 3: Add comparison metrics**

At minimum emit:
- trade count by `template_name`
- PnL by regime
- drawdown closes by regime
- fill-count cohorts split by template
- count of trades with `fills >= 5`

**Step 4: Re-run test**

```bash
PYTHONPATH=. pytest tests/test_entry_shape_backtest_smoke.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add backtest_engine.py backtest_v4.py stress_test.py tests/test_entry_shape_backtest_smoke.py
git commit -m "feat: add backtest metrics for entry-shape templates"
```

### Task 8.2: Run end-to-end targeted verification suite

**Objective:** Confirm the feature is safe, deterministic, and aligned with existing policy.

**Files:**
- No code changes required unless tests fail.

**Step 1: Run targeted suite**

```bash
PYTHONPATH=. pytest \
  tests/test_entry_shape_planner.py \
  tests/test_asymmetric_grid.py \
  tests/test_decision_supervisor.py \
  tests/test_grid_api_state.py \
  tests/test_improvement_loop_entry_shape.py \
  tests/test_smart_close_e2e.py \
  tests/test_no_negative_position_closes.py -q
```

Expected: PASS.

**Step 2: Run broader regression smoke**

```bash
PYTHONPATH=. pytest \
  tests/test_grid_core.py \
  tests/test_live_close_paths.py \
  tests/test_heartbeat_regulator.py \
  tests/test_risk_policy_caps.py -q
```

Expected: PASS.

**Step 3: Optional historical comparison**

Run:

```bash
python backtest_engine.py --run-all
python stress_test.py
```

Expected review points:
- fewer drawdown-heavy cohorts with `fills >= 5`
- direction not silently collapsed to all-neutral if trend templates are intended
- no regression into one-sided executable ladders

**Step 4: Commit final implementation batch**

```bash
git add .
git commit -m "feat: add regime-aware entry-shape planning for grid deployment"
```

---

## File-by-file implementation map

### Core planning and scanner
- `coin_scanner.py`
  - add shape metrics
  - classify regime explicitly
  - compute entry quality
  - delegate bounds/template planning to `entry_shape_planner.py`
- `entry_shape_planner.py` *(new)*
  - pure functions and dataclasses for planner output
  - no exchange imports

### Execution path
- `grid_engine.py`
  - support asymmetric spacing inputs
  - preserve two-sided buy-below / sell-above invariant
- `dry_run_engine.py`
  - pass planner ladder params into grid creation
- `live_engine.py`
  - same as dry-run path; no divergence
- `agentic_orchestrator.py`
  - if still used for pretrade flow, keep planner fields synchronized

### Deployment gate / runtime state
- `decision_supervisor.py`
  - reject low entry-quality setups
- `multi_grid_manager.py`
  - log entry-shape decisions/rejections
  - surface fill-danger telemetry
- `grid_api.py`
  - expose entry-shape fields in active/completed state

### Persistence / learning
- `improvement_loop.py`
  - additive schema migration only
  - persist shape metadata in `grid_cycles`
- `backtest_engine.py`, `backtest_v4.py`, `stress_test.py`
  - compare template/regime outcomes and deep-fill cohorts

### Tests to add or extend
- `tests/test_entry_shape_planner.py` *(new)*
- `tests/test_improvement_loop_entry_shape.py` *(new)*
- `tests/test_entry_shape_backtest_smoke.py` *(new)*
- `tests/test_asymmetric_grid.py`
- `tests/test_decision_supervisor.py`
- `tests/test_grid_api_state.py`
- `tests/test_smart_close_e2e.py`
- `tests/test_no_negative_position_closes.py`

---

## Risks and non-goals

### Risks
- overfitting planner templates to recent audit behavior
- accidentally turning regime metadata into direct close authority
- creating asymmetric level density that behaves like one-sided directional betting
- API/DB drift if metadata is persisted in one path but not another

### Non-goals
- no LLM in the bot loop
- no DB reset / delete / history wipe
- no abandonment of recovery-first close policy
- no switch to Bybit chase/pegged order semantics for the ladder

---

## Suggested execution order

1. dataclasses and helper metrics
2. regime classification
3. entry-quality scoring
4. structure-aware planner
5. scanner integration
6. decision gate
7. asymmetric ladder spacing
8. deployment path wiring
9. fill-danger telemetry
10. DB/API persistence
11. backtest validation
12. regression suite

---

## Done definition

The implementation is done when all of the following are true:
- scanner output carries usable market-shape metadata
- at least one test proves a good symbol can still be rejected for poor entry location
- at least one test proves an uptrend/downtrend template changes ladder density without breaking two-sided execution
- journal and API expose entry-shape metadata for auditability
- targeted tests and regression smokes pass
- no code path violates the user’s recovery-first policy or reintroduces LLM dependency into the bot loop
