"""
Dry-Run Engine v4 — simulates grid trading with smart close logic.

v4 Changes:
- Uses grid_core.py for shared logic (position tracking, fill processing, close conditions)
- Smart negative-close: time decay, momentum exit, grid imbalance, trailing stop
- No more duplicated position tracking between dry-run and live

v3 Features (preserved):
- Fast spike detection (10s window)
- Per-level exposure cap
- Multi-cycle grid support
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from coin_scanner import CoinScore
from grid_engine import GridState, GridLevel
from adaptive_grid import AdaptiveGrid, AdaptiveConfig, AdaptiveResult, default_config
from grid_core import (
    GridPosition, FillEvent, PnLResult, CycleState, GridImbalance,
    SmartCloseEngine, SmartCloseConfig, CloseReason,
    process_fill, check_close_conditions, reset_position, reset_imbalance,
    perform_partial_close,
    allocated_margin_usdt, target_pnl_usdt, drawdown_limit_usdt,
)

from config import (
    TARGET_PNL_LOW, TARGET_PNL_HIGH, TARGET_PNL_PCT_LOW, TARGET_PNL_PCT_HIGH,
    MAX_DRAWDOWN_PCT, BASE_ORDER_SIZE_USDT, DEFAULT_LEVERAGE, DEFAULT_NUM_GRIDS,
)

logger = logging.getLogger("dry_run_engine")


def _smart_close_config_from_env(**overrides) -> SmartCloseConfig:
    """Build a SmartCloseConfig from env vars, allowing kwarg overrides."""
    def _f(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, default))
        except (TypeError, ValueError):
            return default

    cfg = SmartCloseConfig(
        time_decay_enabled=str(os.getenv("SMART_CLOSE_TIME_DECAY_ENABLED", "false")).lower()
        in ("1", "true", "yes", "on"),
        time_decay_hours=_f("SMART_CLOSE_TIME_DECAY_HOURS", 4.0),
        time_decay_min_loss_pct=_f("SMART_CLOSE_TIME_DECAY_MIN_LOSS_PCT", 1.0),
        momentum_exit_enabled=str(os.getenv("SMART_CLOSE_MOMENTUM_EXIT_ENABLED", "false")).lower()
        in ("1", "true", "yes", "on"),
        momentum_threshold_pct=_f("SMART_CLOSE_MOMENTUM_THRESHOLD_PCT", 2.5),
        momentum_window_sec=_f("SMART_CLOSE_MOMENTUM_WINDOW_SEC", 60.0),
        imbalance_close_enabled=str(os.getenv("SMART_CLOSE_IMBALANCE_CLOSE_ENABLED", "false")).lower()
        in ("1", "true", "yes", "on"),
        imbalance_ratio_threshold=_f("SMART_CLOSE_IMBALANCE_RATIO", 3.5),
        imbalance_min_fills=int(_f("SMART_CLOSE_IMBALANCE_MIN_FILLS", 8)),
        trailing_stop_enabled=str(os.getenv("SMART_CLOSE_TRAILING_STOP_ENABLED", "false")).lower()
        in ("1", "true", "yes", "on"),
        trailing_stop_initial_pct=_f("SMART_CLOSE_TRAILING_INITIAL_PCT", 3.5),
        trailing_stop_tightened_pct=_f("SMART_CLOSE_TRAILING_TIGHTENED_PCT", 2.5),
        trailing_stop_tighten_hours=_f("SMART_CLOSE_TRAILING_TIGHTEN_HOURS", 4.0),
        recovery_check_enabled=str(os.getenv("SMART_CLOSE_RECOVERY_CHECK_ENABLED", "false")).lower()
        in ("1", "true", "yes", "on"),
        recovery_min_depth_pct=_f("SMART_CLOSE_RECOVERY_MIN_DEPTH_PCT", 1.5),
        recovery_max_hours=_f("SMART_CLOSE_RECOVERY_MAX_HOURS", 6.0),
        min_seconds_since_last_fill=_f("SMART_CLOSE_POST_FILL_COOLDOWN_SEC", 180.0),
        recovery_window_sec=_f("SMART_CLOSE_RECOVERY_WINDOW_SEC", 300.0),
        recovery_partial_pct=_f("SMART_CLOSE_RECOVERY_PARTIAL_PCT", 30.0),
        # Hard floor calibrated to leverage-aware noise:
        # - 12% was too tight: at 50x leverage, 12% margin = 0.24% price move,
        #   well within ranging-market noise. Stops fired in 1-3 minutes on
        #   normal grid wobble.
        # - 20% was too loose: avg_loss reached -$2 vs avg_win +$0.30.
        # - 15-18% is the sweet spot: real distress without noise stops.
        # Combined with the post-scale-out fix (only unrealized counts after
        # the half-close), the recovery window has real room to work.
        hard_loss_pct_floor=_f("HARD_FLOOR_BASE_PCT", 40.0),
        hard_loss_pct_floor_min=_f("HARD_FLOOR_MIN_PCT", 30.0),
        hard_loss_pct_floor_max=_f("HARD_FLOOR_MAX_PCT", 55.0),
        scale_out_fraction=_f("SCALE_OUT_FRACTION", 0.5),
        # Patch J: post-scale-out cooldown (env-tunable).
        post_scale_out_cooldown_sec=_f("POST_SCALE_OUT_COOLDOWN_SEC", 60.0),
        min_position_age_sec=_f("SMART_CLOSE_MIN_POS_AGE_SEC", 90.0),
        imbalance_emergency_ratio=_f("IMBALANCE_EMERGENCY_RATIO", 4.0),
        imbalance_emergency_min_fills=int(_f("IMBALANCE_EMERGENCY_MIN_FILLS", 4)),
        imbalance_emergency_min_loss_pct=_f("IMBALANCE_EMERGENCY_MIN_LOSS_PCT", 8.0),
        imbalance_emergency_min_age_sec=_f("IMBALANCE_EMERGENCY_MIN_AGE_SEC", 60.0),
        tp_floor_pct=_f("DYNAMIC_TP_FLOOR_PCT", 3.0),
        tp_min_age_full_target_min=_f("DYNAMIC_TP_FULL_TARGET_MIN", 10.0),
        tp_decay_step_pct=_f("DYNAMIC_TP_DECAY_STEP_PCT", 2.0),
        tp_decay_to_zero_at_min=_f("DYNAMIC_TP_DECAY_TO_ZERO_MIN", 30.0),
        tp_dust_floor_usdt=_f("DYNAMIC_TP_DUST_FLOOR_USDT", 0.05),
        tp_momentum_max_age_min=_f("DYNAMIC_TP_MOMENTUM_MAX_AGE_MIN", 30.0),
        tp_momentum_velocity_pct_per_min=_f("DYNAMIC_TP_MOMENTUM_VELOCITY_PCT_PER_MIN", 1.5),
        tp_momentum_extend_max_pct=_f("DYNAMIC_TP_MOMENTUM_EXTEND_MAX_PCT", 5.0),
        tp_momentum_trailing_giveback_pct=_f("DYNAMIC_TP_TRAILING_GIVEBACK_PCT", 0.5),
        tp_min_fills=int(_f("DYNAMIC_TP_MIN_FILLS", 1)),
        # Patch D: env hooks for piecewise TP step curve (3/2/1/0 default).
        tp_step_curve_enabled=str(os.getenv("DYNAMIC_TP_STEP_CURVE", "true")).lower()
        in ("1", "true", "yes", "on"),
        tp_tier_1_min=_f("DYNAMIC_TP_TIER_1_MIN", 10.0),
        tp_tier_2_min=_f("DYNAMIC_TP_TIER_2_MIN", 15.0),
        tp_tier_3_min=_f("DYNAMIC_TP_TIER_3_MIN", 30.0),
        tp_tier_2_pct=_f("DYNAMIC_TP_TIER_2_PCT", 2.0),
        tp_tier_3_pct=_f("DYNAMIC_TP_TIER_3_PCT", 1.0),
        tp_trailing_enabled=str(os.getenv("TRAILING_PROFIT_ENABLED", "true")).lower()
        in ("1", "true", "yes", "on"),
        tp_trailing_threshold_pct=_f("TRAILING_PROFIT_THRESHOLD_PCT", 0.5),
        tp_trailing_retracement_pct=_f("TRAILING_PROFIT_RETRACEMENT_PCT", 0.2),
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@dataclass
class SimFill:
    """A simulated order fill — kept for backward compat with journal/API."""
    level_index: int
    side: str
    price: float
    qty: float
    timestamp: float
    sim_pnl: float = 0.0


@dataclass
class DryRunState:
    """Tracks the full dry-run simulation state."""
    grid: GridState
    started_at: float
    current_price: float = 0.0
    # Position — delegated to GridPosition
    position: GridPosition = field(default_factory=GridPosition)
    # Fill tracking
    fills: list[SimFill] = field(default_factory=list)
    is_active: bool = True
    filled_levels: set = field(default_factory=set)
    pending_fills: list[int] = field(default_factory=list)
    # Grid imbalance tracker
    imbalance: GridImbalance = field(default_factory=GridImbalance)
    # v3: recentering history
    recenter_count: int = 0
    last_recenter_at: float = 0.0
    # ATR % at deploy time (frozen for grid lifetime — drives hard floor)
    atr_pct: float = 0.0


class DryRunEngine:
    """
    v4 Dry-Run Engine with smart close logic.
    
    Uses grid_core for:
    - Position tracking (GridPosition)
    - Fill processing (process_fill)
    - Close conditions (check_close_conditions)
    - Smart negative close (SmartCloseEngine)
    """

    def __init__(self, adaptive_config: AdaptiveConfig = None, max_cycles: int = 1,
                 smart_close_config: SmartCloseConfig = None):
        self.state: Optional[DryRunState] = None
        self._tick_count = 0
        # Touch-fill tracker: tracks the full price range seen so any
        # level whose price has been reached fills immediately.
        self._min_seen_price: Optional[float] = None
        self._max_seen_price: Optional[float] = None
        self._drawdown_hold_alert_cooldown_seconds = float(
            os.getenv("DRAWDOWN_HOLD_ALERT_COOLDOWN_SECONDS", "60")
        )
        self._last_drawdown_hold_alert_at = 0.0
        # v3: Adaptive grid controller
        self._adaptive_config = adaptive_config or default_config()
        self._adaptive: Optional[AdaptiveGrid] = None
        self._last_adaptive_result: Optional[AdaptiveResult] = None
        # v4: Multi-cycle + smart close
        self._cycle_state = CycleState(max_cycles=max_cycles)
        self._smart_close = SmartCloseEngine(
            smart_close_config or _smart_close_config_from_env()
        )

    def deploy_grid(self, coin_score: CoinScore) -> DryRunState:
        """Create a simulated grid from a CoinScore."""
        from grid_engine import GridEngine
        engine = GridEngine()
        grid = engine.calculate_grid_levels(
            symbol=coin_score.symbol,
            upper=coin_score.suggested_upper,
            lower=coin_score.suggested_lower,
            num_grids=coin_score.suggested_grids,
            current_price=coin_score.price,
            leverage=coin_score.suggested_leverage,
            order_size_usdt=BASE_ORDER_SIZE_USDT,
            exp_sizing_gamma=self._adaptive_config.exp_sizing_gamma,
            progressive_sizing_enabled=self._adaptive_config.progressive_sizing_enabled,
            progressive_min_factor=self._adaptive_config.progressive_min_factor,
            progressive_max_factor=self._adaptive_config.progressive_max_factor,
            progressive_curve_power=self._adaptive_config.progressive_curve_power,
        )

        state = DryRunState(
            grid=grid,
            started_at=time.time(),
            current_price=coin_score.price,
            atr_pct=getattr(coin_score, "atr_pct", 0.0),
        )

        for level in grid.grid_levels:
            level.status = "placed"

        self.state = state
        
        # Reset touch-fill trackers for fresh grid deployment
        self._min_seen_price = None
        self._max_seen_price = None
        
        # v3: Initialize adaptive grid
        self._adaptive = AdaptiveGrid(
            config=self._adaptive_config,
            upper=coin_score.suggested_upper,
            lower=coin_score.suggested_lower,
            num_grids=coin_score.suggested_grids,
            base_order_size=BASE_ORDER_SIZE_USDT,
            leverage=coin_score.suggested_leverage,
        )
        
        # ── Sweep fill: immediately fill levels already crossed by price ──
        sweep_price = coin_score.price
        sweep_fills = 0
        for level in grid.grid_levels:
            if level.index in state.filled_levels:
                continue
            if level.side == "Buy" and sweep_price <= level.price:
                self._simulate_fill(level, sweep_price)
                sweep_fills += 1
            elif level.side == "Sell" and sweep_price >= level.price:
                self._simulate_fill(level, sweep_price)
                sweep_fills += 1
        if sweep_fills > 0:
            logger.info(f"   🧹 SWEEP FILL: {sweep_fills} levels filled on deploy @ ${sweep_price:.4f}")
        
        logger.info(f"🧪 DRY-RUN Grid deployed (v4): {grid.symbol} | "
                     f"{grid.lower_price:.4f}-{grid.upper_price:.4f} | "
                     f"{len(grid.grid_levels)} levels | lev={grid.leverage}x")
        logger.info(f"   💰 Margin: ${grid.order_size_usdt}/level | "
                    f"Target: {TARGET_PNL_PCT_LOW}-{TARGET_PNL_PCT_HIGH}% | "
                    f"Smart close: time_decay + momentum + imbalance + trailing")
        return state

    def on_price_update(self, price: float) -> Optional[str]:
        """
        Process a new price tick.
        
        Order: spike check → fill detection → recenter/trail → close checks → smart close
        """
        if not self.state or not self.state.is_active:
            return None

        old_price = self.state.current_price
        self.state.current_price = price
        self._tick_count += 1
        
        # Track full price range for touch-fill detection.
        # Even if price jumps over a level (no clean cross), the level
        # still fills because its price lies within the seen range.
        if self._min_seen_price is None or price < self._min_seen_price:
            self._min_seen_price = price
        if self._max_seen_price is None or price > self._max_seen_price:
            self._max_seen_price = price
        
        # Update smart close price history
        self._smart_close.update_price(price)
        
        # ── v3: Spike/Exposure checks ──
        if self._adaptive:
            result = self._adaptive.on_price(price)
            self._last_adaptive_result = result
            
            if result.action == "pause":
                logger.warning(f"⚡ SPIKE PAUSE | fills blocked for "
                             f"{self._adaptive_config.spike_cooldown_sec:.0f}s")
                self.state.position.update_unrealized(price)
                return None
            
            if result.action == "close_excess":
                logger.warning(
                    f"🛑 EXPOSURE CAP BREACHED! {result.close_side} side overloaded | "
                    f"consecutive={self._adaptive.exposure_cap.exposure.consecutive_same_side}"
                )
                self.state.is_active = False
                return "exposure_breach"
            
            if result.action == "freeze":
                logger.warning(f"❄️ GRID FROZEN | exposure cap hit")
                self.state.position.update_unrealized(price)
                return None
        
        # ── Fill Detection — cross + touch model ──
        # Cross: price crosses through a level (old was one side, new is the other)
        # Touch: price has ever reached this level (handles initial-tick fills
        #        for both sides, and fills when a recently-deployed grid's
        #        first price update is already past a level).
        event = None
        for level in self.state.grid.grid_levels:
            if level.index in self.state.filled_levels:
                continue

            filled = False
            if level.side == "Buy" and old_price > level.price >= price:
                filled = True
            elif level.side == "Sell" and old_price < level.price <= price:
                filled = True
            # Touch fill: if price has ever touched this level (any entry,
            # not just clean crosses), fill it immediately.
            elif level.side == "Buy" and self._min_seen_price is not None and self._min_seen_price <= level.price:
                filled = True
            elif level.side == "Sell" and self._max_seen_price is not None and self._max_seen_price >= level.price:
                filled = True
            
            if filled:
                if self._adaptive and not self._adaptive.exposure_cap.fills_allowed():
                    continue
                event = self._simulate_fill(level, price)
        
        # ── Recenter/trail AFTER fills ──
        if self._adaptive:
            result = self._last_adaptive_result
            if result.recentered:
                self._handle_recenter(result, price)
            if result.trail_shift is not None:
                self._handle_trail(result, price)

        # ── Standard close checks ──
        self.state.position.update_unrealized(price)
        margin = allocated_margin_usdt(
            self.state.grid.order_size_usdt, self.state.grid.num_grids
        )
        
        # `check_close_conditions` is called with HIGH for both target args so
        # `should_close` only fires as the upper-ceiling backstop (e.g. 8%).
        # The dynamic-TP path below handles every close below the ceiling.
        pnl_result = check_close_conditions(
            position=self.state.position,
            current_price=price,
            allocated_margin=margin,
            target_pnl_pct_low=TARGET_PNL_PCT_HIGH,
            target_pnl_pct_high=TARGET_PNL_PCT_HIGH,
            max_drawdown_pct=MAX_DRAWDOWN_PCT,
            total_fills=len(self.state.fills),
        )

        # ── Dynamic take-profit ──
        if pnl_result.total_pnl > 0 and not self.state.position.is_flat:
            tp_reason = self._smart_close.evaluate_take_profit(
                position=self.state.position,
                current_price=price,
                allocated_margin=margin,
                total_pnl=pnl_result.total_pnl,
                total_fills=len(self.state.fills),
            )
            if tp_reason:
                return self._handle_close(tp_reason.value, pnl_result.total_pnl)

        # Ceiling backstop — fires only at TARGET_PNL_PCT_HIGH.
        if pnl_result.should_close:
            return self._handle_close(pnl_result.close_reason, pnl_result.total_pnl)

        # ── Smart close on losing positions ──
        # Drawdown breaches are routed through SmartCloseEngine via the
        # `drawdown_breached` flag so the cooldown + recovery window apply
        # to them too. The hard floor (margin %, ATR-bucketed) triggers a
        # scale-out on first breach — half-close + recovery on the remainder
        # — and a full close on the next breach.
        if pnl_result.total_pnl < 0 and not self.state.position.is_flat:
            smart_reason = self._smart_close.check_smart_close(
                position=self.state.position,
                current_price=price,
                allocated_margin=margin,
                imbalance=self.state.imbalance,
                total_fills=len(self.state.fills),
                drawdown_breached=pnl_result.drawdown_breached,
                atr_pct=self.state.atr_pct,
            )
            if smart_reason == CloseReason.PARTIAL_CLOSE:
                # Half-close at the floor; keep grid alive for recovery.
                realised, qty = perform_partial_close(
                    self.state.position, self.state.imbalance, price,
                    fraction=self._smart_close.config.scale_out_fraction,
                )
                # Patch G: do NOT reset_recovery() here — keep the window
                # active so the remaining half can bounce instead of
                # immediately retripping the floor and closing flat.
                logger.warning(
                    f"⚖️  SCALE-OUT: closed {qty:.6f} @ ${price:.4f} | "
                    f"realised=${realised:.4f} | remaining qty={self.state.position.qty:.6f}"
                )
                # Record the partial as a fill for the dashboard.
                self.state.fills.append(SimFill(
                    level_index=-1, side=("Sell" if self.state.position.side == "Buy" else "Buy"),
                    price=price, qty=qty, timestamp=time.time(), sim_pnl=realised,
                ))
                return "partial_close"
            if smart_reason:
                logger.info(
                    f"🧠 SMART CLOSE: {smart_reason.value} | "
                    f"PnL=${pnl_result.total_pnl:.4f} | "
                    f"dd_breached={pnl_result.drawdown_breached}"
                )
                return self._handle_close(smart_reason.value, pnl_result.total_pnl)

        return event
    
    def _handle_close(self, reason: str, total_pnl: float) -> str:
        """Handle grid close — either final close or cycle reset."""
        if self._cycle_state.max_cycles > 1:
            cycle_done = self._cycle_state.complete_cycle(total_pnl)
            logger.info(
                f"🧪 CYCLE {self._cycle_state.cycles_completed}/{self._cycle_state.max_cycles} | "
                f"reason={reason} | PnL=${total_pnl:.4f} | "
                f"cumulative=${self._cycle_state.cumulative_pnl:.4f}"
            )
            
            if cycle_done:
                logger.info(f"🛑 MAX CYCLES REACHED — closing grid")
                self.state.is_active = False
                return reason
            else:
                self._reset_for_next_cycle(self.state.current_price)
                return "cycle_complete"
        else:
            logger.info(f"🧪 GRID CLOSE | reason={reason} | PnL=${total_pnl:.4f}")
            self.state.is_active = False
            return reason
    
    def _reset_for_next_cycle(self, price: float):
        """Reset grid for next trading cycle."""
        reset_position(self.state.position)
        reset_imbalance(self.state.imbalance)
        self._smart_close.reset_recovery()
        self._smart_close.reset_tp_peak()
        self.state.filled_levels.clear()
        
        for level in self.state.grid.grid_levels:
            level.status = "placed"
        
        if self._adaptive:
            self._adaptive.exposure_cap.reset()
        
        logger.info(
            f"🔄 GRID RESET for cycle {self._cycle_state.cycles_completed + 1}/{self._cycle_state.max_cycles} | "
            f"{len(self.state.grid.grid_levels)} levels reopened | "
            f"cumulative=${self._cycle_state.cumulative_pnl:.4f}"
        )
    
    def _simulate_fill(self, level: GridLevel, fill_price: float) -> str:
        """Simulate an order fill using grid_core.process_fill."""
        self.state.filled_levels.add(level.index)
        level.status = "filled"

        if self._adaptive:
            self._adaptive.record_fill(level.side, level.qty, level.index)

        fill = FillEvent(
            level_index=level.index,
            side=level.side,
            price=fill_price,
            qty=level.qty,
            timestamp=time.time(),
        )

        # Use grid_core to process fill
        pnl = process_fill(fill, self.state.position, self.state.imbalance)

        # Keep SimFill for backward compat
        sim_fill = SimFill(
            level_index=level.index,
            side=level.side,
            price=fill_price,
            qty=level.qty,
            timestamp=fill.timestamp,
            sim_pnl=pnl,
        )
        self.state.fills.append(sim_fill)

        total_pnl = self.state.position.realized_pnl + self.state.position.unrealized_pnl
        
        exposure_info = ""
        if self._adaptive:
            exp = self._adaptive.exposure_cap.exposure
            exposure_info = f" | exposure: buy={exp.buy_fills} sell={exp.sell_fills} streak={exp.consecutive_same_side}"
        
        logger.info(f"  💰 DRY FILL: {level.side} {level.qty:.6f} @ ${fill_price:.4f} | "
                     f"PnL=${pnl:.4f} | total=${total_pnl:.4f} | "
                     f"pos={self.state.position.side} {self.state.position.qty:.6f}{exposure_info}")

        level.status = "rebalanced"
        return "fill"

    def _handle_recenter(self, result: AdaptiveResult, price: float):
        """Handle grid recentering."""
        if not result.recenter_event:
            return
        
        event = result.recenter_event
        self.state.grid.upper_price = event.new_upper
        self.state.grid.lower_price = event.new_lower
        
        level_sizes = result.level_sizes or {}
        step = (event.new_upper - event.new_lower) / self.state.grid.num_grids
        new_levels = []
        for i in range(self.state.grid.num_grids + 1):
            price_lvl = event.new_lower + step * i
            if abs(price_lvl - price) < step * 0.3:
                continue
            side = "Buy" if price_lvl < price else "Sell"
            order_size = level_sizes.get(i, BASE_ORDER_SIZE_USDT)
            leverage = self.state.grid.leverage
            qty = (order_size * leverage) / price_lvl
            new_levels.append(GridLevel(index=i, price=price_lvl, side=side, qty=qty, status="placed"))
        
        self.state.grid.grid_levels = new_levels
        self.state.filled_levels.clear()
        self.state.recenter_count += 1
        self.state.last_recenter_at = time.time()
        if self._adaptive:
            self._adaptive.exposure_cap.reset()
        
        logger.info(f"🔄 RECENTERED | {len(new_levels)} levels | "
                    f"[{event.new_lower:.4f}-{event.new_upper:.4f}]")
    
    def _handle_trail(self, result: AdaptiveResult, price: float):
        """Handle grid trailing."""
        shift = result.trail_shift
        if shift is None:
            return
        
        for level in self.state.grid.grid_levels:
            level.price += shift
            level.status = "placed"
        
        self.state.grid.upper_price += shift
        self.state.grid.lower_price += shift
        self.state.filled_levels.clear()
        if self._adaptive:
            self._adaptive.exposure_cap.reset()
        
        logger.info(f"📈 TRAILED | shift={shift:+.4f}")

    def _allocated_margin_usdt(self) -> float:
        if not self.state:
            return 0.0
        level_count = len(self.state.grid.grid_levels) or self.state.grid.num_grids
        return self.state.grid.order_size_usdt * level_count

    def _target_pnl_low_usdt(self) -> float:
        if not self.state:
            return TARGET_PNL_LOW
        active_fills = max(len(self.state.fills), 2)
        filled_margin = self.state.grid.order_size_usdt * active_fills
        allocated = filled_margin if filled_margin > 0 else self._allocated_margin_usdt()
        if allocated <= 0:
            return TARGET_PNL_LOW
        return allocated * TARGET_PNL_PCT_LOW / 100

    def _target_pnl_high_usdt(self) -> float:
        allocated = self._allocated_margin_usdt()
        if allocated <= 0:
            return TARGET_PNL_HIGH
        return allocated * TARGET_PNL_PCT_HIGH / 100

    def _drawdown_limit_usdt(self) -> float:
        allocated = self._allocated_margin_usdt()
        if allocated <= 0:
            allocated = BASE_ORDER_SIZE_USDT * DEFAULT_NUM_GRIDS
        return allocated * MAX_DRAWDOWN_PCT / 100

    def _should_log_drawdown_hold(self, now: float) -> bool:
        cooldown = max(0.0, self._drawdown_hold_alert_cooldown_seconds)
        if self._last_drawdown_hold_alert_at <= 0 or now - self._last_drawdown_hold_alert_at >= cooldown:
            self._last_drawdown_hold_alert_at = now
            return True
        return False

    def get_status(self) -> dict:
        """Get current dry-run status with v4 smart close info."""
        if not self.state:
            return {"active": False}
        s = self.state
        pos = s.position
        total = pos.realized_pnl + pos.unrealized_pnl
        duration = time.time() - s.started_at
        
        status = {
            "active": s.is_active,
            "dry_run": True,
            "grid_id": s.grid.grid_id,
            "symbol": s.grid.symbol,
            "current_price": s.current_price,
            "upper": s.grid.upper_price,
            "lower": s.grid.lower_price,
            "leverage": s.grid.leverage,
            "num_grids": s.grid.num_grids,
            "position_side": pos.side,
            "position_qty": round(pos.qty, 6),
            "entry_price": pos.entry_price,
            "realized_pnl": round(pos.realized_pnl, 4),
            "unrealized_pnl": round(pos.unrealized_pnl, 4),
            "total_pnl": round(total, 4),
            "fills": len(s.fills),
            "filled_levels": len(s.filled_levels),
            "cycles_completed": self._cycle_state.cycles_completed,
            "max_cycles": self._cycle_state.max_cycles,
            "position_age_hours": round(pos.age_hours, 1),
            "imbalance_ratio": round(s.imbalance.imbalance_ratio, 2),
            "fill_log": [
                {
                    "side": getattr(f, "side", "unknown"),
                    "price": getattr(f, "price", 0.0),
                    "qty": getattr(f, "qty", 0.0),
                    "timestamp": getattr(f, "timestamp", 0.0),
                    "pnl": getattr(f, "sim_pnl", 0.0),
                }
                for f in s.fills[-50:]
            ],
            "grid_levels": [
                {
                    "index": lv.index,
                    "price": lv.price,
                    "side": lv.side,
                    "status": lv.status,
                    "qty": float(lv.qty),
                    "entry_notional": round(float(lv.qty) * float(lv.price), 6),
                    "margin_usdt": round(float(lv.qty) * float(lv.price) / s.grid.leverage, 6),
                }
                for lv in s.grid.grid_levels
            ],
            "duration_sec": round(duration, 1),
            "allocated_margin_usdt": round(self._allocated_margin_usdt(), 4),
            "target_pnl_low": round(self._target_pnl_low_usdt(), 4),
            "target_pnl_high": round(self._target_pnl_high_usdt(), 4),
            "target_pnl_pct_low": TARGET_PNL_PCT_LOW,
            "target_pnl_pct_high": TARGET_PNL_PCT_HIGH,
        }
        
        if self._adaptive:
            adaptive_status = self._adaptive.status
            status["v3_adaptive"] = {
                "spike_active": adaptive_status["spike_active"],
                "spike_direction": adaptive_status["spike_state"]["direction"],
                "exposure_buy_fills": adaptive_status["exposure"]["buy_fills"],
                "exposure_sell_fills": adaptive_status["exposure"]["sell_fills"],
                "exposure_consecutive": adaptive_status["exposure"]["consecutive_same_side"],
                "exposure_breached": adaptive_status["exposure"]["breached"],
                "imbalance_ratio": adaptive_status["exposure"]["imbalance_ratio"],
                "recenters": adaptive_status["recenters"],
                "range_pct": adaptive_status["range_pct"],
                "trailing_enabled": adaptive_status["trailing_enabled"],
            }
            status["upper"] = adaptive_status["upper"]
            status["lower"] = adaptive_status["lower"]
        
        return status
