"""
Grid Core — Shared trading logic for dry-run and live engines.

Extracted from dry_run_engine.py and live_engine.py to eliminate duplication.
Zero exchange dependencies — pure logic only.

Includes smart negative-close logic:
- Time-based position decay (positions lose recovery probability over time)
- Momentum-based exit (accelerating price = cut losses)
- Grid imbalance detection (too many same-side fills = close losers)
- Trailing stop on underwater positions (tighten stop as time passes)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum

logger = logging.getLogger("grid_core")


# ── Shared Dataclasses ─────────────────────────────────────────

@dataclass
class GridPosition:
    """Unified position tracking — used by both dry-run and live engines."""
    side: str = ""          # "Buy" or "Sell"
    qty: float = 0.0
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    opened_at: float = 0.0  # timestamp when position opened
    
    @property
    def is_flat(self) -> bool:
        return self.qty <= 0 or not self.side
    
    @property
    def age_seconds(self) -> float:
        if self.opened_at <= 0:
            return 0.0
        return time.time() - self.opened_at
    
    @property
    def age_hours(self) -> float:
        return self.age_seconds / 3600
    
    def update_unrealized(self, price: float) -> float:
        """Update and return unrealized PnL."""
        if self.is_flat or self.entry_price <= 0:
            self.unrealized_pnl = 0.0
            return 0.0
        if self.side == "Buy":
            self.unrealized_pnl = (price - self.entry_price) * self.qty
        else:
            self.unrealized_pnl = (self.entry_price - price) * self.qty
        return self.unrealized_pnl


@dataclass
class FillEvent:
    """A fill event — unified across dry-run and live."""
    level_index: int
    side: str           # "Buy" or "Sell"
    price: float
    qty: float
    timestamp: float
    order_id: str = ""
    pnl_from_fill: float = 0.0


@dataclass
class PnLResult:
    """Result of checking close conditions."""
    should_close: bool = False
    close_reason: str = ""      # "target_hit", "drawdown", "time_decay", "momentum_exit", "grid_imbalance"
    total_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    target_usdt: float = 0.0
    drawdown_limit: float = 0.0


@dataclass
class CycleState:
    """Multi-cycle grid state."""
    cycles_completed: int = 0
    max_cycles: int = 1
    cumulative_pnl: float = 0.0
    
    @property
    def remaining_cycles(self) -> int:
        return max(0, self.max_cycles - self.cycles_completed)
    
    def complete_cycle(self, pnl: float) -> bool:
        """Record cycle completion. Returns True if max cycles reached."""
        self.cycles_completed += 1
        self.cumulative_pnl += pnl
        return self.cycles_completed >= self.max_cycles


@dataclass
class GridImbalance:
    """Tracks buy/sell fill imbalance for a grid."""
    buy_fills: int = 0
    sell_fills: int = 0
    consecutive_same_side: int = 0
    last_side: str = ""
    
    @property
    def imbalance_ratio(self) -> float:
        """Ratio of dominant side fills. 1.0 = balanced, >2.0 = heavily imbalanced."""
        total = self.buy_fills + self.sell_fills
        if total == 0:
            return 1.0
        return max(self.buy_fills, self.sell_fills) / max(min(self.buy_fills, self.sell_fills), 1)
    
    @property
    def dominant_side(self) -> str:
        if self.buy_fills > self.sell_fills:
            return "Buy"
        elif self.sell_fills > self.buy_fills:
            return "Sell"
        return ""
    
    def record_fill(self, side: str):
        if side == "Buy":
            self.buy_fills += 1
        else:
            self.sell_fills += 1
        
        if side == self.last_side:
            self.consecutive_same_side += 1
        else:
            self.consecutive_same_side = 1
        self.last_side = side


# ── Smart Close Logic ──────────────────────────────────────────

class CloseReason(Enum):
    TARGET_HIT = "target_hit"
    DRAWDOWN = "drawdown"
    TIME_DECAY = "time_decay"
    MOMENTUM_EXIT = "momentum_exit"
    GRID_IMBALANCE = "grid_imbalance"
    SPIKE_CLOSE = "spike_close"
    EXPOSURE_BREACH = "exposure_breach"


@dataclass
class SmartCloseConfig:
    """Configuration for smart negative-close logic."""
    # Time-based decay
    time_decay_enabled: bool = True
    time_decay_hours: float = 8.0        # Close if negative for this long
    time_decay_min_loss_pct: float = 1.0 # Minimum loss % to trigger time decay
    
    # Momentum-based exit
    momentum_exit_enabled: bool = True
    momentum_window_sec: float = 60.0    # Look at last 60s of price movement
    momentum_threshold_pct: float = 2.0  # 2% move in window = trend acceleration
    
    # Grid imbalance
    imbalance_close_enabled: bool = True
    imbalance_ratio_threshold: float = 3.0  # 3:1 buy:sell = close losers
    imbalance_min_fills: int = 6             # Need at least 6 fills before checking
    
    # Trailing stop on underwater positions
    trailing_stop_enabled: bool = True
    trailing_stop_initial_pct: float = 3.0   # Initial stop at -3%
    trailing_stop_tighten_hours: float = 4.0 # After 4 hours, tighten
    trailing_stop_tightened_pct: float = 2.0 # Tightened stop at -2%
    
    # Recovery probability (learned from data)
    recovery_check_enabled: bool = True
    recovery_min_depth_pct: float = 1.5  # Only check if position is >1.5% underwater
    recovery_max_hours: float = 6.0      # If >6 hours at this depth, unlikely to recover


class SmartCloseEngine:
    """
    Analyzes positions and price history to determine if a losing position
    should be closed instead of held.
    
    Key insight: Grid bots have ~37% win rate in trending markets. The current
    "hold until positive" strategy works 63% of the time but the 37% that fail
    can drain the account. Smart close logic cuts the losers that are unlikely
    to recover, improving overall win rate.
    """
    
    def __init__(self, config: SmartCloseConfig = None):
        self.config = config or SmartCloseConfig()
        self._price_history: List[Tuple[float, float]] = []  # (timestamp, price)
        self._position_depth_history: Dict[str, List[Tuple[float, float]]] = {}  # side -> [(time, depth_pct)]
    
    def update_price(self, price: float, timestamp: float = None):
        """Record price for momentum analysis."""
        ts = timestamp or time.time()
        self._price_history.append((ts, price))
        # Keep last 5 minutes of prices
        cutoff = ts - 300
        self._price_history = [(t, p) for t, p in self._price_history if t > cutoff]
    
    def check_smart_close(
        self,
        position: GridPosition,
        current_price: float,
        allocated_margin: float,
        imbalance: GridImbalance,
        total_fills: int,
    ) -> Optional[CloseReason]:
        """
        Check if a losing position should be closed based on smart logic.
        
        Returns CloseReason if should close, None if should hold.
        Only triggers for POSITIONS THAT ARE NEGATIVE.
        """
        if position.is_flat:
            return None
        
        # Calculate current loss
        if position.side == "Buy":
            loss_pct = (position.entry_price - current_price) / position.entry_price * 100
        else:
            loss_pct = (current_price - position.entry_price) / position.entry_price * 100
        
        # Only apply to losing positions
        if loss_pct <= 0:
            return None
        
        # Track depth over time
        self._track_depth(position.side, loss_pct)
        
        # ── Check 1: Time-based decay ──
        if self.config.time_decay_enabled:
            reason = self._check_time_decay(position, loss_pct)
            if reason:
                return reason
        
        # ── Check 2: Momentum exit ──
        if self.config.momentum_exit_enabled:
            reason = self._check_momentum(position, current_price)
            if reason:
                return reason
        
        # ── Check 3: Grid imbalance ──
        if self.config.imbalance_close_enabled:
            reason = self._check_imbalance(position, imbalance, total_fills)
            if reason:
                return reason
        
        # ── Check 4: Trailing stop on underwater ──
        if self.config.trailing_stop_enabled:
            reason = self._check_trailing_stop(position, loss_pct)
            if reason:
                return reason
        
        # ── Check 5: Recovery probability ──
        if self.config.recovery_check_enabled:
            reason = self._check_recovery_probability(position, loss_pct)
            if reason:
                return reason
        
        return None
    
    def _track_depth(self, side: str, depth_pct: float):
        """Track position depth over time for recovery analysis."""
        now = time.time()
        if side not in self._position_depth_history:
            self._position_depth_history[side] = []
        self._position_depth_history[side].append((now, depth_pct))
        # Keep last 24 hours
        cutoff = now - 86400
        self._position_depth_history[side] = [
            (t, d) for t, d in self._position_depth_history[side] if t > cutoff
        ]
    
    def _check_time_decay(self, position: GridPosition, loss_pct: float) -> Optional[CloseReason]:
        """
        Time decay: If position has been negative for too long, close it.
        
        Logic: Grid positions should bounce back quickly (within hours). If a
        position has been negative for 8+ hours with >1% loss, the market has
        moved against us and the grid level is no longer valid.
        """
        if position.age_hours < self.config.time_decay_hours:
            return None
        
        if loss_pct < self.config.time_decay_min_loss_pct:
            return None
        
        logger.info(
            f"⏰ TIME DECAY: {position.side} position negative for "
            f"{position.age_hours:.1f}h with {loss_pct:.2f}% loss"
        )
        return CloseReason.TIME_DECAY
    
    def _check_momentum(self, position: GridPosition, current_price: float) -> Optional[CloseReason]:
        """
        Momentum exit: If price is accelerating against us, close.
        
        Logic: Grid levels should act as support/resistance. If price is
        accelerating through levels (momentum increasing), the grid structure
        has broken and we should cut losses before more levels fill.
        """
        if len(self._price_history) < 10:
            return None
        
        window = self.config.momentum_window_sec
        now = self._price_history[-1][0]
        recent = [(t, p) for t, p in self._price_history if t > now - window]
        
        if len(recent) < 5:
            return None
        
        # Calculate price velocity (% per second)
        first_price = recent[0][1]
        last_price = recent[-1][1]
        time_span = recent[-1][0] - recent[0][0]
        
        if time_span <= 0 or first_price <= 0:
            return None
        
        velocity_pct = abs((last_price - first_price) / first_price) * 100
        
        # Check if velocity is against our position
        if position.side == "Buy" and last_price < first_price:
            # Price dropping against long
            if velocity_pct >= self.config.momentum_threshold_pct:
                logger.info(
                    f"📉 MOMENTUM EXIT: {position.side} — price velocity "
                    f"{velocity_pct:.2f}% in {window:.0f}s (threshold {self.config.momentum_threshold_pct}%)"
                )
                return CloseReason.MOMENTUM_EXIT
        elif position.side == "Sell" and last_price > first_price:
            # Price rising against short
            if velocity_pct >= self.config.momentum_threshold_pct:
                logger.info(
                    f"📈 MOMENTUM EXIT: {position.side} — price velocity "
                    f"{velocity_pct:.2f}% in {window:.0f}s (threshold {self.config.momentum_threshold_pct}%)"
                )
                return CloseReason.MOMENTUM_EXIT
        
        return None
    
    def _check_imbalance(
        self,
        position: GridPosition,
        imbalance: GridImbalance,
        total_fills: int,
    ) -> Optional[CloseReason]:
        """
        Grid imbalance: If too many same-side fills, close the losing side.
        
        Logic: A balanced grid has roughly equal buy/sell fills. When one side
        dominates 3:1+, the grid is no longer functioning as intended — it's
        become a directional bet. Close the losing side positions to restore balance.
        """
        if total_fills < self.config.imbalance_min_fills:
            return None
        
        if imbalance.imbalance_ratio < self.config.imbalance_ratio_threshold:
            return None
        
        # Only close if this position is on the overloaded side
        if position.side == imbalance.dominant_side:
            # This position is on the overloaded side — close it
            logger.info(
                f"⚖️ GRID IMBALANCE: {position.side} side overloaded "
                f"(ratio {imbalance.imbalance_ratio:.1f}:1, "
                f"buy={imbalance.buy_fills} sell={imbalance.sell_fills})"
            )
            return CloseReason.GRID_IMBALANCE
        
        return None
    
    def _check_trailing_stop(self, position: GridPosition, loss_pct: float) -> Optional[CloseReason]:
        """
        Trailing stop on underwater positions: Tighten stop as time passes.
        
        Logic: A -2% position that recovers in 1 hour is fine. A -2% position
        that's been underwater for 4 hours is unlikely to recover. Tighten the
        stop loss over time.
        """
        if position.age_hours < 1:
            return None
        
        # Determine stop level based on age
        if position.age_hours >= self.config.trailing_stop_tighten_hours:
            stop_pct = self.config.trailing_stop_tightened_pct
        else:
            # Linear interpolation between initial and tightened
            progress = position.age_hours / self.config.trailing_stop_tighten_hours
            stop_pct = self.config.trailing_stop_initial_pct - progress * (
                self.config.trailing_stop_initial_pct - self.config.trailing_stop_tightened_pct
            )
        
        if loss_pct >= stop_pct:
            logger.info(
                f"🔒 TRAILING STOP: {position.side} — loss {loss_pct:.2f}% >= "
                f"stop {stop_pct:.2f}% (age {position.age_hours:.1f}h)"
            )
            return CloseReason.GRID_IMBALANCE  # Reuse reason code
        
        return None
    
    def _check_recovery_probability(self, position: GridPosition, loss_pct: float) -> Optional[CloseReason]:
        """
        Recovery probability: If position has been deeply underwater for too long,
        the probability of recovery drops below acceptable threshold.
        
        Logic: Track how long a position has been at each depth level. If a -1.5%
        position has been there for 6+ hours, historically only ~20% recover.
        Close to prevent further damage.
        """
        if loss_pct < self.config.recovery_min_depth_pct:
            return None
        
        if position.age_hours < self.config.recovery_max_hours:
            return None
        
        # Check if we've been at this depth for a while
        depth_history = self._position_depth_history.get(position.side, [])
        if len(depth_history) < 10:
            return None
        
        # Calculate time spent at current depth or deeper
        deep_time = 0
        for i in range(1, len(depth_history)):
            t_prev, d_prev = depth_history[i - 1]
            t_curr, d_curr = depth_history[i]
            if d_curr >= self.config.recovery_min_depth_pct:
                deep_time += t_curr - t_prev
        
        deep_hours = deep_time / 3600
        
        if deep_hours >= self.config.recovery_max_hours:
            logger.info(
                f"📊 RECOVERY LOW: {position.side} — {loss_pct:.2f}% deep for "
                f"{deep_hours:.1f}h (threshold {self.config.recovery_max_hours}h)"
            )
            return CloseReason.TIME_DECAY
        
        return None


# ── Shared Fill Processing ─────────────────────────────────────

def process_fill(
    fill: FillEvent,
    position: GridPosition,
    imbalance: GridImbalance,
) -> float:
    """
    Process a fill event and update position state.
    
    Returns the PnL from this fill (positive = profit, negative = loss).
    Updates position in-place.
    """
    # Record in imbalance tracker
    imbalance.record_fill(fill.side)
    
    pnl = 0.0
    
    if position.is_flat:
        # Opening new position
        position.side = fill.side
        position.qty = fill.qty
        position.entry_price = fill.price
        position.opened_at = fill.timestamp
        position.unrealized_pnl = 0.0
    
    elif position.side == fill.side:
        # Adding to existing position (averaging)
        total_cost = position.entry_price * position.qty + fill.price * fill.qty
        position.qty += fill.qty
        position.entry_price = total_cost / position.qty
    
    else:
        # Closing or reducing position (opposite side fill)
        close_qty = min(fill.qty, position.qty)
        
        if position.side == "Buy":
            pnl = (fill.price - position.entry_price) * close_qty
        else:
            pnl = (position.entry_price - fill.price) * close_qty
        
        position.realized_pnl += pnl
        position.qty -= close_qty
        
        if position.qty <= 0:
            # Fully closed
            position.qty = 0.0
            position.side = ""
            position.entry_price = 0.0
            position.unrealized_pnl = 0.0
            position.opened_at = 0.0
        # If remaining qty > 0, position stays open with same entry
    
    fill.pnl_from_fill = pnl
    return pnl


def check_close_conditions(
    position: GridPosition,
    current_price: float,
    allocated_margin: float,
    target_pnl_pct_low: float,
    target_pnl_pct_high: float,
    max_drawdown_pct: float,
    min_fills: int = 2,
    total_fills: int = 0,
) -> PnLResult:
    """
    Check standard close conditions (target hit, drawdown).
    
    This is the CORE close logic shared between dry-run and live.
    """
    position.update_unrealized(current_price)
    total_pnl = position.realized_pnl + position.unrealized_pnl
    
    result = PnLResult(
        total_pnl=total_pnl,
        realized_pnl=position.realized_pnl,
        unrealized_pnl=position.unrealized_pnl,
    )
    
    if allocated_margin <= 0:
        return result
    
    # Target PnL check
    target_low = allocated_margin * target_pnl_pct_low / 100
    target_high = allocated_margin * target_pnl_pct_high / 100
    result.target_usdt = target_low
    
    if total_pnl >= target_low and total_fills >= min_fills:
        result.should_close = True
        result.close_reason = CloseReason.TARGET_HIT.value
        return result
    
    # Drawdown check
    drawdown_limit = allocated_margin * max_drawdown_pct / 100
    result.drawdown_limit = drawdown_limit
    
    if total_pnl < 0 and abs(total_pnl) > drawdown_limit:
        result.should_close = True
        result.close_reason = CloseReason.DRAWDOWN.value
        return result
    
    return result


# ── Cycle Reset ────────────────────────────────────────────────

def reset_position(position: GridPosition):
    """Reset position for next cycle."""
    position.side = ""
    position.qty = 0.0
    position.entry_price = 0.0
    position.unrealized_pnl = 0.0
    position.opened_at = 0.0


def reset_imbalance(imbalance: GridImbalance):
    """Reset imbalance tracker for next cycle."""
    imbalance.buy_fills = 0
    imbalance.sell_fills = 0
    imbalance.consecutive_same_side = 0
    imbalance.last_side = ""


# ── Margin Calculations ────────────────────────────────────────

def allocated_margin_usdt(order_size_usdt: float, num_grids: int) -> float:
    """Calculate allocated margin for a grid."""
    return order_size_usdt * num_grids


def target_pnl_usdt(order_size_usdt: float, num_grids: int, target_pct: float) -> float:
    """Calculate target PnL in USDT."""
    margin = allocated_margin_usdt(order_size_usdt, num_grids)
    return margin * target_pct / 100


def drawdown_limit_usdt(order_size_usdt: float, num_grids: int, max_dd_pct: float) -> float:
    """Calculate drawdown limit in USDT."""
    margin = allocated_margin_usdt(order_size_usdt, num_grids)
    return margin * max_dd_pct / 100
