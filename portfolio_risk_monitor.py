"""
Portfolio Risk Monitor — the core risk management module for cross-margin trading.
"""

import json
import logging
import os
from typing import Optional

from config import MIN_SAFE_LEVERAGE, MAX_SAFE_LEVERAGE, MAX_TRADE_WALLET_EXPOSURE_PCT, MIN_ORDER_SIZE_USDT

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
            self.profiles = {k: v for k, v in data.items() if not k.startswith("_")}
            self.portfolio_config = data.get("portfolio", {})
            self.blacklist = data.get("blacklist", [])
            self.correlation_groups = self.portfolio_config.get("correlation_groups", [])
        except FileNotFoundError:
            logger.warning(f"⚠️ Token profiles not found")
            self.defaults = {"leverage": 50, "max_wallet_exposure_pct": 60.0}
            self.portfolio_config = {"max_total_wallet_exposure_pct": 80}

    def get_token_profile(self, symbol: str) -> dict:
        return self.profiles.get(symbol, self.defaults)

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
        max_allowed = max(MIN_SAFE_LEVERAGE, min(leverage, MAX_SAFE_LEVERAGE))
        adjusted_leverage = max(MIN_SAFE_LEVERAGE, min(leverage, max_allowed))
        adjusted_order_size = order_size_usdt

        # Calculate position exposure
        position_margin = adjusted_order_size * num_grids
        position_wallet_pct = (position_margin / wallet_balance) * 100 if wallet_balance > 0 else 0

        # KEY FIX: Use global config, not restrictive token profile
        config_pct = float(self.portfolio_config.get("max_trade_wallet_exposure_pct", MAX_TRADE_WALLET_EXPOSURE_PCT))
        profile_pct = float(profile.get("max_wallet_exposure_pct", 60.0))
        max_trade_pct = max(config_pct, profile_pct)  # Use higher of the two

        # Apply cap if needed
        if position_wallet_pct > max_trade_pct:
            cap_per_level = (max_trade_pct / 100 * wallet_balance) / num_grids
            adjusted_order_size = max(MIN_ORDER_SIZE_USDT, cap_per_level)
            warnings.append(f"Size reduced to ${adjusted_order_size:.2f} ({position_wallet_pct:.1f}% > {max_trade_pct}%)")
            position_margin = adjusted_order_size * num_grids
            position_wallet_pct = (position_margin / wallet_balance) * 100

        # Check total exposure
        total_exposure = sum(g.get("position_wallet_pct", 0) for g in active_grids.values())
        max_total = self.portfolio_config.get("max_total_wallet_exposure_pct", 80)
        if total_exposure + position_wallet_pct > max_total:
            available = max_total - total_exposure
            if available > 0:
                cap = (available / 100 * wallet_balance) / num_grids
                adjusted_order_size = min(adjusted_order_size, max(MIN_ORDER_SIZE_USDT, cap))
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
        # Handle GridSlot objects or dicts
        total_exposure = 0
        for g in active_grids.values():
            if hasattr(g, 'position_wallet_pct'):  # GridSlot object
                total_exposure += g.position_wallet_pct or 0
            elif isinstance(g, dict):
                total_exposure += g.get("position_wallet_pct", 0)
            elif isinstance(g, (int, float)):
                total_exposure += g
                
        max_total = self.portfolio_config.get("max_total_wallet_exposure_pct", 80)
        buffer = self.portfolio_config.get("emergency_liquidation_buffer_pct", 10)
        
        if total_exposure > max_total - buffer:
            return {"emergency": True, "message": f"Exposure {total_exposure:.1f}% > {max_total - buffer}%"}
        return {"emergency": False}

    def _get_base_symbol(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def _get_correlation_group(self, base: str) -> Optional[dict]:
        for g in self.correlation_groups:
            if base in g.get("symbols", []):
                return g
        return None
