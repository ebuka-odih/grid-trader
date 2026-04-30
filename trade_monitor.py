"""""
Trade Monitor — watches active grid positions via WebSocket,
tracks PnL in real-time, and triggers close when target PnL is hit.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional

from ws_manager import BybitWSManager
from grid_engine import GridState

from config import (
    TARGET_PNL_PCT_LOW,
    TARGET_PNL_PCT_HIGH,
    MAX_DRAWDOWN_PCT,
    BASE_ORDER_SIZE_USDT,
    DEFAULT_NUM_GRIDS,
    DRY_RUN,
)

logger = logging.getLogger("trade_monitor")


class TradeMonitor:
    """""Monitors active grid trades and triggers actions based on PnL."""""

    def __init__(self, ws_manager: BybitWSManager):
        self.ws = ws_manager
        self.grid: Optional[GridState] = None
        self._current_price: float = 0.0
        self._realized_pnl: float = 0.0
        self._unrealized_pnl: float = 0.0
        self._position_qty: float = 0.0
        self._entry_price: float = 0.0
        self._on_target_hit: Optional[Callable] = None
        self._on_drawdown_hit: Optional[Callable] = None
        self._on_fill: Optional[Callable] = None
        self._fills_count: int = 0

    # ── Callbacks Setup ────────────────────────────────────────

    def on_target_hit(self, callback: Callable):
        """""Register callback for when target PnL is reached."""""
        self._on_target_hit = callback

    def on_drawdown_hit(self, callback: Callable):
        """""Register callback for when max drawdown is hit."""""
        self._on_drawdown_hit = callback

    def on_fill(self, callback: Callable):
        """""Register callback for when an order fills."""""
        self._on_fill = callback

    # ── Start Monitoring a Grid ────────────────────────────────

    async def monitor_grid(self, grid: GridState):
        """""Start monitoring an active grid."""""
        self.grid = grid
        self._current_price = 0.0
        self._realized_pnl = 0.0
        self._unrealized_pnl = 0.0
        self._fills_count = 0

        # Extract base symbol for WS (e.g., "BTCUSDT" from "BTC/USDT:USDT")
        # Bybit WS uses format like "BTCUSDT"
        raw_symbol = grid.symbol.replace("/", "").replace(":USDT", "")
        ws_symbol = raw_symbol  # e.g. BTCUSDT

        # Subscribe to ticker for price tracking
        await self.ws.subscribe_ticker(ws_symbol, self._handle_ticker)

        # Subscribe to private position/execution updates only for live mode.
        # DRY_RUN must not require private WebSocket permissions/endpoints.
        if not DRY_RUN:
            await self.ws.subscribe_position(self._handle_position)
            await self.ws.subscribe_execution(self._handle_execution)

        logger.info(
            f"👁️ Monitoring grid: {grid.grid_id} | target PnL: {TARGET_PNL_PCT_LOW}-{TARGET_PNL_PCT_HIGH}% of allocated margin"
        )

    # ── WebSocket Handlers ─────────────────────────────────────

    async def _handle_ticker(self, data: dict):
        """""Process ticker updates to track current price."""""
        try:
            if "data" in data:
                self._current_price = float(data["data"].get("lastPrice", 0))
                # Update unrealized PnL
                self._update_pnl()
        except Exception as e:
            logger.error(f"Ticker handler error: {e}")

    async def _handle_position(self, data: dict):
        """""Process position updates."""""
        try:
            if "data" in data:
                for pos in data["data"]:
                    size = float(pos.get("size", 0))
                    entry = float(pos.get("entryPrice", 0))
                    unrealized = float(pos.get("unrealisedPnl", 0))
                    self._position_qty = size
                    self._entry_price = entry
                    self._unrealized_pnl = unrealized
                    if self.grid:
                        self.grid.position_qty = size
                        self.grid.entry_price = entry

                    logger.info(
                        f"📈 Position: size={size} entry={entry} uPnL=${unrealized:.4f}"
                    )
                    self._check_targets()
        except Exception as e:
            logger.error(f"Position handler error: {e}")

    async def _handle_execution(self, data: dict):
        """""Process execution (fill) updates — counts grid fills."""""
        try:
            if "data" in data:
                for execn in data["data"]:
                    exec_type = execn.get("execType", "")
                    if exec_type == "TradeFill":
                        self._fills_count += 1
                        pnl = float(execn.get("closedPnl", 0))
                        self._realized_pnl += pnl
                        side = execn.get("side", "")
                        price = float(execn.get("execPrice", 0))
                        qty = float(execn.get("execQty", 0))
                        logger.info(
                            f"💰 Fill #{self._fills_count}: {side} {qty} @ {price:.4f} | closedPnL=${pnl:.4f}"
                        )

                        if self._on_fill:
                            await self._on_fill(execn)

                        self._check_targets()
        except Exception as e:
            logger.error(f"Execution handler error: {e}")

    # ── PnL Tracking ───────────────────────────────────────────

    def _update_pnl(self):
        """""Recalculate unrealized PnL from current price and position."""""
        if (
            self._position_qty > 0
            and self._entry_price > 0
            and self._current_price > 0
            and self.grid
            and self.grid.position_qty > 0
        ):
            # Long position
            self._unrealized_pnl = (
                self._current_price - self._entry_price
            ) * self._position_qty

    def _total_pnl(self) -> float:
        """""Get total PnL (realized + unrealized)."""""
        return self._realized_pnl + self._unrealized_pnl

    def _allocated_margin_usdt(self) -> float:
        """Margin allocated to this grid across all grid levels."""
        if not self.grid:
            return BASE_ORDER_SIZE_USDT * DEFAULT_NUM_GRIDS  # fallback
        return self.grid.order_size_usdt * self.grid.num_grids

    def _target_pnl_low_usdt(self) -> float:
        """Minimum close target in USDT, based on % of allocated margin."""
        allocated = self._allocated_margin_usdt()
        return allocated * TARGET_PNL_PCT_LOW / 100

    def _target_pnl_high_usdt(self) -> float:
        """Maximum close target in USDT, based on % of allocated margin."""
        allocated = self._allocated_margin_usdt()
        return allocated * TARGET_PNL_PCT_HIGH / 100

    def _drawdown_limit_usdt(self) -> float:
        """Maximum drawdown limit in USDT, based on % of allocated margin."""
        allocated = self._allocated_margin_usdt()
        return allocated * MAX_DRAWDOWN_PCT / 100

    def _check_targets(self):
        """Check if PnL targets or drawdown limits are hit - NOW WITH SPREAD AWARENESS."""
        total = self._total_pnl()
        
        # Calculate spread from ticker (if available) - bid/ask from position data
        spread_pct = 0.0
        spread_width = 0.0
        
        # Get spread from position data if available
        if hasattr(self, '_position_data') and self._position_data:
            bid = self._position_data.get('bidPrice', 0)
            ask = self._position_data.get('askPrice', 0)
            if bid > 0 and ask > bid:
                spread_width = ask - bid
                mid = (bid + ask) / 2
                spread_pct = (spread_width / mid) * 100
        
        # Target hit check - REMOVED minimum fills requirement
        # Allow closing after ANY profitable fill
        if total >= self._target_pnl_low_usdt():
            spread_warning = f" (spread={spread_pct:.3f}%)" if spread_pct > 0.05 else ""
            logger.info(
                f"🎯 TARGET HIT! Total PnL = ${total:.4f} "
                f">= ${self._target_pnl_low_usdt():.4f} ({TARGET_PNL_PCT_LOW}% of margin)"
                f" | Fills: {self._fills_count}{spread_warning}"
            )
            if spread_pct > 0.05:
                logger.warning(f"⚠️ Wide spread detected ({spread_pct:.3f}%) - consider limit order close")
            if self._on_target_hit:
                asyncio.create_task(self._on_target_hit(total))

        # Drawdown check - also without minimum fills
        elif total < 0 and abs(total) > self._drawdown_limit_usdt():
            logger.warning(
                f"⚠️ DRAWDOWN LIMIT! Total PnL = ${total:.4f} "
                f"<= -${self._drawdown_limit_usdt():.4f} (-{MAX_DRAWDOWN_PCT}% of margin)"
                f" | Fills: {self._fills_count}"
            )
            if self._on_drawdown_hit:
                asyncio.create_task(self._on_drawdown_hit(total))

    # ── Status ────────────────────────────────────────────────

    def get_status(self) -> dict:
        """""Get current monitor status summary."""""
        return {
            "grid_id": self.grid.grid_id if self.grid else None,
            "symbol": self.grid.symbol if self.grid else None,
            "current_price": self._current_price,
            "position_qty": self._position_qty,
            "entry_price": self._entry_price,
            "realized_pnl": round(self._realized_pnl, 4),
            "unrealized_pnl": round(self._unrealized_pnl, 4),
            "total_pnl": round(self._total_pnl(), 4),
            "fills": self._fills_count,
            "target_pnl_low": round(self._target_pnl_low_usdt(), 4),
            "target_pnl_high": round(self._target_pnl_high_usdt(), 4),
            "target_pnl_pct_low": TARGET_PNL_PCT_LOW,
            "target_pnl_pct_high": TARGET_PNL_PCT_HIGH,
        }

    def stop(self):
        """""Stop monitoring."""""
        self.grid = None
        logger.info("👁️ Trade monitor stopped")