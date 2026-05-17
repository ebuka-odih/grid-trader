"""
Portfolio Risk Monitor — the core risk management module for cross-margin trading.
"""

import json
import logging
import os
from typing import Optional

from config import (
    DEFAULT_LEVERAGE,
    MAX_SAFE_LEVERAGE,
    MAX_TRADE_WALLET_EXPOSURE_PCT,
    MIN_ORDER_SIZE_USDT,
    clamp_leverage,
    resolve_profile_leverage,
    resolve_profile_max_leverage,
)

logger = logging.getLogger("portfolio_risk_monitor")


class PortfolioRiskMonitor:
    """Portfolio-level risk gate for cross-margin grid trading."""

    def __init__(self, profiles_path: str = "token_profiles.json"):
        self.profiles_path = profiles_path
        self.profiles = {}
        self.portfolio_config = {}
        self.defaults = {}
        self.blacklist = []
        self.correlation_groups = []
        self._load_profiles()
        logger.info(f"🛡️ Portfolio Risk Monitor initialized | profiles={len(self.profiles)}")

    def _load_profiles(self):
        """Load token profiles from JSON file."""
        try:
            with open(self.profiles_path, "r") as f:
                data = json.load(f)
            raw_profiles = data.get("profiles", data)
            self.profiles = {k: v for k, v in raw_profiles.items() if isinstance(v, dict) and not k.startswith("_")}
            self.portfolio_config = data.get("portfolio", {})
            self.blacklist = data.get("blacklist", [])
            self.correlation_groups = self.portfolio_config.get("correlation_groups", [])
            self.defaults = self.portfolio_config.get("default_token_profile", {}) or {
                "leverage": MAX_SAFE_LEVERAGE,
                "max_leverage": MAX_SAFE_LEVERAGE,
                "max_wallet_exposure_pct": MAX_TRADE_WALLET_EXPOSURE_PCT,
                "order_size_usdt": MIN_ORDER_SIZE_USDT,
                "num_grids": 10,
                "target_pnl_pct": [2.0, 4.0],
            }
        except FileNotFoundError:
            logger.warning(f"⚠️ Token profiles not found")
            self.defaults = {
                "leverage": MAX_SAFE_LEVERAGE,
                "max_leverage": MAX_SAFE_LEVERAGE,
                "max_wallet_exposure_pct": MAX_TRADE_WALLET_EXPOSURE_PCT,
                "order_size_usdt": MIN_ORDER_SIZE_USDT,
                "num_grids": 10,
            }
            self.portfolio_config = {"max_total_wallet_exposure_pct": 80}

    def get_token_profile(self, symbol: str) -> dict:
        base = symbol.split("/")[0]
        profile = dict(self.profiles.get(symbol) or self.profiles.get(base) or self.defaults)
        profile["max_leverage"] = resolve_profile_max_leverage(profile)
        profile["leverage"] = resolve_profile_leverage(profile, fallback=DEFAULT_LEVERAGE)
        profile["max_wallet_exposure_pct"] = min(
            float(profile.get("max_wallet_exposure_pct", MAX_TRADE_WALLET_EXPOSURE_PCT)),
            MAX_TRADE_WALLET_EXPOSURE_PCT,
        )
        return profile

    def get_direction_bias(self, symbol: str) -> str:
        """Return token-specific direction bias for fallback scanner deployments."""
        profile = self.get_token_profile(symbol)
        bias = str(profile.get("direction_bias", "neutral") or "neutral").lower()
        return bias if bias in {"long", "short", "neutral"} else "neutral"

    def is_blacklisted(self, symbol: str) -> bool:
        base = symbol.split("/")[0]
        return base in self.blacklist

    def check_deploy(self, symbol: str, direction: str, leverage: int, order_size_usdt: float,
                     wallet_balance: float, active_grids: dict, num_grids: int = None,
                     max_trade_pct_override: float = None) -> dict:
        """Check if deployment is allowed."""
        reasons, warnings = [], []
        profile = self.get_token_profile(symbol)
        num_grids = num_grids or 10
        
        if self.is_blacklisted(symbol):
            return {"approved": False, "adjusted_leverage": leverage, "adjusted_order_size": order_size_usdt,
                    "reasons": [f"{symbol} is blacklisted"], "warnings": []}

        # Adjusted leverage
        adjusted_leverage = clamp_leverage(leverage, maximum=resolve_profile_max_leverage(profile))
        adjusted_order_size = order_size_usdt

        level_count = self.RESERVE_BUFFER_LEVELS + 1  # reserve for new grid: buffer + 1 initial fill

        # Calculate position exposure based on reserve buffer, not full grid capacity.
        position_margin = adjusted_order_size * level_count
        position_wallet_pct = (position_margin / wallet_balance) * 100 if wallet_balance > 0 else 0

        # Use the explicit global/per-deploy cap as a hard wallet-reserved-margin
        # limit. Token profiles may be looser, but they must not override the
        # system-level cap when the goal is many small concurrent grids.
        config_pct = float(self.portfolio_config.get("max_trade_wallet_exposure_pct", MAX_TRADE_WALLET_EXPOSURE_PCT))
        if max_trade_pct_override is not None:
            config_pct = float(max_trade_pct_override)
        profile_pct = float(profile.get("max_wallet_exposure_pct", config_pct))
        max_trade_pct = min(config_pct, profile_pct)

        # Apply cap if needed
        if position_wallet_pct > max_trade_pct:
            cap_per_level = (max_trade_pct / 100 * wallet_balance) / level_count
            adjusted_order_size = round(max(MIN_ORDER_SIZE_USDT, cap_per_level), 4)
            warnings.append(f"Size reduced to ${adjusted_order_size:.2f} ({position_wallet_pct:.1f}% > {max_trade_pct}%)")
            position_margin = adjusted_order_size * level_count
            position_wallet_pct = (position_margin / wallet_balance) * 100

        # Check total exposure. active_grids is a dict of GridSlot objects in the
        # live multi-grid manager, but older tests/tools may pass plain dicts.
        # Keep this adapter here so risk checks do not crash the trading loop.
        total_exposure = sum(
            self._grid_exposure_pct(g, wallet_balance, reserved=True)
            for g in active_grids.values()
        )
        max_total = self.portfolio_config.get("max_total_wallet_exposure_pct", 80)
        if total_exposure + position_wallet_pct > max_total:
            available = max_total - total_exposure
            if available > 0:
                cap = (available / 100 * wallet_balance) / level_count
                adjusted_order_size = round(min(adjusted_order_size, max(MIN_ORDER_SIZE_USDT, cap)), 4)
                warnings.append(f"Total cap reduced to ${adjusted_order_size:.2f}")
            else:
                reasons.append(f"Total exposure {total_exposure:.1f}% at max")

        approved = len(reasons) == 0
        if not approved:
            logger.warning(f"🛡️ DEPLOY REJECTED: {symbol} | {reasons}")
        elif warnings:
            logger.info(f"🛡️ DEPLOY APPROVED (adjusted): {symbol} | size=${order_size_usdt:.1f}→${adjusted_order_size:.1f}")

        return {
            "approved": approved,
            "adjusted_leverage": adjusted_leverage,
            "adjusted_order_size": adjusted_order_size,
            "reasons": reasons,
            "warnings": warnings,
        }

    def check_emergency(self, wallet_balance: float, active_grids: dict) -> dict:
        """Emergency check - return grids to close if wallet at risk."""
        total_exposure = sum(
            self._grid_exposure_pct(g, wallet_balance, reserved=True)
            for g in active_grids.values()
        )

        max_total = self.portfolio_config.get("max_total_wallet_exposure_pct", 80)
        buffer = self.portfolio_config.get("emergency_liquidation_buffer_pct", 10)

        if total_exposure > max_total - buffer:
            return {"emergency": True, "message": f"Exposure {total_exposure:.1f}% > {max_total - buffer}%"}
        return {"emergency": False}

    def _grid_direction(self, grid) -> str:
        """Extract direction from GridSlot-like objects or dict state."""
        if isinstance(grid, dict):
            return grid.get("direction") or grid.get("side") or "neutral"
        decision = getattr(grid, "decision", None)
        if decision is not None:
            return getattr(decision, "direction", "neutral") or "neutral"
        position = getattr(grid, "position", None)
        if isinstance(position, dict):
            return position.get("direction", "neutral")
        return "neutral"

    # How many extra levels to reserve beyond actual fills.
    # Empty grids still reserve a small buffer for immediate fill headroom.
    RESERVE_BUFFER_LEVELS = 3

    def _grid_exposure_pct(self, grid, wallet_balance: float, reserved: bool = False) -> float:
        """Return wallet exposure percentage for dicts and GridSlot objects.

        Exposure is based on ACTUAL fills + a small reserve buffer (3 levels),
        NOT the full grid capacity. This lets the portfolio run many concurrent
        grids without blowing the exposure budget on empty reservations.
        """
        if wallet_balance <= 0:
            return 0.0

        if isinstance(grid, (int, float)):
            return float(grid)

        if isinstance(grid, dict):
            explicit = grid.get("position_wallet_pct") or grid.get("wallet_exposure_pct")
            if explicit is not None:
                try:
                    return float(explicit)
                except (TypeError, ValueError):
                    return 0.0
            order_size = float(grid.get("adjusted_order_size") or grid.get("order_size_usdt") or grid.get("order_size") or 0.0)
            fills = int(grid.get("fills") or grid.get("fills_count") or 0)
            if reserved:
                level_count = max(1, fills + self.RESERVE_BUFFER_LEVELS)
            else:
                level_count = fills
            return (order_size * max(0, level_count) / wallet_balance) * 100

        # GridSlot-like object from multi_grid_manager.py
        explicit = getattr(grid, "position_wallet_pct", None)
        if explicit is not None:
            try:
                return float(explicit)
            except (TypeError, ValueError):
                return 0.0

        order_size = float(getattr(grid, "adjusted_order_size", 0.0) or 0.0)
        fills = int(getattr(grid, "fills", 0) or 0)
        if reserved:
            level_count = max(1, fills + self.RESERVE_BUFFER_LEVELS)
        else:
            level_count = fills
        return (order_size * max(0, level_count) / wallet_balance) * 100

    def get_portfolio_exposure(self, active_grids: dict, wallet_balance: float) -> dict:
        """Calculate portfolio-wide exposure across all active grids."""
        long_exposure = 0.0
        short_exposure = 0.0
        neutral_exposure = 0.0

        for g in active_grids.values():
            direction = self._grid_direction(g)
            pct = self._grid_exposure_pct(g, wallet_balance, reserved=False)
            if direction == 'long':
                long_exposure += pct
            elif direction == 'short':
                short_exposure += pct
            else:
                neutral_exposure += pct

        return {
            'long_exposure_pct': long_exposure,
            'short_exposure_pct': short_exposure,
            'neutral_exposure_pct': neutral_exposure,
            'total_exposure_pct': long_exposure + short_exposure + neutral_exposure,
            'group_exposures': {}
        }

    def _get_base_symbol(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def _get_correlation_group(self, base: str) -> Optional[dict]:
        for g in self.correlation_groups:
            if base in g.get("symbols", []):
                return g
        return None
