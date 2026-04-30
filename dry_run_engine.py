"""
Dry-Run Engine — simulates grid trading without placing real orders.
Uses live market data from WebSocket but simulates fills, PnL, and position tracking.
"""

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from coin_scanner import CoinScore
from grid_engine import GridState, GridLevel

from config import (
    TARGET_PNL_LOW, TARGET_PNL_HIGH, TARGET_PNL_PCT_LOW, TARGET_PNL_PCT_HIGH,
    MAX_DRAWDOWN_PCT, BASE_ORDER_SIZE_USDT, DEFAULT_LEVERAGE, DEFAULT_NUM_GRIDS,
)

logger = logging.getLogger("dry_run_engine")


@dataclass
class SimFill:
    """A simulated order fill."""
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
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    position_qty: float = 0.0
    position_side: str = ""
    entry_price: float = 0.0
    fills: list[SimFill] = field(default_factory=list)
    is_active: bool = True
    # Grid tracking: which levels have been "filled"
    filled_levels: set = field(default_factory=set)
    # Simulated open orders that become "pending fills"
    pending_fills: list[int] = field(default_factory=list)


class DryRunEngine:
    """
    Simulates the full grid trading loop using real-time price data.
    No real orders are placed — everything is simulated.
    Includes spike detection: monitors price velocity and one-sided fill count.
    """

    def __init__(self):
        self.state: Optional[DryRunState] = None
        self._tick_count = 0
        self._recent_prices: list[tuple[float, float]] = []  # (timestamp, price)
        self._spike_threshold_pct = float(os.getenv("SPIKE_THRESHOLD_PCT", "1.0"))  # 1% move in 60s = spike
        self._onesided_fill_limit = int(os.getenv("ONESIDED_FILL_LIMIT", "3"))  # 3 same-side fills in a row = danger
        self._last_fill_side: Optional[str] = None
        self._consecutive_same_side = 0
        self._drawdown_hold_alert_cooldown_seconds = float(
            os.getenv("DRAWDOWN_HOLD_ALERT_COOLDOWN_SECONDS", "60")
        )
        self._last_drawdown_hold_alert_at = 0.0

    def deploy_grid(self, coin_score: CoinScore) -> DryRunState:
        """
        Create a simulated grid from a CoinScore.
        Calculates grid levels but does NOT place real orders.
        """
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
        )

        state = DryRunState(
            grid=grid,
            started_at=time.time(),
            current_price=coin_score.price,
        )

        # All grid levels start as "simulated placed"
        for level in grid.grid_levels:
            level.status = "placed"

        self.state = state
        logger.info(f"🧪 DRY-RUN Grid deployed: {grid.symbol} | "
                     f"{grid.lower_price:.4f}-{grid.upper_price:.4f} | "
                     f"{len(grid.grid_levels)} levels | lev={grid.leverage}x")
        logger.info(f"   💰 Margin: ${grid.order_size_usdt}/level | "
                    f"Target: {TARGET_PNL_PCT_LOW}-{TARGET_PNL_PCT_HIGH}% of allocated margin")
        return state

    def on_price_update(self, price: float) -> Optional[str]:
        """
        Process a new price tick. Simulates grid fills when price crosses a level.
        Returns event type if something happened, None otherwise.
        """
        if not self.state or not self.state.is_active:
            return None

        old_price = self.state.current_price
        self.state.current_price = price
        self._tick_count += 1

        # ── Spike Detection ──────────────────────────────────────
        # Track recent prices for velocity calculation
        now = time.time()
        self._recent_prices.append((now, price))
        # Keep only last 120 seconds of prices
        self._recent_prices = [(t, p) for t, p in self._recent_prices if now - t <= 120]

        # Calculate price velocity (% change over last 60s)
        spike_detected = False
        spike_direction = ""
        if len(self._recent_prices) >= 2:
            # Find price from ~60 seconds ago
            old_ts, old_p = self._recent_prices[0]
            if now - old_ts >= 30:  # at least 30s of data
                velocity_pct = (price - old_p) / old_p * 100
                if abs(velocity_pct) >= self._spike_threshold_pct:
                    spike_detected = True
                    spike_direction = "up" if velocity_pct > 0 else "down"
                    logger.warning(
                        f"⚡ SPIKE DETECTED! {velocity_pct:+.2f}% in {now - old_ts:.0f}s | "
                        f"direction={spike_direction}"
                    )

        # ── One-Sided Fill Danger ────────────────────────────────
        # If too many fills on the same side, trend may be breaking out
        onesided_danger = False
        if self._consecutive_same_side >= self._onesided_fill_limit:
            onesided_danger = True
            logger.warning(
                f"⚠️ ONE-SIDED DANGER! {self._consecutive_same_side} consecutive "
                f"{self._last_fill_side} fills — possible breakout"
            )

        # If spike + one-sided danger → emergency close
        if spike_detected and onesided_danger:
            logger.error(
                f"🚨 EMERGENCY CLOSE! Spike {spike_direction} + "
                f"{self._consecutive_same_side} same-side fills → grid broken"
            )
            self.state.is_active = False
            return "spike_close"

        event = None

        # Check each grid level for simulated fills
        for level in self.state.grid.grid_levels:
            if level.index in self.state.filled_levels:
                continue  # already filled

            # Price crossed this level?
            if level.side == "Buy" and old_price > level.price >= price:
                # Price dropped through buy level → simulate buy fill
                event = self._simulate_fill(level, price)
            elif level.side == "Sell" and old_price < level.price <= price:
                # Price rose through sell level → simulate sell fill
                event = self._simulate_fill(level, price)
            elif level.side == "Buy" and price <= level.price and self._tick_count <= 2:
                # Price already below this level at deploy → instant fill
                if level.index not in self.state.filled_levels:
                    event = self._simulate_fill(level, price)

        # Update unrealized PnL
        if self.state.position_qty > 0 and self.state.entry_price > 0:
            if self.state.position_side == "Buy":
                self.state.unrealized_pnl = (price - self.state.entry_price) * self.state.position_qty
            else:
                self.state.unrealized_pnl = (self.state.entry_price - price) * self.state.position_qty

        # Check if percentage-based target PnL hit.
        # Target is a % of margin allocated across active grid levels, not a static $1.
        total = self.state.realized_pnl + self.state.unrealized_pnl
        target_low = self._target_pnl_low_usdt()
        if total >= target_low and len(self.state.fills) >= 2:
            logger.info(
                f"🧪 DRY-RUN TARGET HIT! Total PnL=${total:.4f} "
                f">= {TARGET_PNL_PCT_LOW:.2f}% target (${target_low:.4f})"
            )
            self.state.is_active = False
            return "target_hit"

        # Check drawdown as % of allocated margin.
        drawdown_limit = self._drawdown_limit_usdt()
        if total < 0 and abs(total) > drawdown_limit:
            if self._should_log_drawdown_hold(now):
                logger.warning(
                    f"🧪 DRY-RUN DRAWDOWN HOLD! Total PnL = ${total:.4f}; "
                    "position remains open until PnL recovers positive"
                )
            return None

        return event

    def _simulate_fill(self, level: GridLevel, fill_price: float) -> str:
        """Simulate an order fill at a grid level."""
        self.state.filled_levels.add(level.index)
        level.status = "filled"

        # Track one-sided fills for spike detection
        if level.side == self._last_fill_side:
            self._consecutive_same_side += 1
        else:
            self._consecutive_same_side = 1
            self._last_fill_side = level.side

        fill = SimFill(
            level_index=level.index,
            side=level.side,
            price=fill_price,
            qty=level.qty,
            timestamp=time.time(),
        )

        # Calculate simulated PnL from this fill
        sim_pnl = 0.0
        if self.state.position_qty > 0 and self.state.position_side != level.side:
            # Closing trade — realize PnL
            if self.state.position_side == "Buy":
                sim_pnl = (fill_price - self.state.entry_price) * min(level.qty, self.state.position_qty)
            else:
                sim_pnl = (self.state.entry_price - fill_price) * min(level.qty, self.state.position_qty)
            self.state.realized_pnl += sim_pnl

            # Reduce position
            self.state.position_qty -= level.qty
            if self.state.position_qty <= 0:
                self.state.position_qty = 0
                self.state.entry_price = 0
                self.state.position_side = ""
        else:
            # Opening or adding to position
            if self.state.position_qty > 0:
                # Average entry
                total_cost = self.state.entry_price * self.state.position_qty + fill_price * level.qty
                self.state.position_qty += level.qty
                self.state.entry_price = total_cost / self.state.position_qty
            else:
                self.state.position_qty = level.qty
                self.state.entry_price = fill_price
                self.state.position_side = level.side

        fill.sim_pnl = sim_pnl
        self.state.fills.append(fill)

        total_pnl = self.state.realized_pnl + self.state.unrealized_pnl
        logger.info(f"  💰 DRY FILL: {level.side} {level.qty} @ ${fill_price:.4f} | "
                     f"simPnL=${sim_pnl:.4f} | totalPnL=${total_pnl:.4f} | "
                     f"pos={self.state.position_side} {self.state.position_qty:.6f}")

        # After a fill, re-activate this level on the opposite side (grid rebalance)
        # In a real grid: when buy fills, place a sell at same level (and vice versa)
        # For dry-run, we just mark it as rebalanced
        level.status = "rebalanced"

        return "fill"

    def _allocated_margin_usdt(self) -> float:
        """Margin allocated to this grid across live grid levels."""
        if not self.state:
            return 0.0
        level_count = len(self.state.grid.grid_levels) or self.state.grid.num_grids
        return self.state.grid.order_size_usdt * level_count

    def _target_pnl_low_usdt(self) -> float:
        """Minimum close target in USDT, based on % of actively filled margin."""
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
        """Throttle repetitive drawdown-hold warnings while keeping the hold active."""
        cooldown = max(0.0, self._drawdown_hold_alert_cooldown_seconds)
        if self._last_drawdown_hold_alert_at <= 0 or now - self._last_drawdown_hold_alert_at >= cooldown:
            self._last_drawdown_hold_alert_at = now
            return True
        return False

    def get_status(self) -> dict:
        """Get current dry-run status."""
        if not self.state:
            return {"active": False}
        s = self.state
        total = s.realized_pnl + s.unrealized_pnl
        duration = time.time() - s.started_at
        return {
            "active": s.is_active,
            "dry_run": True,
            "grid_id": s.grid.grid_id,
            "symbol": s.grid.symbol,
            "current_price": s.current_price,
            "upper": s.grid.upper_price,
            "lower": s.grid.lower_price,
            "leverage": s.grid.leverage,
            "num_grids": s.grid.num_grids,
            "position_side": s.position_side,
            "position_qty": round(s.position_qty, 6),
            "entry_price": s.entry_price,
            "realized_pnl": round(s.realized_pnl, 4),
            "unrealized_pnl": round(s.unrealized_pnl, 4),
            "total_pnl": round(total, 4),
        "fills": len(s.fills),
        "filled_levels": len(s.filled_levels),
        "fill_log": [
            {
                "side": getattr(f, "side", "unknown"),
                "price": getattr(f, "price", 0.0),
                "qty": getattr(f, "qty", 0.0),
                "timestamp": getattr(f, "timestamp", 0.0),
                "pnl": getattr(f, "sim_pnl", 0.0),
            }
            for f in s.fills[-50:]  # last 50 fills
        ],
        "grid_levels": [
            {
                "index": lv.index,
                "price": lv.price,
                "side": lv.side,
                "status": lv.status,
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
