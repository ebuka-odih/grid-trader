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
    last_fill_at: float = 0.0  # timestamp of most recent fill (for post-fill cooldown)
    scaled_out: bool = False  # True after a partial-close at the hard floor
    
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
    drawdown_breached: bool = False


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
    PARTIAL_CLOSE = "partial_close"  # First floor breach — close half, retry


def compute_atr_bucketed_floor(
    atr_pct: float,
    base_pct: float = 20.0,
    min_pct: float = 15.0,
    max_pct: float = 30.0,
) -> float:
    """
    Per-grid hard floor calibrated to coin volatility.

      ATR < 0.5%  → 15%   (calm: BTC, ETH)
      0.5–1%      → 20%   (normal)
      1–2%        → 25%   (active)
      > 2%        → 30%   (volatile memes)

    Returns the floor as % of allocated margin. The {min,max}_pct args clamp
    the buckets — operators can widen/tighten via env without code changes.
    """
    if atr_pct < 0.5:
        floor = max(min_pct, base_pct - 5.0)
    elif atr_pct < 1.0:
        floor = base_pct
    elif atr_pct < 2.0:
        floor = base_pct + 5.0
    else:
        floor = base_pct + 10.0
    return max(min_pct, min(max_pct, floor))


@dataclass
class SmartCloseConfig:
    """Configuration for smart negative-close logic."""
    # NOTE on units: every loss_pct field below is now percent of ALLOCATED
    # MARGIN (real-money risk), not percent of entry price. Margin-% is
    # leverage-aware — a 20% margin loss means lost 20% of the capital
    # committed to this grid, regardless of leverage. The previous price-%
    # interpretation became unreachable at high leverage (50× → 1% price
    # = 50% margin, so the old 5% price floor only fired at 250% margin).

    # Time-based decay
    time_decay_enabled: bool = True
    time_decay_hours: float = 8.0        # Close if negative for this long
    time_decay_min_loss_pct: float = 5.0 # Min margin-loss % to trigger time decay
    
    # Momentum-based exit
    momentum_exit_enabled: bool = True
    momentum_window_sec: float = 60.0    # Look at last 60s of price movement
    momentum_threshold_pct: float = 2.0  # 2% move in window = trend acceleration
    
    # Grid imbalance
    imbalance_close_enabled: bool = True
    imbalance_ratio_threshold: float = 3.0  # 3:1 buy:sell = close losers
    imbalance_min_fills: int = 6             # Need at least 6 fills before checking

    # ── Emergency imbalance bypass ───────────────────────────────
    # When the grid is filling rapidly in one direction *and* the position
    # has had time to declare itself, bypass min-age + hard floor so the
    # close fires before -15% to -28%. Two guards stop the bypass from
    # firing on routine candle events that the freeze + hard-floor + scale
    # -out chain would have absorbed:
    #   - min_loss_pct: 8% margin (was 3%) — below this, we trust freeze
    #     + recovery window. ZEN closing at 3.6% in 23s with -$0.55 was
    #     the canonical false trigger; data showed 10/24 post-fix losers
    #     all hit this path.
    #   - min_age_sec: 60s — a 4-fill burst inside one minute is the
    #     candle pattern, not a sustained directional move. Skip the
    #     bypass and let freeze pause new fills while smart-close watches.
    imbalance_emergency_ratio: float = 4.0
    imbalance_emergency_min_fills: int = 4
    imbalance_emergency_min_loss_pct: float = 8.0
    imbalance_emergency_min_age_sec: float = 60.0
    
    # Trailing stop on underwater positions (margin-%)
    trailing_stop_enabled: bool = True
    trailing_stop_initial_pct: float = 18.0  # Initial stop at -18% margin
    trailing_stop_tighten_hours: float = 4.0 # After 4 hours, tighten
    trailing_stop_tightened_pct: float = 12.0 # Tightened stop at -12% margin

    # Recovery probability (learned from data) — margin-%
    recovery_check_enabled: bool = True
    recovery_min_depth_pct: float = 8.0  # Only check if >8% margin underwater
    recovery_max_hours: float = 6.0      # If >6 hours at this depth, unlikely to recover

    # ── Minimum position age ─────────────────────────────────
    # No close (not even hard floor) can fire on a position younger than
    # this. Protects against initial-deployment noise: a fresh grid
    # filling its first 1-3 levels can show an apparent 15%+ margin loss
    # before any meaningful price action — that's the entry-price ladder,
    # not real distress. After this age, all the normal triggers apply.
    min_position_age_sec: float = 90.0

    # ── Premature-close protection ─────────────────────────────
    # After any new fill the grid is still actively working — give it room
    # to do its job before any smart-close trigger fires. Bypassed only by
    # the hard loss floor below.
    min_seconds_since_last_fill: float = 180.0

    # ── Recovery window before realising a loss ────────────────
    # When a smart-close check would fire, enter a recovery window: track
    # the worst loss seen, and if the position recovers `recovery_partial_pct`
    # of that worst loss within `recovery_window_sec`, abort the close and
    # let the grid keep working. If the window expires without partial
    # recovery, the close fires.
    recovery_window_sec: float = 300.0
    recovery_partial_pct: float = 30.0

    # ── Hard loss floor (margin %) ──────────────────────────────
    # Absolute cap on per-position loss as % of allocated margin. When
    # breached, scale-out logic kicks in (close half + reset recovery on the
    # remainder); a second breach closes the rest. The floor is calibrated
    # for genuine distress in volatile markets, not noise. ATR-based
    # plumbing in the engine can override this per-grid (15-30% bucket).
    hard_loss_pct_floor: float = 20.0
    hard_loss_pct_floor_min: float = 15.0   # ATR-bucketed clamp lower bound
    hard_loss_pct_floor_max: float = 30.0   # ATR-bucketed clamp upper bound

    # ── Scale-out at hard floor ─────────────────────────────────
    # On first floor breach, close `scale_out_fraction` of the position and
    # reset the recovery window. If the remainder breaches again, full close.
    # Set scale_out_fraction=1.0 to disable scale-out (single-shot close).
    scale_out_fraction: float = 0.5

    # ── Dynamic take-profit ────────────────────────────────────
    # The TP target shrinks with position age (so old positions don't sit
    # stale with marginal gains) and can extend above the floor when
    # momentum is favorable in the early window.
    tp_floor_pct: float = 3.0                    # Default close target (%)
    tp_min_age_full_target_min: float = 10.0     # Below this, full target
    tp_decay_step_pct: float = 2.0               # Target the curve drops to right after full_target_min
    tp_decay_to_zero_at_min: float = 60.0        # Above this, any positive closes
    tp_dust_floor_usdt: float = 0.05             # Minimum $ PnL to honour late close
    tp_momentum_enabled: bool = True
    tp_momentum_max_age_min: float = 30.0        # Above this, no extension
    tp_momentum_velocity_pct_per_min: float = 1.5
    tp_momentum_extend_max_pct: float = 5.0      # Hard cap on extended target
    tp_momentum_trailing_giveback_pct: float = 0.5  # Lock-in giveback above floor
    tp_min_fills: int = 2


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
        # side -> {"start_ts": float, "worst_loss_pct": float}
        self._recovery_state: Dict[str, Dict[str, float]] = {}
        # side -> highest pnl_pct seen during the current open position
        self._tp_peak_pct: Dict[str, float] = {}

    def reset_recovery(self, side: str = ""):
        """Drop recovery tracking for a side (or all if empty)."""
        if side:
            self._recovery_state.pop(side, None)
        else:
            self._recovery_state.clear()

    def reset_tp_peak(self, side: str = ""):
        """Drop dynamic-TP peak tracking for a side (or all if empty)."""
        if side:
            self._tp_peak_pct.pop(side, None)
        else:
            self._tp_peak_pct.clear()

    def _velocity_pct_per_min_in_favor(self, position: GridPosition) -> float:
        """Recent price velocity (%/min) in favor of the position. Positive = good."""
        if len(self._price_history) < 3:
            return 0.0
        now = self._price_history[-1][0]
        window_sec = 60.0
        recent = [(t, p) for t, p in self._price_history if t > now - window_sec]
        if len(recent) < 3:
            return 0.0
        first_t, first_p = recent[0]
        last_t, last_p = recent[-1]
        if first_p <= 0 or last_t <= first_t:
            return 0.0
        delta_pct = (last_p - first_p) / first_p * 100
        if position.side == "Sell":
            delta_pct = -delta_pct
        minutes = (last_t - first_t) / 60
        return delta_pct / minutes if minutes > 0 else 0.0

    def evaluate_take_profit(
        self,
        position: GridPosition,
        current_price: float,
        allocated_margin: float,
        total_pnl: float,
        total_fills: int,
    ) -> Optional[CloseReason]:
        """
        Dynamic take-profit:
          • Target shrinks with age (3% → 0% over 15-60 min).
          • Inside the early momentum window (<30 min by default), the target
            can extend up to `tp_momentum_extend_max_pct` while velocity in
            our favor stays above `tp_momentum_velocity_pct_per_min`.
          • Once the peak crosses the floor, a trailing giveback locks in
            profit if PnL retraces by `tp_momentum_trailing_giveback_pct`.
          • Past the momentum window, time-decay rules — no extension.
          • Past `tp_decay_to_zero_at_min`, any positive PnL closes (with a
            dust floor in $ terms to avoid fee-eating churn).
        Returns CloseReason.TARGET_HIT if the position should close, else None.
        """
        cfg = self.config

        if position.is_flat or allocated_margin <= 0:
            self._tp_peak_pct.pop(position.side, None)
            return None

        if total_pnl <= 0:
            # Not in profit — clear peak so future re-entry to profit starts clean.
            self._tp_peak_pct.pop(position.side, None)
            return None

        if total_fills < cfg.tp_min_fills:
            return None

        pnl_pct = total_pnl / allocated_margin * 100
        age_min = position.age_seconds / 60

        # Update peak
        peak = max(self._tp_peak_pct.get(position.side, 0.0), pnl_pct)
        self._tp_peak_pct[position.side] = peak

        # 1. Time-decayed floor (% required to close).
        #    Below full_target_min: full floor target (e.g. 3%).
        #    At full_target_min the curve steps down to `decay_step_pct` (2%)
        #    and ramps linearly to 0 by `decay_to_zero_at_min`.
        if age_min <= cfg.tp_min_age_full_target_min:
            time_target_pct = cfg.tp_floor_pct
        elif age_min >= cfg.tp_decay_to_zero_at_min:
            time_target_pct = 0.0
        else:
            progress = (age_min - cfg.tp_min_age_full_target_min) / (
                cfg.tp_decay_to_zero_at_min - cfg.tp_min_age_full_target_min
            )
            time_target_pct = cfg.tp_decay_step_pct * (1.0 - progress)

        # 2. Late-stage dust guard
        if age_min >= cfg.tp_decay_to_zero_at_min and total_pnl < cfg.tp_dust_floor_usdt:
            return None

        # 3. Past momentum window — time-decay rules, no extension.
        if not cfg.tp_momentum_enabled or age_min >= cfg.tp_momentum_max_age_min:
            if pnl_pct >= time_target_pct:
                logger.info(
                    f"🎯 TP TIME-DECAY ({position.side}): pnl={pnl_pct:.2f}% >= "
                    f"target={time_target_pct:.2f}% (age={age_min:.1f}m)"
                )
                return CloseReason.TARGET_HIT
            return None

        # 4. Young position. If peak hasn't crossed floor, close on the
        #    time-decayed target (which equals the floor while age <
        #    full_target_min and decays afterward).
        if peak < cfg.tp_floor_pct:
            if pnl_pct >= time_target_pct:
                logger.info(
                    f"🎯 TP FLOOR ({position.side}): pnl={pnl_pct:.2f}% >= "
                    f"target={time_target_pct:.2f}% (age={age_min:.1f}m)"
                )
                return CloseReason.TARGET_HIT
            return None

        # 5. Peak above floor. Hard cap at extend_max_pct.
        if peak >= cfg.tp_momentum_extend_max_pct:
            logger.info(
                f"🎯 TP CAP ({position.side}): peak={peak:.2f}% reached "
                f"max={cfg.tp_momentum_extend_max_pct:.2f}% (pnl={pnl_pct:.2f}%)"
            )
            return CloseReason.TARGET_HIT

        # 6. Trailing giveback — lock in if retraced too far from peak.
        if pnl_pct <= peak - cfg.tp_momentum_trailing_giveback_pct:
            logger.info(
                f"🎯 TP TRAIL LOCK ({position.side}): peak={peak:.2f}% now={pnl_pct:.2f}% "
                f"giveback={peak-pnl_pct:.2f}%"
            )
            return CloseReason.TARGET_HIT

        # 7. Velocity check — if momentum has died, accept floor.
        velocity = self._velocity_pct_per_min_in_favor(position)
        if velocity < cfg.tp_momentum_velocity_pct_per_min and pnl_pct >= cfg.tp_floor_pct:
            logger.info(
                f"🎯 TP MOMENTUM FADED ({position.side}): pnl={pnl_pct:.2f}% "
                f"velocity={velocity:.2f}%/min — closing at floor"
            )
            return CloseReason.TARGET_HIT

        # Hold and let it run.
        return None

    def _seconds_since_last_fill(self, position: GridPosition) -> float:
        if position.last_fill_at <= 0:
            return position.age_seconds
        return max(0.0, time.time() - position.last_fill_at)

    def _should_defer_close(self, side: str, loss_pct: float) -> bool:
        """
        Manage the recovery window. Returns True if the close should be
        deferred (hold the position), False if recovery has failed and the
        close should fire.
        """
        if self.config.recovery_window_sec <= 0:
            return False

        now = time.time()
        state = self._recovery_state.get(side)
        if state is None:
            self._recovery_state[side] = {"start_ts": now, "worst_loss_pct": loss_pct}
            logger.info(
                f"🩹 RECOVERY WINDOW open ({side}): loss={loss_pct:.2f}% — "
                f"holding up to {self.config.recovery_window_sec:.0f}s for "
                f"{self.config.recovery_partial_pct:.0f}% bounce"
            )
            return True

        # Update worst loss seen during this window.
        if loss_pct > state["worst_loss_pct"]:
            state["worst_loss_pct"] = loss_pct

        worst = state["worst_loss_pct"]
        if worst > 0:
            recovered_pct = (worst - loss_pct) / worst * 100
        else:
            recovered_pct = 0.0

        if recovered_pct >= self.config.recovery_partial_pct:
            logger.info(
                f"🩹 RECOVERY OK ({side}): worst={worst:.2f}% now={loss_pct:.2f}% "
                f"({recovered_pct:.0f}% recovered) — aborting close"
            )
            self._recovery_state.pop(side, None)
            return True

        elapsed = now - state["start_ts"]
        if elapsed < self.config.recovery_window_sec:
            return True

        logger.warning(
            f"🩹 RECOVERY EXPIRED ({side}): worst={worst:.2f}% now={loss_pct:.2f}% "
            f"only {recovered_pct:.0f}% recovered after {elapsed:.0f}s — closing"
        )
        self._recovery_state.pop(side, None)
        return False
    
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
        drawdown_breached: bool = False,
        atr_pct: float = 0.0,
    ) -> Optional[CloseReason]:
        """
        Check if a losing position should be closed based on smart logic.
        
        Returns CloseReason if should close, None if should hold.
        Only triggers for POSITIONS THAT ARE NEGATIVE.
        """
        if position.is_flat:
            return None

        # ── Loss measurement ──
        # `loss_pct` is the loss as a percentage of allocated margin (real
        # money risk), NOT a percentage of entry price. Margin-% is leverage-
        # aware: a 5% margin loss means you lost $0.50 on $10 allocated,
        # regardless of leverage.
        #
        # SCALE-OUT INTERACTION: once a position has been scaled out, the
        # already-realized loss from the half-close is a sunk cost. Counting
        # it toward `loss_pct` for the hard-floor check would re-fire the
        # floor on the very next tick (since realized + unrealized still
        # adds up to the same amount). Instead, post-scale-out the floor
        # measures only the unrealized PnL on the remaining position — so
        # the floor can only fire again if the price moves FURTHER against
        # us by another floor's worth on the smaller remaining size.
        position.update_unrealized(current_price)
        if position.scaled_out:
            relevant_pnl = position.unrealized_pnl
            margin_basis = allocated_margin / 2.0  # remaining qty exposes ~half the margin
        else:
            relevant_pnl = position.realized_pnl + position.unrealized_pnl
            margin_basis = allocated_margin
        if margin_basis > 0:
            loss_pct = -relevant_pnl / margin_basis * 100
        else:
            # Fallback to price-% if we don't know the margin.
            if position.side == "Buy":
                loss_pct = (position.entry_price - current_price) / position.entry_price * 100
            else:
                loss_pct = (current_price - position.entry_price) / position.entry_price * 100

        # Only apply to losing positions
        if loss_pct <= 0:
            # Position is flat or in profit — clear any in-flight recovery state
            self._recovery_state.pop(position.side, None)
            return None

        # Track depth over time
        self._track_depth(position.side, loss_pct)

        # ── Emergency imbalance bypass (FIRST — bypasses both min-age AND floor) ──
        # Catches sustained directional fills before the hard floor at 15%.
        # Two new guards keep it from firing on candle events:
        #   - loss_pct >= imbalance_emergency_min_loss_pct (default 8%) so
        #     small-bleed bursts get to recover via freeze + smart-close.
        #   - position.age_seconds >= imbalance_emergency_min_age_sec
        #     (default 60s) so a 4-fill candle inside one minute can't
        #     trip the bypass.
        # Below those thresholds the freeze (max_same_side_fills) pauses
        # new fills, the hard floor (15-30% margin, ATR-bucketed) is the
        # real safety net, and scale-out at the floor halves before close.
        meets_size = (
            self.config.imbalance_close_enabled
            and total_fills >= self.config.imbalance_emergency_min_fills
            and imbalance.imbalance_ratio >= self.config.imbalance_emergency_ratio
            and position.side == imbalance.dominant_side
        )
        if meets_size:
            meets_loss = loss_pct >= self.config.imbalance_emergency_min_loss_pct
            meets_age = position.age_seconds >= self.config.imbalance_emergency_min_age_sec
            if meets_loss and meets_age:
                self._recovery_state.pop(position.side, None)
                # First bypass hit: scale out half, give the rest a chance
                # via the recovery window. Second hit (already scaled_out)
                # closes the remainder. Mirrors the hard-floor pattern at
                # line 648 below and roughly halves avg_loss on bypass
                # closures vs the previous immediate-close behaviour.
                if not position.scaled_out and self.config.scale_out_fraction < 1.0:
                    logger.warning(
                        f"⚡ EMERGENCY IMBALANCE ({position.side}): "
                        f"ratio={imbalance.imbalance_ratio:.1f}:1, fills={total_fills}, "
                        f"loss={loss_pct:.2f}%, age={position.age_seconds:.0f}s — "
                        f"scaling out {self.config.scale_out_fraction*100:.0f}% "
                        f"(first hit, recovery window opens)"
                    )
                    return CloseReason.PARTIAL_CLOSE
                logger.warning(
                    f"⚡ EMERGENCY IMBALANCE ({position.side}): "
                    f"ratio={imbalance.imbalance_ratio:.1f}:1, fills={total_fills}, "
                    f"loss={loss_pct:.2f}%, age={position.age_seconds:.0f}s — "
                    f"closing remainder (already scaled out)"
                )
                return CloseReason.GRID_IMBALANCE
            elif not meets_age:
                logger.info(
                    f"⏸  IMBALANCE bypass deferred (candle guard): {position.side} "
                    f"ratio={imbalance.imbalance_ratio:.1f}:1 fills={total_fills} "
                    f"loss={loss_pct:.2f}% age={position.age_seconds:.0f}s "
                    f"< {self.config.imbalance_emergency_min_age_sec:.0f}s — "
                    f"letting freeze + hard floor manage exit"
                )
            elif not meets_loss:
                logger.info(
                    f"⏸  IMBALANCE bypass deferred (loss guard): {position.side} "
                    f"ratio={imbalance.imbalance_ratio:.1f}:1 fills={total_fills} "
                    f"loss={loss_pct:.2f}% < "
                    f"{self.config.imbalance_emergency_min_loss_pct:.1f}% — "
                    f"letting freeze + recovery window work"
                )

        # ── Minimum position age ──
        # A brand-new grid filling its first few levels can briefly show a
        # large apparent margin loss that is just the cost basis re-spreading
        # across more fills. Don't close (except via emergency bypass above)
        # in the first `min_position_age_sec` seconds.
        if position.age_seconds < self.config.min_position_age_sec:
            return None

        # ── Hard loss floor: bypass cooldown + recovery ──
        # Per-grid floor: ATR-bucketed if atr_pct supplied, else config default.
        if atr_pct > 0:
            floor_pct = compute_atr_bucketed_floor(
                atr_pct,
                base_pct=self.config.hard_loss_pct_floor,
                min_pct=self.config.hard_loss_pct_floor_min,
                max_pct=self.config.hard_loss_pct_floor_max,
            )
        else:
            floor_pct = self.config.hard_loss_pct_floor

        if loss_pct >= floor_pct:
            self._recovery_state.pop(position.side, None)
            # First breach scales out (half-close + retry); the engine layer
            # synthesizes the partial fill. Subsequent breach closes the rest.
            if not position.scaled_out and self.config.scale_out_fraction < 1.0:
                logger.warning(
                    f"🛑 HARD FLOOR ({position.side}): loss={loss_pct:.2f}% >= "
                    f"{floor_pct:.2f}% (ATR={atr_pct:.2f}%) — scaling out "
                    f"{self.config.scale_out_fraction*100:.0f}%"
                )
                return CloseReason.PARTIAL_CLOSE
            logger.warning(
                f"🛑 HARD FLOOR ({position.side}): loss={loss_pct:.2f}% >= "
                f"{floor_pct:.2f}% — closing remainder (already scaled out)"
            )
            return CloseReason.DRAWDOWN

        # ── Post-fill cooldown: let the grid work after a recent fill ──
        sec_since_fill = self._seconds_since_last_fill(position)
        if sec_since_fill < self.config.min_seconds_since_last_fill:
            return None

        # ── Identify the strongest candidate close reason ──
        candidate: Optional[CloseReason] = None

        if self.config.time_decay_enabled:
            candidate = candidate or self._check_time_decay(position, loss_pct)

        if self.config.momentum_exit_enabled:
            candidate = candidate or self._check_momentum(position, current_price)

        if self.config.imbalance_close_enabled:
            candidate = candidate or self._check_imbalance(position, imbalance, total_fills)

        if self.config.trailing_stop_enabled:
            candidate = candidate or self._check_trailing_stop(position, loss_pct)

        if self.config.recovery_check_enabled:
            candidate = candidate or self._check_recovery_probability(position, loss_pct)

        # Promote a drawdown breach into a candidate. This routes the
        # drawdown floor through the same cooldown + recovery deferral as
        # the other smart-close triggers, so a position close to the
        # margin-based DD limit still gets a chance to recover before the
        # loss is realised. The hard_loss_pct_floor above is the absolute
        # safety net.
        if candidate is None and drawdown_breached:
            candidate = CloseReason.DRAWDOWN

        if candidate is None:
            # No trigger — but if we were in a recovery window and the
            # underlying check has stopped firing, leave the state alone:
            # the next firing-or-bounce will resolve it.
            return None

        # Route through the recovery window before realising the loss.
        if self._should_defer_close(position.side, loss_pct):
            return None

        return candidate
    
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
            position.last_fill_at = 0.0
            position.scaled_out = False
        # If remaining qty > 0, position stays open with same entry

    if not position.is_flat:
        position.last_fill_at = fill.timestamp

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
    
    # Drawdown check — flag breach but don't auto-close (let SmartCloseEngine decide first)
    drawdown_limit = allocated_margin * max_drawdown_pct / 100
    result.drawdown_limit = drawdown_limit
    
    if total_pnl < 0 and abs(total_pnl) > drawdown_limit:
        result.drawdown_breached = True
        result.close_reason = CloseReason.DRAWDOWN.value
        # Don't set should_close=True — SmartCloseEngine gets first chance to evaluate
        return result
    
    return result


# ── Cycle Reset ────────────────────────────────────────────────

def perform_partial_close(
    position: GridPosition,
    imbalance: GridImbalance,
    current_price: float,
    fraction: float = 0.5,
) -> tuple[float, float]:
    """
    Synthesize a closing fill that realizes `fraction` of the position at
    `current_price`. Marks `position.scaled_out=True`. Returns (realised_pnl,
    qty_closed).

    Uses the standard process_fill machinery — the closing fill is just an
    opposite-side fill that reduces qty without flipping the position.
    """
    if position.is_flat or fraction <= 0 or fraction >= 1:
        return 0.0, 0.0
    close_qty = position.qty * fraction
    opposite = "Sell" if position.side == "Buy" else "Buy"
    fill = FillEvent(
        level_index=-1,
        side=opposite,
        price=current_price,
        qty=close_qty,
        timestamp=time.time(),
    )
    pnl = process_fill(fill, position, imbalance)
    if not position.is_flat:
        position.scaled_out = True
    return pnl, close_qty


def reset_position(position: GridPosition):
    """Reset position for next cycle."""
    position.side = ""
    position.qty = 0.0
    position.entry_price = 0.0
    position.unrealized_pnl = 0.0
    position.opened_at = 0.0
    position.last_fill_at = 0.0
    position.scaled_out = False


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
