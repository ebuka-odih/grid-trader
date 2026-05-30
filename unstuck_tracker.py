"""
Unstuck Tracker — Gradual loss-realisation budget manager.

Passivbot-style unsticking: instead of a binary "close or don't close"
decision at a hard floor, dose partial closes over time within a loss
allowance budget.

Core concepts:
  - **Peak balance tracking**: highest wallet balance since last reset
  - **Loss allowance budget**: unstuck_loss_allowance_pct % of peak × TWEL
  - **Budget spent**: cumulative realised losses from unstuck closes
  - **Cooldown**: minimum time between unstuck actions per side
  - **Priority**: sort stuck positions by abs(distance to breakeven) ASC

Usage:
    1. Initialise with wallet_balance, twel_pct (total wallet exposure limit %)
    2. Call update_peak(balance) each cycle
    3. Call can_unstuck(symbol, side, loss_pct) to check eligibility
    4. Call record_unstuck(symbol, side, realised_loss) after closing
"""

import time
import logging
from typing import Optional, Dict, Tuple

logger = logging.getLogger("unstuck_tracker")


class UnstuckTracker:
    """Tracks peak balance and manages loss realisation budget."""

    def __init__(
        self,
        loss_allowance_pct: float = 4.0,
        min_loss_pct: float = 2.0,
        min_age_minutes: float = 10,
        close_fraction: float = 0.08,
        cooldown_minutes: float = 5,
        twel_pct: float = 15.0,
    ):
        self.loss_allowance_pct = loss_allowance_pct
        self.min_loss_pct = min_loss_pct
        self.min_age_minutes = min_age_minutes
        self.close_fraction = close_fraction
        self.cooldown_minutes = cooldown_minutes
        self.twel_pct = twel_pct

        self._peak_balance: float = 0.0
        self._budget_spent: float = 0.0
        self._last_unstuck: Dict[str, float] = {}  # side -> timestamp
        self._unstuck_count: Dict[str, int] = {}     # side -> count

    @property
    def peak_balance(self) -> float:
        return self._peak_balance

    @property
    def remaining_budget(self) -> float:
        """Remaining loss allowance in USDT."""
        allowance = self._peak_balance * (self.twel_pct / 100) * (self.loss_allowance_pct / 100)
        return max(0.0, allowance - self._budget_spent)

    @property
    def total_allowance(self) -> float:
        """Total loss allowance budget in USDT."""
        return self._peak_balance * (self.twel_pct / 100) * (self.loss_allowance_pct / 100)

    def update_peak(self, balance: float) -> None:
        """Update peak balance if new high reached."""
        if balance > self._peak_balance:
            self._peak_balance = balance

    def reset_peak(self, balance: float) -> None:
        """Reset peak to current balance (after wallet auto-close, etc.)."""
        self._peak_balance = balance
        self._budget_spent = 0.0

    def can_unstuck(
        self,
        symbol: str,
        side: str,
        loss_pct: float,
        position_age_minutes: float,
    ) -> Tuple[bool, str]:
        """
        Check if a position is eligible for partial unstuck close.

        Returns (eligible: bool, reason: str).
        """
        key = f"{symbol}:{side}"

        if loss_pct < self.min_loss_pct:
            return False, f"loss {loss_pct:.1f}% < min {self.min_loss_pct}%"

        if position_age_minutes < self.min_age_minutes:
            return False, f"age {position_age_minutes:.1f}m < min {self.min_age_minutes}m"

        if self.remaining_budget <= 0:
            return False, f"budget exhausted (${self.remaining_budget:.4f})"

        now = time.time()
        last = self._last_unstuck.get(key, 0)
        cooldown_sec = self.cooldown_minutes * 60
        if now - last < cooldown_sec:
            remaining = cooldown_sec - (now - last)
            return False, f"cooldown {remaining:.0f}s remaining"

        return True, "eligible"

    def record_unstuck(
        self,
        symbol: str,
        side: str,
        realised_loss: float,
    ) -> None:
        """
        Record an unstuck close. Deducts realised loss from budget.
        Use positive value for loss (e.g., 0.15 for $0.15 loss).
        """
        key = f"{symbol}:{side}"
        self._last_unstuck[key] = time.time()
        self._unstuck_count[key] = self._unstuck_count.get(key, 0) + 1

        # Deduct from budget
        self._budget_spent += abs(realised_loss)

        logger.info(
            f"📤 UNSTUCK: {symbol} {side} — "
            f"loss ${abs(realised_loss):.4f} | "
            f"budget ${self.remaining_budget:.4f}/${self.total_allowance:.4f} | "
            f"count {self._unstuck_count[key]}"
        )

    def get_unstuck_size(self, position_qty: float) -> float:
        """Calculate how much of the position to close (in qty units)."""
        return position_qty * self.close_fraction

    def status(self) -> dict:
        """Return current unstuck tracker status for monitoring."""
        return {
            "peak_balance": round(self._peak_balance, 2),
            "total_allowance": round(self.total_allowance, 4),
            "budget_spent": round(self._budget_spent, 4),
            "remaining_budget": round(self.remaining_budget, 4),
            "unstuck_count": dict(self._unstuck_count),
            "cooldowns": {
                k: round(max(0, time.time() - t), 0)
                for k, t in self._last_unstuck.items()
            },
        }
