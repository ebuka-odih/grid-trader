"""
Adaptive Grid v3 — Dynamic grid management that responds to price movement.

Key improvements over v2:
1. Grid Recentering: Shifts grid bounds when price moves through levels
2. Trailing Grid: Follows price trend with configurable offset
3. Exponential Level Sizing: Smaller orders at edges, larger near price
4. Per-Level Exposure Cap: Max same-side fills before hedge/close
5. Fast Spike Detection: 10s window instead of 60s

Cross-margin aware: all calculations respect wallet exposure limits.
"""

import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Optional
from collections import deque

logger = logging.getLogger("adaptive_grid")


# ── Configuration ──────────────────────────────────────────────

@dataclass
class AdaptiveConfig:
    """Tunable parameters for adaptive grid behavior."""
    
    # Recentering
    recenter_trigger_pct: float = 40.0    # Recenter when price crosses this % of grid range
    recenter_cooldown_sec: float = 60.0   # Min seconds between recenters
    recenter_range_shrink: float = 0.85   # New range = old_range * this factor (tightens on recenter)
    recenter_range_min_pct: float = 1.5   # Minimum grid range as % of price
    
    # Trailing
    trailing_enabled: bool = True
    trailing_offset_pct: float = 0.3      # Trail 30% behind price movement
    trailing_min_move_pct: float = 0.5    # Only trail if price moved >0.5% from last trail
    
    # Exponential sizing
    exp_sizing_enabled: bool = True
    exp_sizing_gamma: float = 1.5         # Higher = more aggressive edge reduction (1.0=uniform, 2.0=very aggressive)
    exp_sizing_min_factor: float = 0.3    # Minimum order size factor (30% of base)
    
    # Spike detection (fast)
    spike_window_sec: float = 10.0        # Fast spike window (was 60s)
    spike_threshold_pct: float = 0.5      # 0.5% in 10s = spike (was 1% in 60s)
    spike_cooldown_sec: float = 30.0      # Pause fills for this long after spike
    
    # Per-level exposure cap
    max_same_side_fills: int = 3          # Max fills on one side before action
    hedge_on_breach: bool = True          # If True, close excess; if False, freeze grid
    
    # Momentum tracking
    momentum_window_sec: float = 30.0     # Track momentum over 30s
    momentum_threshold: float = 0.3       # 0.3% move = directional momentum


# ── Price Tracker ──────────────────────────────────────────────

class PriceTracker:
    """Fast price tracking for spike detection and momentum."""
    
    def __init__(self, window_sec: float = 60.0):
        self._window_sec = window_sec
        self._prices: deque = deque()  # (timestamp, price)
    
    def add(self, price: float, timestamp: float = None):
        ts = timestamp or time.time()
        self._prices.append((ts, price))
        self._trim(ts)
    
    def _trim(self, now: float):
        cutoff = now - self._window_sec
        while self._prices and self._prices[0][0] < cutoff:
            self._prices.popleft()
    
    def velocity_pct(self, window_sec: float = None) -> Optional[float]:
        """Price change % over the given window. None if insufficient data."""
        window = window_sec or self._window_sec
        # Use latest price's timestamp instead of real time
        # This supports both real-time and synthetic timestamps
        if not self._prices:
            return None
        now = self._prices[-1][0]
        self._trim(now)
        if len(self._prices) < 2:
            return None
        # Find oldest price within window
        target_ts = now - window
        oldest = None
        for ts, p in self._prices:
            if ts >= target_ts:
                oldest = (ts, p)
                break
        if oldest is None:
            return None
        newest = self._prices[-1]
        if newest[0] - oldest[0] < window * 0.5:
            return None  # Not enough data
        return (newest[1] - oldest[1]) / oldest[1] * 100
    
    def current(self) -> Optional[float]:
        return self._prices[-1][1] if self._prices else None
    
    def high_in_window(self, window_sec: float = None) -> Optional[float]:
        window = window_sec or self._window_sec
        now = time.time()
        self._trim(now)
        if not self._prices:
            return None
        cutoff = now - window
        return max(p for ts, p in self._prices if ts >= cutoff)
    
    def low_in_window(self, window_sec: float = None) -> Optional[float]:
        window = window_sec or self._window_sec
        now = time.time()
        self._trim(now)
        if not self._prices:
            return None
        cutoff = now - window
        return min(p for ts, p in self._prices if ts >= cutoff)


# ── Spike Detector ─────────────────────────────────────────────

@dataclass
class SpikeState:
    """Tracks spike detection state."""
    active: bool = False
    detected_at: float = 0.0
    direction: str = ""  # "up" or "down"
    velocity_pct: float = 0.0
    fills_paused_until: float = 0.0


class SpikeDetector:
    """Fast spike detection using 10s price windows."""
    
    def __init__(self, config: AdaptiveConfig):
        self.config = config
        self._tracker = PriceTracker(window_sec=config.spike_window_sec * 3)
        self._spike = SpikeState()
    
    def update(self, price: float, timestamp: float = None) -> Optional[SpikeState]:
        """Add price tick. Returns SpikeState if spike detected, None otherwise."""
        self._tracker.add(price, timestamp)
        vel = self._tracker.velocity_pct(self.config.spike_window_sec)
        if vel is None:
            return None
        
        if abs(vel) >= self.config.spike_threshold_pct:
            now = time.time()
            # Don't re-trigger during cooldown
            if self._spike.active and now - self._spike.detected_at < self.config.spike_cooldown_sec:
                return None
            
            self._spike = SpikeState(
                active=True,
                detected_at=now,
                direction="up" if vel > 0 else "down",
                velocity_pct=vel,
                fills_paused_until=now + self.config.spike_cooldown_sec,
            )
            logger.warning(
                f"⚡ FAST SPIKE! {vel:+.2f}% in {self.config.spike_window_sec:.0f}s | "
                f"dir={self._spike.direction} | pausing fills until {self._spike.fills_paused_until:.0f}"
            )
            return self._spike
        
        # Clear spike if cooldown expired
        if self._spike.active and time.time() > self._spike.fills_paused_until:
            self._spike.active = False
        
        return None
    
    def is_paused(self) -> bool:
        """Are fills paused due to spike?"""
        return self._spike.active and time.time() < self._spike.fills_paused_until
    
    @property
    def state(self) -> SpikeState:
        return self._spike


# ── Exposure Tracker ───────────────────────────────────────────

@dataclass
class SideExposure:
    """Tracks fills per side."""
    buy_fills: int = 0
    sell_fills: int = 0
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    last_fill_side: str = ""
    consecutive_same_side: int = 0
    
    def record_fill(self, side: str, qty: float):
        if side == "Buy":
            self.buy_fills += 1
            self.buy_qty += qty
        else:
            self.sell_fills += 1
            self.sell_qty += qty
        
        if side == self.last_fill_side:
            self.consecutive_same_side += 1
        else:
            self.consecutive_same_side = 1
        self.last_fill_side = side
    
    @property
    def imbalance_ratio(self) -> float:
        """Ratio of buy/sell fills. 1.0 = balanced, >1 = more buys, <1 = more sells."""
        total = self.buy_fills + self.sell_fills
        if total == 0:
            return 1.0
        return self.buy_fills / max(self.sell_fills, 1)
    
    @property
    def net_exposure_qty(self) -> float:
        """Net directional exposure (positive = long, negative = short)."""
        return self.buy_qty - self.sell_qty


class ExposureCap:
    """Enforces per-level exposure limits."""
    
    def __init__(self, config: AdaptiveConfig):
        self.config = config
        self.exposure = SideExposure()
    
    def record_fill(self, side: str, qty: float):
        self.exposure.record_fill(side, qty)
    
    def is_breached(self) -> bool:
        """Has the same-side fill limit been breached?"""
        return self.exposure.consecutive_same_side >= self.config.max_same_side_fills
    
    def should_close_excess(self) -> bool:
        """Should we close excess positions?"""
        return self.is_breached() and self.config.hedge_on_breach
    
    def fills_allowed(self) -> bool:
        """Are new fills allowed?"""
        return not self.is_breached()


# ── Adaptive Grid Controller ──────────────────────────────────

@dataclass
class GridBounds:
    """Current grid bounds."""
    upper: float
    lower: float
    num_grids: int
    center_price: float
    
    @property
    def range_pct(self) -> float:
        """Grid range as % of center price."""
        if self.center_price <= 0:
            return 0.0
        return (self.upper - self.lower) / self.center_price * 100
    
    def price_position_pct(self, price: float) -> float:
        """Where is price within the grid? 0%=lower, 100%=upper."""
        rng = self.upper - self.lower
        if rng <= 0:
            return 50.0
        return (price - self.lower) / rng * 100


@dataclass
class RecenterEvent:
    """Describes a grid recentering event."""
    timestamp: float
    old_upper: float
    old_lower: float
    new_upper: float
    new_lower: float
    reason: str
    price_at_recenter: float


class AdaptiveGrid:
    """
    Manages a single adaptive grid that recenters and trails with price.
    
    Usage:
        ag = AdaptiveGrid(config, initial_upper, initial_lower, num_grids)
        result = ag.on_price(price)
        if result.recentered:
            # Cancel old orders, place new ones
        if result.fill_allowed:
            # Process fill at result.fill_level
    """
    
    def __init__(
        self,
        config: AdaptiveConfig,
        upper: float,
        lower: float,
        num_grids: int,
        base_order_size: float,
        leverage: int = 50,
    ):
        self.config = config
        self.bounds = GridBounds(upper=upper, lower=lower, num_grids=num_grids, center_price=(upper + lower) / 2)
        self.base_order_size = base_order_size
        self.leverage = leverage
        
        self.spike_detector = SpikeDetector(config)
        self.exposure_cap = ExposureCap(config)
        self.price_tracker = PriceTracker(window_sec=config.momentum_window_sec * 2)
        
        self._last_recenter_at: float = 0.0
        self._last_trail_at: float = 0.0
        self._last_trail_price: float = 0.0
        self._recenter_history: list[RecenterEvent] = []
        
        # Level sizing cache
        self._level_sizes: dict[int, float] = {}  # index -> adjusted order size
    
    def on_price(self, price: float, timestamp: float = None) -> 'AdaptiveResult':
        """
        Process a new price tick. Returns an AdaptiveResult describing
        what action (if any) should be taken.
        
        Args:
            price: Current price
            timestamp: Optional timestamp (uses time.time() if None)
        """
        now = timestamp or time.time()
        self.price_tracker.add(price, now)
        
        result = AdaptiveResult(price=price)
        
        # 1. Fast spike detection
        spike = self.spike_detector.update(price, now)
        if spike:
            result.spike_detected = True
            result.spike_state = spike
            if self.spike_detector.is_paused():
                result.fill_allowed = False
                result.action = "pause"
                return result
        
        # 2. Check exposure cap
        if not self.exposure_cap.fills_allowed():
            result.fill_allowed = False
            if self.exposure_cap.should_close_excess():
                result.action = "close_excess"
                result.close_side = self.exposure_cap.exposure.last_fill_side
            else:
                result.action = "freeze"
            return result
        
        # 3. Check if recentering is needed
        recenter = self._check_recenter(price, now)
        if recenter:
            result.recentered = True
            result.recenter_event = recenter
            result.action = "recenter"
        
        # 4. Check if trailing should shift grid
        if self.config.trailing_enabled and not result.recentered:
            trail = self._check_trailing(price, now)
            if trail:
                result.trail_shift = trail
                result.action = "trail"
        
        # 5. Calculate level sizing for this price
        result.level_sizes = self._compute_level_sizes(price)
        
        return result
    
    def record_fill(self, side: str, qty: float, level_index: int):
        """Record a fill for exposure tracking."""
        self.exposure_cap.record_fill(side, qty)
    
    def get_level_sizes(self, current_price: float) -> dict[int, float]:
        """Get order sizes for each grid level."""
        return self._compute_level_sizes(current_price)
    
    def _check_recenter(self, price: float, now: float) -> Optional[RecenterEvent]:
        """Check if grid needs recentering."""
        # Cooldown check
        if now - self._last_recenter_at < self.config.recenter_cooldown_sec:
            return None
        
        pos_pct = self.bounds.price_position_pct(price)
        
        # Recenter if price is too close to edge
        trigger = self.config.recenter_trigger_pct
        needs_recenter = pos_pct <= (100 - trigger) or pos_pct >= trigger
        
        if not needs_recenter:
            return None
        
        # Calculate new bounds centered on current price
        old_range = self.bounds.upper - self.bounds.lower
        new_range = old_range * self.config.recenter_range_shrink
        
        # Enforce minimum range
        min_range = price * self.config.recenter_range_min_pct / 100
        new_range = max(new_range, min_range)
        
        new_upper = price + new_range / 2
        new_lower = price - new_range / 2
        
        event = RecenterEvent(
            timestamp=now,
            old_upper=self.bounds.upper,
            old_lower=self.bounds.lower,
            new_upper=new_upper,
            new_lower=new_lower,
            reason=f"price_at_{pos_pct:.0f}pct",
            price_at_recenter=price,
        )
        
        # Update bounds
        self.bounds.upper = new_upper
        self.bounds.lower = new_lower
        self.bounds.center_price = price
        self._last_recenter_at = now
        self._recenter_history.append(event)
        
        logger.info(
            f"🔄 GRID RECENTERED | {self.bounds.num_grids} levels | "
            f"old=[{event.old_lower:.4f}-{event.old_upper:.4f}] → "
            f"new=[{new_lower:.4f}-{new_upper:.4f}] | "
            f"range={self.bounds.range_pct:.2f}% | reason={event.reason}"
        )
        
        return event
    
    def _check_trailing(self, price: float, now: float) -> Optional[float]:
        """Check if grid should trail price movement."""
        if self._last_trail_price == 0:
            self._last_trail_price = price
            return None
        
        move_pct = abs(price - self._last_trail_price) / self._last_trail_price * 100
        if move_pct < self.config.trailing_min_move_pct:
            return None
        
        # Calculate trail shift
        direction = 1 if price > self._last_trail_price else -1
        shift = (price - self._last_trail_price) * self.config.trailing_offset_pct
        
        new_upper = self.bounds.upper + shift
        new_lower = self.bounds.lower + shift
        
        # Don't trail if it would make range too small
        if new_upper - new_lower < price * self.config.recenter_range_min_pct / 100:
            return None
        
        self.bounds.upper = new_upper
        self.bounds.lower = new_lower
        self.bounds.center_price = (new_upper + new_lower) / 2
        self._last_trail_price = price
        self._last_trail_at = now
        
        logger.info(
            f"📈 GRID TRAIL | shift={shift:+.4f} | "
            f"new=[{new_lower:.4f}-{new_upper:.4f}] | "
            f"range={self.bounds.range_pct:.2f}%"
        )
        
        return shift
    
    def _compute_level_sizes(self, current_price: float) -> dict[int, float]:
        """
        Compute order sizes for each grid level using exponential sizing.
        Levels closer to current price get larger orders.
        Levels at edges get smaller orders.
        
        v3-Lite: Sizes are normalized so total buy-side = total sell-side
        to prevent directional bias from exponential decay.
        """
        if not self.config.exp_sizing_enabled:
            return {i: self.base_order_size for i in range(self.bounds.num_grids)}
        
        step = (self.bounds.upper - self.bounds.lower) / self.bounds.num_grids
        raw_sizes = {}
        gamma = self.config.exp_sizing_gamma
        min_factor = self.config.exp_sizing_min_factor
        
        for i in range(self.bounds.num_grids):
            level_price = self.bounds.lower + step * (i + 0.5)
            dist = abs(level_price - current_price) / max(self.bounds.upper - self.bounds.lower, 1e-8)
            dist = min(dist, 1.0)
            factor = math.exp(-gamma * dist * 3)
            factor = max(min_factor, min(1.0, factor))
            raw_sizes[i] = self.base_order_size * factor
        
        # Normalize: ensure total buy size = total sell size
        buy_total = sum(s for i, s in raw_sizes.items() 
                       if self.bounds.lower + step * (i + 0.5) < current_price)
        sell_total = sum(s for i, s in raw_sizes.items() 
                        if self.bounds.lower + step * (i + 0.5) >= current_price)
        
        if buy_total > 0 and sell_total > 0:
            # Scale to equalize
            target = (buy_total + sell_total) / 2
            buy_scale = target / buy_total
            sell_scale = target / sell_total
            
            sizes = {}
            for i, s in raw_sizes.items():
                level_price = self.bounds.lower + step * (i + 0.5)
                if level_price < current_price:
                    sizes[i] = s * buy_scale
                else:
                    sizes[i] = s * sell_scale
            return sizes
        
        return raw_sizes
    
    @property
    def status(self) -> dict:
        """Get current adaptive grid status."""
        return {
            "upper": self.bounds.upper,
            "lower": self.bounds.lower,
            "num_grids": self.bounds.num_grids,
            "range_pct": round(self.bounds.range_pct, 4),
            "center_price": self.bounds.center_price,
            "spike_active": self.spike_detector.is_paused(),
            "spike_state": {
                "direction": self.spike_detector.state.direction,
                "velocity_pct": round(self.spike_detector.state.velocity_pct, 4),
                "paused_until": self.spike_detector.state.fills_paused_until,
            },
            "exposure": {
                "buy_fills": self.exposure_cap.exposure.buy_fills,
                "sell_fills": self.exposure_cap.exposure.sell_fills,
                "consecutive_same_side": self.exposure_cap.exposure.consecutive_same_side,
                "imbalance_ratio": round(self.exposure_cap.exposure.imbalance_ratio, 4),
                "breached": self.exposure_cap.is_breached(),
            },
            "recenters": len(self._recenter_history),
            "last_recenter_at": self._last_recenter_at,
            "trailing_enabled": self.config.trailing_enabled,
        }


@dataclass
class AdaptiveResult:
    """Result of processing a price tick through AdaptiveGrid."""
    price: float = 0.0
    action: str = "none"  # none, pause, freeze, close_excess, recenter, trail
    fill_allowed: bool = True
    recentered: bool = False
    recenter_event: Optional[RecenterEvent] = None
    trail_shift: Optional[float] = None
    spike_detected: bool = False
    spike_state: Optional[SpikeState] = None
    close_side: str = ""
    level_sizes: dict = field(default_factory=dict)


# ── Default Config Factory ─────────────────────────────────────

def default_config() -> AdaptiveConfig:
    """Return default adaptive grid config tuned for $100 wallet, cross-margin.
    
    v3-Lite: Only spike detection + exposure cap active.
    Recentering, exponential sizing, and trailing disabled after backtest
    showed they create directional bias or reset grids before positions close.
    
    Active features: fast spike detection, exposure cap.
    """
    return AdaptiveConfig(
        # Recentering DISABLED
        recenter_trigger_pct=100.0,
        # Trailing DISABLED (was creating one-sided fills)
        trailing_enabled=False,
        # Exponential sizing DISABLED
        exp_sizing_enabled=False,
        # Spike detection — fast 10s window.
        # Threshold tightened from 0.7→0.5 and cooldown extended from 30→60
        # to catch more cross-symbol candle events. Drawdown closes were
        # the dominant losing close-reason (avg -$1.93/trade) and 27% of
        # them clustered in 9 five-minute windows, so the existing
        # spike-fill-pause has more work to do.
        spike_window_sec=float(os.getenv("SPIKE_WINDOW_SEC", "10.0")),
        spike_threshold_pct=float(os.getenv("SPIKE_THRESHOLD_PCT", "0.5")),
        spike_cooldown_sec=float(os.getenv("SPIKE_COOLDOWN_SEC", "60.0")),
        # Exposure cap — freeze (don't auto-close) on breach.
        # 4 consecutive same-side fills triggers a freeze: pauses new fills
        # but lets the position run for the smart-close engine to manage.
        # Auto-closing on the 3rd fill was too aggressive — closed normal
        # grid wobble during initial deployment as if it were a real trend.
        # The hard-floor + scale-out + recovery window handles real
        # directional moves; the exposure cap just stops the position from
        # ballooning while smart-close decides.
        max_same_side_fills=4,
        hedge_on_breach=False,
    )
