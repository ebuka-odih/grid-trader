"""Hummingbot strategy/config generation helpers.

This module intentionally writes generated configs into an isolated subdirectory
under the Hummingbot home. It does not overwrite the user's active
`conf_perpetual_market_making_1.yml` until the paper adapter is explicitly wired
to do so.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .base import GridDeployRequest

MAX_HUMMINGBOT_LEVERAGE = 100


@dataclass(frozen=True, slots=True)
class SpreadConfig:
    bid_spread: float
    ask_spread: float
    mid_price: float


@dataclass(frozen=True, slots=True)
class GeneratedHummingbotConfig:
    path: Path
    market: str
    exchange: str
    leverage: int
    bid_spread: float
    ask_spread: float


class HummingbotConfigGenerator:
    """Generate Hummingbot PMM-compatible YAML from grid deploy requests."""

    def __init__(self, hummingbot_home: str | Path):
        self.hummingbot_home = Path(hummingbot_home).expanduser()
        self.generated_dir = self.hummingbot_home / "conf" / "generated" / "grid_trader"

    def convert_market(self, symbol: str, exchange: str) -> str:
        """Convert ccxt-like perp symbols into Hummingbot market strings.

        Examples:
        - AAVE/USDC:USDC -> AAVE-USDC for Hyperliquid
        - DOGE/USDT:USDT -> DOGE-USDT for Bybit/Binance perpetuals
        """
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")

        cleaned = symbol.strip().upper()
        cleaned = cleaned.split(":", 1)[0]
        cleaned = cleaned.replace("/", "-")
        cleaned = re.sub(r"[^A-Z0-9-]", "", cleaned)

        if "-" not in cleaned:
            quote = "USDC" if exchange == "hyperliquid_perpetual" else "USDT"
            cleaned = f"{cleaned}-{quote}"

        return cleaned

    def calculate_spreads(self, lower: float, upper: float) -> SpreadConfig:
        if lower <= 0 or upper <= 0 or upper <= lower:
            raise ValueError("upper must be greater than positive lower")
        mid_price = (lower + upper) / 2.0
        bid_spread = ((mid_price - lower) / mid_price) * 100.0
        ask_spread = ((upper - mid_price) / mid_price) * 100.0
        return SpreadConfig(
            bid_spread=round(bid_spread, 6),
            ask_spread=round(ask_spread, 6),
            mid_price=mid_price,
        )

    def write_strategy_config(self, request: GridDeployRequest) -> GeneratedHummingbotConfig:
        market = self.convert_market(request.symbol, request.exchange)
        leverage = min(int(request.leverage), MAX_HUMMINGBOT_LEVERAGE)
        spreads = self.calculate_spreads(request.lower, request.upper)
        self.generated_dir.mkdir(parents=True, exist_ok=True)

        safe_symbol = re.sub(r"[^A-Z0-9_-]", "_", market)
        timestamp = int(time.time() * 1000)
        path = self.generated_dir / f"conf_grid_trader_{safe_symbol}_{timestamp}.yml"

        content = self.render_strategy_config(
            request=request,
            market=market,
            leverage=leverage,
            spreads=spreads,
        )
        path.write_text(content)

        return GeneratedHummingbotConfig(
            path=path,
            market=market,
            exchange=request.exchange,
            leverage=leverage,
            bid_spread=spreads.bid_spread,
            ask_spread=spreads.ask_spread,
        )

    def render_strategy_config(
        self,
        request: GridDeployRequest,
        market: str,
        leverage: int,
        spreads: SpreadConfig,
    ) -> str:
        """Render simple YAML accepted by Hummingbot PMM-style configs.

        Extra grid-trader fields are included for the adapter/state reader. They
        are harmless metadata until a custom script strategy consumes them.
        """
        position_mode = "ONEWAY"
        lines = [
            "strategy: perpetual_market_making",
            f"exchange: {request.exchange}",
            f"market: {market}",
            f"bid_spread: {spreads.bid_spread}",
            f"ask_spread: {spreads.ask_spread}",
            f"order_amount: {float(request.margin_per_level_usdt)}",
            f"grid_levels: {int(request.num_grids)}",
            f"leverage: {leverage}",
            f"position_mode: {position_mode}",
            "# grid-trader adapter metadata",
            f"source_symbol: {request.symbol}",
            f"direction: {request.direction}",
            f"lower_price: {float(request.lower)}",
            f"upper_price: {float(request.upper)}",
            f"cross_margin: {str(bool(request.cross_margin)).lower()}",
            "",
        ]
        return "\n".join(lines)
