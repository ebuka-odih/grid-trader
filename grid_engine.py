"""
Grid Engine — the core strategy module.
Calculates grid levels, places limit orders on Bybit, and manages the grid lifecycle.

Grid Trading Logic:
  - Given upper/lower bounds and N grid levels, evenly space limit buy/sell orders
  - Each grid level has a BUY order below current price and a SELL order above
  - As price moves through levels, orders fill → profit from the spread
  - Target PnL: $1-2 per grid cycle, then close all positions
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

import ccxt.async_support as ccxt

from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, TRADING_MODE,
    DEFAULT_LEVERAGE, DEFAULT_NUM_GRIDS,
    TARGET_PNL_LOW, TARGET_PNL_HIGH,
    BASE_ORDER_SIZE_USDT, DRY_RUN,
)

from coin_scanner import CoinScore

logger = logging.getLogger("grid_engine")


@dataclass
class GridLevel:
    """A single grid level with its order details."""
    index: int
    price: float
    side: str          # "Buy" or "Sell"
    order_id: str = ""
    status: str = "pending"  # pending, placed, filled, cancelled
    qty: float = 0.0


@dataclass
class GridState:
    """Tracks the full state of an active grid."""
    symbol: str
    upper_price: float
    lower_price: float
    num_grids: int
    leverage: int
    grid_levels: list[GridLevel] = field(default_factory=list)
    order_size_usdt: float = BASE_ORDER_SIZE_USDT  # margin allocated per grid level
    total_pnl: float = 0.0
    position_qty: float = 0.0
    entry_price: float = 0.0
    is_active: bool = False
    grid_id: str = ""


class GridEngine:
    """Places and manages grid orders on Bybit."""

    def __init__(self):
        exchange_opts = {
            "apiKey": BYBIT_API_KEY,
            "secret": BYBIT_API_SECRET,
            "enableRateLimit": True,
        }
        if TRADING_MODE == "testnet":
            self.exchange = ccxt.bybit({
                **exchange_opts,
                "options": {"defaultType": "linear"},
                "urls": {"api": {"public": "https://api-testnet.bybit.com", "private": "https://api-testnet.bybit.com"}},
            })
        else:
            self.exchange = ccxt.bybit({**exchange_opts, "options": {"defaultType": "linear"}})
        self.active_grid: Optional[GridState] = None
        # Cache for symbol precision
        self._precision_cache: dict = {}

    async def close(self):
        await self.exchange.close()

    # ── Symbol Precision Helpers ───────────────────────────────────

    async def _get_symbol_precision(self, symbol: str) -> dict:
        """Get price and quantity precision for a symbol from exchange."""
        if symbol in self._precision_cache:
            return self._precision_cache[symbol]
        
        try:
            markets = await self.exchange.load_markets()
            if symbol in markets:
                market = markets[symbol]
                precision = {
                    'price': market.get('precision', {}).get('price', 4),
                    'amount': market.get('precision', {}).get('amount', 3),
                    'min_amount': market.get('limits', {}).get('amount', {}).get('min', 0.001),
                    'min_cost': market.get('limits', {}).get('cost', {}).get('min', 0.0),
                }
                self._precision_cache[symbol] = precision
                return precision
            else:
                # Fallback defaults
                return {'price': 4, 'amount': 3, 'min_amount': 0.001, 'min_cost': 0.0}
        except Exception as e:
            logger.warning(f"Failed to get precision for {symbol}: {e}")
            return {'price': 4, 'amount': 3, 'min_amount': 0.001, 'min_cost': 0.0}

    def _round_to_precision(self, value: float, precision: int) -> float:
        """Round value to specified decimal places."""
        if precision < 0:
            return value
        factor = 10 ** precision
        return round(value * factor) / factor

    # ── Grid Calculation ───────────────────────────────────

    def calculate_grid_levels(
        self,
        symbol: str,
        upper: float,
        lower: float,
        num_grids: int,
        current_price: float,
        leverage: int = DEFAULT_LEVERAGE,
        order_size_usdt: float = BASE_ORDER_SIZE_USDT,
        exp_sizing_gamma: float = 0.0,
    ) -> GridState:
        """Calculate evenly-spaced grid levels with symbol-aware precision.
        
        v3: Supports exponential level sizing (exp_sizing_gamma > 0).
        When gamma > 0, levels closer to current price get larger orders.
        """
        # Calculate quantity per grid level
        # Each order_size_usdt is margin allocated per grid level. Leverage
        # controls the simulated/live notional exposure for that level.
        notional_per_level = order_size_usdt * leverage
        qty_per_level = notional_per_level / current_price
        
        # Get symbol-specific precision
        price_precision = 4  # Default for most USDT pairs
        qty_precision = 3    # Default quantity precision
        min_qty = 0.001      # Default minimum quantity
        
        # Round to exchange precision
        qty_per_level = self._round_to_precision(qty_per_level, qty_precision)
        # Ensure minimum qty
        qty_per_level = max(qty_per_level, min_qty)

        step = (upper - lower) / num_grids
        levels = []
        
        # v3: Pre-compute exponential sizing factors
        import math
        total_range = upper - lower

        for i in range(num_grids + 1):
            price = self._round_to_precision(lower + step * i, price_precision)
            if abs(price - current_price) < step * 0.3:
                continue  # skip level too close to current price

            side = "Buy" if price < current_price else "Sell"
            
            # v3: Exponential level sizing
            if exp_sizing_gamma > 0:
                dist = abs(price - current_price) / max(total_range, 1e-8)
                dist = min(dist, 1.0)
                factor = math.exp(-exp_sizing_gamma * dist * 3)
                factor = max(0.3, min(1.0, factor))  # clamp 30%-100%
                level_qty = self._round_to_precision(qty_per_level * factor, qty_precision)
                level_qty = max(level_qty, min_qty)
            else:
                level_qty = qty_per_level
            
            levels.append(GridLevel(
                index=i,
                price=price,
                side=side,
                qty=level_qty,
            ))

        grid = GridState(
            symbol=symbol,
            upper_price=upper,
            lower_price=lower,
            num_grids=num_grids,
            leverage=leverage,
            grid_levels=levels,
            order_size_usdt=order_size_usdt,
            grid_id=(
                f"grid_{symbol.replace('/', '_').replace(':', '')}_"
                f"{int(current_price)}_{time.time_ns()}"
            ),
        )

        logger.info(f"📊 Grid calculated: {symbol} | {lower:.{price_precision}f}-{upper:.{price_precision}f} | "
                     f"{len(levels)} levels | lev={leverage}x | "
                     f"order=${order_size_usdt:.2f} | qty/level={qty_per_level}")

        return grid

    # ── Set Leverage ───────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int):
        """Set leverage for a symbol on Bybit."""
        try:
            # ccxt unified: set_leverage
            await self.exchange.set_leverage(leverage, symbol)
            logger.info(f"⚡ Leverage set to {leverage}x for {symbol}")
        except Exception as e:
            # Bybit sometimes errors if leverage already set — that's fine
            logger.warning(f"Leverage set warning for {symbol}: {e}")

    # ── Place Grid Orders ──────────────────────────────────

    async def deploy_grid(self, coin_score: CoinScore) -> GridState:
        """Deploy a full grid based on a CoinScore from the scanner."""
        symbol = coin_score.symbol
        upper = coin_score.suggested_upper
        lower = coin_score.suggested_lower
        num_grids = coin_score.suggested_grids
        leverage = coin_score.suggested_leverage
        price = coin_score.price

        # Calculate grid
        grid = self.calculate_grid_levels(symbol, upper, lower, num_grids, price, leverage)

        # Safety: in dry-run mode, never touch private exchange endpoints.
        # Use mainnet/testnet public market data elsewhere, but simulate order placement here.
        if DRY_RUN:
            for level in grid.grid_levels:
                level.status = "placed"
            grid.is_active = True
            self.active_grid = grid
            logger.info(
                f"🧪 DRY-RUN Grid simulated: {symbol} | "
                f"{len(grid.grid_levels)}/{len(grid.grid_levels)} simulated orders | "
                f"no private Bybit calls made"
            )
            return grid

        # Set leverage
        await self.set_leverage(symbol, leverage)

        # Place all limit orders
        for level in grid.grid_levels:
            try:
                order = await self.exchange.create_limit_order(
                    symbol=symbol,
                    side=level.side.lower(),  # ccxt uses "buy"/"sell"
                    amount=level.qty,
                    price=level.price,
                )
                level.order_id = order["id"]
                level.status = "placed"
                logger.info(f"  ✅ {level.side} {level.qty} @ {level.price:.4f} → {order['id']}")
            except Exception as e:
                level.status = "failed"
                logger.error(f"  ❌ {level.side} {level.qty} @ {level.price:.4f} → {e}")

        grid.is_active = True
        self.active_grid = grid

        placed = sum(1 for l in grid.grid_levels if l.status == "placed")
        logger.info(f"🎯 Grid deployed: {placed}/{len(grid.grid_levels)} orders placed for {symbol}")

        return grid

    async def quick_deploy(self, coin_score: CoinScore) -> GridState:
        """Backward-compatible alias for deploy_grid used by legacy dry-run paths/tests."""
        return await self.deploy_grid(coin_score)

    # ── Cancel All Grid Orders ─────────────────────────────────

    async def cancel_grid(self, grid: Optional[GridState] = None):
        """Cancel all open orders in the grid."""
        grid = grid or self.active_grid
        if not grid:
            return

        for level in grid.grid_levels:
            if level.status == "placed" and level.order_id:
                try:
                    await self.exchange.cancel_order(level.order_id, grid.symbol)
                    level.status = "cancelled"
                    logger.info(f"  🗑️ Cancelled {level.side} @ {level.price:.4f}")
                except Exception as e:
                    # Order might already be filled or cancelled
                    logger.warning(f"  Cancel failed for {level.order_id}: {e}")
                    level.status = "unknown"

    # ── Close Position ─────────────────────────────────────

    async def close_position(self, symbol: str, side: str, qty: float):
        """Close the current position with a market order."""
        close_side = "sell" if side == "Buy" else "buy"
        try:
            # Reduce-only market order
            order = await self.exchange.create_market_order(
                symbol=symbol,
                side=close_side,
                amount=qty,
                params={"reduceOnly": True},
            )
            logger.info(f"🔴 Position closed: {close_side} {qty} @ market → {order['id']}")
            return order
        except Exception as e:
            logger.error(f"Close position failed: {e}")
            return None

    # ── Full Grid Cleanup ──────────────────────────────────

    async def shutdown_grid(self):
        """Cancel all orders and close any open position."""
        if not self.active_grid:
            return
        grid = self.active_grid

        # Cancel all pending orders
        await self.cancel_grid(grid)

        # Close position if any
        if grid.position_qty > 0:
            await self.close_position(grid.symbol, "Buy" if grid.position_qty > 0 else "Sell", abs(grid.position_qty))

        grid.is_active = False
        logger.info(f"🛑 Grid shut down: {grid.grid_id}")
        self.active_grid = None