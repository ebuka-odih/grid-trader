"""
Wallet Tracker — tracks wallet balance, per-position exposure, and total unrealized PnL
for a cross-margin Bybit grid trading bot.

With cross margin, all positions share one wallet balance. This tracker provides:
- Current balance (simulated for dry-run, live for real trading)
- Total unrealized PnL across all open positions
- Per-position exposure (notional value, margin used)
- Available margin calculation
- Liquidation risk assessment

The wallet state drives the Portfolio Risk Monitor's decisions.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("wallet_tracker")


@dataclass
class PositionExposure:
    """Tracks a single position's exposure data."""
    symbol: str
    direction: str
    order_size_usdt: float
    leverage: int
    notional_value: float = 0.0
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0
    num_fills: int = 0


class WalletTracker:
    """
    Tracks wallet state for cross-margin grid trading.

    For dry-run: simulates balance starting at initial_balance.
    When grids close, their realized PnL gets added to the balance.
    For live trading: would query Bybit API for actual wallet state.
    """

    def __init__(self, initial_balance: float = 100.0):
        self.initial_balance = initial_balance
        self._balance = initial_balance
        self._realized_pnl_total = 0.0
        self.positions: dict[str, PositionExposure] = {}
        self._realized_pnls: list[float] = []  # history of all realized PnLs
        self._live_balance_mode = False
        self._external_available_margin: Optional[float] = None
        self._external_margin_used: Optional[float] = None

        logger.info(f"💰 Wallet Tracker initialized | balance=${initial_balance:.2f} | mode=dry-run")

    def set_live_balance(
        self,
        *,
        equity: float,
        available_margin: Optional[float] = None,
        margin_used: Optional[float] = None,
    ):
        """
        Override wallet values from exchange in live mode.

        The first live sync re-anchors `initial_balance` so pnl% and exposure
        are measured against the real account, not the dry-run default.
        """
        if equity is None:
            return
        try:
            equity_f = float(equity)
        except (TypeError, ValueError):
            return
        if equity_f <= 0:
            return

        if not self._live_balance_mode:
            self.initial_balance = equity_f
            self._live_balance_mode = True
            logger.info(
                f"💰 Live wallet mode enabled | initial=${self.initial_balance:.4f}"
            )

        self._balance = equity_f
        if available_margin is not None:
            try:
                self._external_available_margin = float(available_margin)
            except (TypeError, ValueError):
                pass
        if margin_used is not None:
            try:
                self._external_margin_used = float(margin_used)
            except (TypeError, ValueError):
                pass

    def restore_realized_pnl(self, pnl_total: float):
        """
        Seed accumulated realized PnL at startup (e.g. from the trades DB).

        Container restarts must not wipe wallet gains/losses: this call lets
        the manager replay the closed-trade total onto a fresh tracker so the
        balance reflects history even when the in-memory state was lost.
        """
        if pnl_total == 0:
            return
        self._balance += pnl_total
        self._realized_pnl_total += pnl_total
        # Single bucket entry — we do not have per-trade history here, just totals.
        self._realized_pnls.append(pnl_total)
        logger.info(
            f"💰 Wallet restored from DB | replayed_pnl=${pnl_total:+.4f} → "
            f"balance=${self._balance:.2f} (initial ${self.initial_balance:.2f})"
        )

    # ── Position Management ───────────────────────────────────

    def update_position(
        self,
        symbol: str,
        direction: str,
        order_size_usdt: float,
        leverage: int,
        unrealized_pnl: float = 0.0,
        num_fills: int = 0,
    ):
        """
        Update or add a position's exposure data.

        Calculates notional_value and margin_used from order params.
        Called every time a grid's status changes (new fill, PnL update, etc.)
        """
        # Notional = approximate total position value
        # For grid: each fill creates a position, so notional scales with fills
        active_levels = max(num_fills, 1)
        notional_value = order_size_usdt * leverage * active_levels
        # order_size_usdt is margin per grid level. Leverage changes simulated
        # notional/quantity, but wallet exposure and liquidation-risk display
        # should reserve margin, not multiply exposure by leverage.
        margin_used = order_size_usdt * active_levels

        if symbol in self.positions:
            pos = self.positions[symbol]
            pos.direction = direction
            pos.order_size_usdt = order_size_usdt
            pos.leverage = leverage
            pos.notional_value = notional_value
            pos.margin_used = margin_used
            pos.unrealized_pnl = unrealized_pnl
            pos.num_fills = num_fills
        else:
            self.positions[symbol] = PositionExposure(
                symbol=symbol,
                direction=direction,
                order_size_usdt=order_size_usdt,
                leverage=leverage,
                notional_value=notional_value,
                margin_used=margin_used,
                unrealized_pnl=unrealized_pnl,
                num_fills=num_fills,
            )

    def remove_position(self, symbol: str, realized_pnl: float = 0.0):
        """
        Remove a closed position and add its realized PnL to balance.
        """
        if symbol in self.positions:
            del self.positions[symbol]
            self.add_realized_pnl(realized_pnl)
            logger.info(f"💰 Position removed: {symbol} | realized_pnl=${realized_pnl:.4f} | balance=${self._balance:.2f}")
        else:
            logger.warning(f"💰 Position not found for removal: {symbol}")

    # ── Wallet State ──────────────────────────────────────────

    def get_wallet_state(self) -> dict:
        """
        Get full wallet state snapshot.

        Returns: {
            balance: float,              # current balance (initial + all realized PnL)
            total_unrealized_pnl: float, # sum of all position unrealized PnLs
            total_exposure_usdt: float,  # sum of reserved margin across positions
            total_margin_used: float,    # sum of all margin used
            available_margin: float,     # balance - margin_used (cross margin)
            positions: dict,             # per-position exposure data
            exposure_pct: float,         # total_exposure / balance * 100
            free_pct: float,            # available_margin / balance * 100
        }
        """
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        total_margin = sum(p.margin_used for p in self.positions.values())
        if self._external_margin_used is not None:
            total_margin = max(total_margin, max(0.0, self._external_margin_used))
        total_exposure = total_margin
        if self._external_available_margin is not None:
            available = self._external_available_margin
        else:
            available = self._balance - total_margin + total_unrealized

        exposure_pct = (total_exposure / self._balance * 100) if self._balance > 0 else 0
        free_pct = max(0, (available / self._balance * 100)) if self._balance > 0 else 0

        return {
            "balance": round(self._balance, 4),
            "initial_balance": round(self.initial_balance, 4),
            "total_unrealized_pnl": round(total_unrealized, 4),
            "total_realized_pnl": round(self._realized_pnl_total, 4),
            "total_exposure_usdt": round(total_exposure, 4),
            "total_margin_used": round(total_margin, 4),
            "available_margin": round(max(0, available), 4),
            "positions": {
                sym: {
                    "direction": p.direction,
                    "order_size_usdt": p.order_size_usdt,
                    "leverage": p.leverage,
                    "notional_value": round(p.notional_value, 4),
                    "margin_used": round(p.margin_used, 4),
                    "unrealized_pnl": round(p.unrealized_pnl, 4),
                    "num_fills": p.num_fills,
                }
                for sym, p in self.positions.items()
            },
            "position_count": len(self.positions),
            "exposure_pct": round(exposure_pct, 2),
            "free_pct": round(free_pct, 2),
        }

    def get_balance(self) -> float:
        """Return current balance (initial + all realized PnL)."""
        return self._balance

    def add_realized_pnl(self, pnl: float):
        """Add realized PnL to balance (when a grid closes)."""
        self._balance += pnl
        self._realized_pnl_total += pnl
        self._realized_pnls.append(pnl)
        logger.info(f"💰 Balance updated: ${pnl:+.4f} → balance=${self._balance:.2f}")

    # ── Risk Assessment ───────────────────────────────────────

    def check_liquidation_risk(self) -> dict:
        """
        Assess liquidation risk for the cross-margin wallet.

        Returns: {
            at_risk: bool,
            risk_level: str,  # 'safe', 'warning', 'danger', 'critical'
            details: str,
            exposure_pct: float,
        }
        """
        state = self.get_wallet_state()
        exposure_pct = state["exposure_pct"]

        if exposure_pct > 95:
            level = "critical"
            at_risk = True
            details = f"Exposure at {exposure_pct:.1f}% — IMMINENT LIQUIDATION RISK"
        elif exposure_pct > 80:
            level = "danger"
            at_risk = True
            details = f"Exposure at {exposure_pct:.1f}% — HIGH LIQUIDATION RISK"
        elif exposure_pct > 60:
            level = "warning"
            at_risk = False
            details = f"Exposure at {exposure_pct:.1f}% — elevated risk, reduce positions"
        else:
            level = "safe"
            at_risk = False
            details = f"Exposure at {exposure_pct:.1f}% — within safe limits"

        return {
            "at_risk": at_risk,
            "risk_level": level,
            "details": details,
            "exposure_pct": exposure_pct,
        }

    def get_position_exposure_pct(self, symbol: str) -> float:
        """What % of wallet does this single position represent."""
        if symbol not in self.positions or self._balance <= 0:
            return 0.0
        return (self.positions[symbol].margin_used / self._balance) * 100

    def get_realized_pnl_history(self) -> list[float]:
        """Get all realized PnLs for analysis."""
        return list(self._realized_pnls)

    def get_stats(self) -> dict:
        """Get wallet performance statistics."""
        if not self._realized_pnls:
            return {
                "total_trades": 0,
                "total_pnl": 0,
                "win_rate": 0,
                "avg_pnl": 0,
                "best_trade": 0,
                "worst_trade": 0,
            }

        wins = sum(1 for p in self._realized_pnls if p > 0)
        total = len(self._realized_pnls)

        return {
            "total_trades": total,
            "total_pnl": round(self._realized_pnl_total, 4),
            "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
            "avg_pnl": round(self._realized_pnl_total / total, 4),
            "best_trade": round(max(self._realized_pnls), 4),
            "worst_trade": round(min(self._realized_pnls), 4),
            "balance": round(self._balance, 4),
            "pnl_pct": round((self._balance - self.initial_balance) / self.initial_balance * 100, 2),
        }
