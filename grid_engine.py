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
    spacing_mode: str = "balanced"
    entry_shape_template: str = "atr_box"
    buy_density_bias: float = 0.5
    sell_density_bias: float = 0.5
    grid_levels: list[GridLevel] = field(default_factory=list)
    order_size_usdt: float = BASE_ORDER_SIZE_USDT  # margin allocated per grid level
    total_pnl: float = 0.0
    position_qty: float = 0.0
    entry_price: float = 0.0
    is_active: bool = False
    grid_id: str = ""


class GridEngine:
    """Places and manages grid orders (Bybit / Binance Futures)."""

    def __init__(self):
        from exchange_factory import create_exchange
        self.exchange = create_exchange()
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

    def _biased_fraction(self, position: int, total: int, bias: float) -> float:
        """Map a 1..N position into 0..1 with density bias.

        Higher bias (>0.5) compresses levels closer to current price.
        Lower bias (<0.5) spreads levels farther toward the range edge.
        """
        if total <= 1:
            return 1.0
        x = position / total
        b = max(0.05, min(0.95, float(bias)))
        exponent = 2.2 - (b * 2.4)
        exponent = max(0.25, min(2.75, exponent))
        return max(0.0, min(1.0, x ** exponent))

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
        progressive_sizing_enabled: bool = False,
        progressive_min_factor: float = 0.35,
        progressive_max_factor: float = 2.0,
        progressive_curve_power: float = 1.5,
        spacing_mode: str = "balanced",
        buy_density_bias: float = 0.5,
        sell_density_bias: float = 0.5,
        entry_shape_template: str = "atr_box",
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

        levels = []

        # v3: Pre-compute exponential sizing factors
        import math
        total_range = upper - lower
        spacing_mode = str(spacing_mode or "balanced")
        buy_density_bias = max(0.0, min(1.0, float(buy_density_bias)))
        sell_density_bias = max(0.0, min(1.0, float(sell_density_bias)))
        nominal_step = total_range / max(num_grids, 1)
        min_gap = max(nominal_step * 0.2, 10 ** (-price_precision))

        lower_span = max(current_price - lower, 0.0)
        upper_span = max(upper - current_price, 0.0)
        total_levels = max(2, int(num_grids))

        if total_range <= 0:
            raise ValueError(f"Invalid grid range for {symbol}: lower={lower} upper={upper}")

        buy_levels_target = max(1, min(total_levels - 1, round(total_levels * lower_span / total_range)))
        sell_levels_target = max(1, total_levels - buy_levels_target)

        if spacing_mode == "buy_weighted":
            buy_share = max(buy_density_bias, 0.52)
            buy_levels_target = max(1, min(total_levels - 1, int(round(total_levels * buy_share))))
            sell_levels_target = max(1, total_levels - buy_levels_target)
        elif spacing_mode == "sell_weighted":
            sell_share = max(sell_density_bias, 0.52)
            sell_levels_target = max(1, min(total_levels - 1, int(round(total_levels * sell_share))))
            buy_levels_target = max(1, total_levels - sell_levels_target)

        ordered_prices: list[tuple[float, str]] = []

        if lower_span > 0:
            side_bias = buy_density_bias if spacing_mode != "sell_weighted" else 0.45
            for pos in range(1, buy_levels_target + 1):
                frac = self._biased_fraction(pos, buy_levels_target, side_bias)
                price = self._round_to_precision(current_price - (lower_span * frac), price_precision)
                ordered_prices.append((price, "Buy"))

        if upper_span > 0:
            side_bias = sell_density_bias if spacing_mode != "buy_weighted" else 0.45
            for pos in range(1, sell_levels_target + 1):
                frac = self._biased_fraction(pos, sell_levels_target, side_bias)
                price = self._round_to_precision(current_price + (upper_span * frac), price_precision)
                ordered_prices.append((price, "Sell"))

        ordered_prices.sort(key=lambda item: item[0])

        filtered_prices: list[tuple[float, str]] = []
        for price, side in ordered_prices:
            if abs(price - current_price) < min_gap:
                continue
            if filtered_prices and abs(price - filtered_prices[-1][0]) < (min_gap * 0.5):
                continue
            filtered_prices.append((price, side))

        for i, (price, side) in enumerate(filtered_prices):
            # v3: Exponential level sizing (near-center decay)
            if exp_sizing_gamma > 0:
                dist = abs(price - current_price) / max(total_range, 1e-8)
                dist = min(dist, 1.0)
                factor = math.exp(-exp_sizing_gamma * dist * 3)
                factor = max(0.3, min(1.0, factor))  # clamp 30%-100%
                level_qty = self._round_to_precision(qty_per_level * factor, qty_precision)
                level_qty = max(level_qty, min_qty)
                # Ensure notional meets minimum ($5 notional)
                if level_qty * price < 5.0:
                    level_qty = self._round_to_precision(5.0 / price, qty_precision)
            # v5: Progressive (martingale) sizing — grows away from center
            elif progressive_sizing_enabled:
                # Per-side distance: 0 at current price, 1 at the range edge
                if side == "Buy":
                    side_span = current_price - lower
                    dist = (current_price - price) / max(side_span, 1e-8)
                else:
                    side_span = upper - current_price
                    dist = (price - current_price) / max(side_span, 1e-8)
                dist = min(dist, 1.0)
                # factor DECLINES from max_factor (near/center) to min_factor (far/edges)
                # Passivbot-style: center=biggest fills (2.0x), edges=smallest (0.35x)
                factor = progressive_max_factor - (progressive_max_factor - progressive_min_factor) * (dist ** progressive_curve_power)
                factor = max(progressive_min_factor, min(progressive_max_factor, factor))
                level_qty = self._round_to_precision(qty_per_level * factor, qty_precision)
                level_qty = max(level_qty, min_qty)
                # Ensure notional meets minimum ($5 notional)
                if level_qty * price < 5.0:
                    level_qty = self._round_to_precision(5.0 / price, qty_precision)
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
            spacing_mode=spacing_mode,
            entry_shape_template=entry_shape_template,
            buy_density_bias=buy_density_bias,
            sell_density_bias=sell_density_bias,
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
        grid = self.calculate_grid_levels(
            symbol,
            upper,
            lower,
            num_grids,
            price,
            leverage,
            spacing_mode=getattr(coin_score, "entry_shape_spacing", "balanced"),
            buy_density_bias=getattr(coin_score, "entry_buy_density_bias", 0.5),
            sell_density_bias=getattr(coin_score, "entry_sell_density_bias", 0.5),
            entry_shape_template=getattr(coin_score, "entry_shape_template", "atr_box"),
        )

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

        # Place all limit orders — batch when exchange supports it
        orders_data = []
        for level in grid.grid_levels:
            orders_data.append({
                "symbol": symbol,
                "side": level.side.lower(),
                "amount": level.qty,
                "price": level.price,
                "type": "limit",
            })

        try:
            # Use batch endpoint when available (CCXT v4+ create_orders)
            orders = await self.exchange.create_orders(orders_data)
            for level, order in zip(grid.grid_levels, orders):
                level.order_id = order.get("id", "")
                level.status = "placed" if order.get("status") != "rejected" else "failed"
                if level.status == "placed":
                    logger.info(f"  ✅ {level.side} {level.qty} @ {level.price:.4f} → {order.get('id', '?')}")
                else:
                    logger.warning(f"  ❌ {level.side} {level.qty} @ {level.price:.4f} → rejected")
        except (AttributeError, NotImplementedError):
            # Fallback: sequential placement (older CCXT or unsupported exchange)
            logger.info("Batch create_orders not supported, falling back to sequential placement")
            for level in grid.grid_levels:
                try:
                    order = await self.exchange.create_limit_order(
                        symbol=symbol,
                        side=level.side.lower(),
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