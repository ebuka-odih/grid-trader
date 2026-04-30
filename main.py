"""
Main Orchestrator — the agentic brain that ties everything together.

Autonomous Loop:
  1. SCAN → Coin Scanner finds the best coin
  2. DEPLOY → Grid Engine places grid orders
  3. MONITOR → Trade Monitor watches PnL via WebSocket
  4. DECIDE → When target PnL hit → close grid → log results
  5. IMPROVE → Improvement Loop analyzes, suggests better params
  6. REPEAT → Start next cycle with improved parameters

The bot runs continuously, cycling through these steps.
"""

import asyncio
import logging
import time
from datetime import datetime

from config import (
    SCAN_INTERVAL_SECONDS,
    TARGET_PNL_PCT_LOW,
    TARGET_PNL_PCT_HIGH,
    MAX_DRAWDOWN_PCT,
    DEFAULT_LEVERAGE,
    DEFAULT_NUM_GRIDS,
    BYBIT_API_KEY,
    BYBIT_API_SECRET,
    DRY_RUN,
)
from ws_manager import BybitWSManager
from coin_scanner import CoinScanner, CoinScore
from grid_engine import GridEngine, GridState
from trade_monitor import TradeMonitor
from improvement_loop import ImprovementLoop
from telegram_alerter import TelegramAlerter

# ── Logging Setup ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("grid_trader.log"),
    ],
)
logger = logging.getLogger("orchestrator")


class GridTraderOrchestrator:
    """
    The main agentic orchestrator. Runs the full trading loop.
    """

    def __init__(self):
        self.ws_manager = BybitWSManager()
        self.scanner = CoinScanner()
        self.engine = GridEngine()
        self.monitor = TradeMonitor(self.ws_manager)
        self.journal = ImprovementLoop(db_path="sqlite:///trades.db")
        self.alerter = TelegramAlerter()

        self._cycle_count = 0
        self._running = False
        self._cycle_start_time = 0.0

        # Register monitor callbacks
        self.monitor.on_target_hit(self._on_target_hit)
        self.monitor.on_drawdown_hit(self._on_drawdown_hit)
        self.monitor.on_fill(self._on_fill)

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self):
        """Start the full agentic trading loop."""
        logger.info("=" * 60)
        logger.info("�0 AGETIC GRID TRADER STARTING")
        logger.info(f"   Mode: {'TESTNET' if BYBIT_API_KEY and True else 'MAINNET'}")
        logger.info(f"   Target PnL: {TARGET_PNL_PCT_LOW}-{TARGET_PNL_PCT_HIGH}% of allocated margin")
        logger.info(f"   Max Drawdown: {MAX_DRAWDOWN_PCT}% of allocated margin")
        logger.info(f"   Scan Interval: {SCAN_INTERVAL_SECONDS}s")
        logger.info("=" * 60)

        # Validate API keys
        if not BYBIT_API_KEY or BYBIT_API_KEY == "your_api_key_here":
            logger.error("❌ API keys not set! Edit .env file with your Bybit credentials.")
            logger.error("   Get testnet keys: https://testnet.bybit.com")
            return

        self._running = True

        # Start WebSocket connections.
        # In DRY_RUN, use public market data only. Private WS is unnecessary and can
        # fail on restricted keys; live mode still uses private fills/positions.
        ws_task = asyncio.create_task(
            self.ws_manager.start(need_private=not DRY_RUN)
        )

        # Give WS time to connect
        await asyncio.sleep(2)

        # Run the main loop
        try:
            while self._running:
                await self._run_cycle()
                # Wait between cycles
                logger.info(f"⏳ Waiting {SCAN_INTERVAL_SECONDS}s before next scan...")
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("🛑 Keyboard interrupt received")
        except Exception as e:
            logger.error(f"❌ Orchestrator error: {e}", exc_info=True)
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Gracefully shut down everything."""
        self._running = False
        logger.info("🛑 Shutting down...")

        # Close any active grid
        if self.engine.active_grid and self.engine.active_grid.is_active:
            await self.engine.shutdown_grid()

        # Close connections
        await self.scanner.close()
        await self.engine.close()
        await self.ws_manager.stop()
        self.monitor.stop()

        logger.info("✅ Shutdown complete")

    # ── Main Cycle ─────────────────────────────────────────────

    async def _run_cycle(self):
        """Execute one full scan → deploy → monitor cycle."""
        self._cycle_count += 1
        self._cycle_start_time = time.time()
        logger.info(f"\n{'='*60}")
        logger.info(f"🔄 CYCLE #{self._cycle_count}")
        logger.info(f"{'='*60}")

        # Step 1: Get improvement suggestions from past data
        suggestions = self.journal.suggest_params()
        if suggestions["reason"]:
            for reason in suggestions["reason"]:
                logger.info(f"🧠 {reason}")

        # Step 2: Scan for best coin
        logger.info("🔍 Step 1: Scanning for best coin...")
        best_coin = await self.scanner.get_best_coin()

        if not best_coin:
            logger.warning("⚠️ No suitable coins found. Will retry next cycle.")
            return

        logger.info(f"🏆 Best coin: {best_coin.symbol} (score={best_coin.grid_score:.3f})")

        # Apply improvement suggestions
        if suggestions.get("preferred_symbol"):
            # If we have a preferred symbol from past performance, check if it's in top results
            pass  # For now, use the scanner's best pick

        # Override scanner suggestions with improvement loop if we have data
        if self.journal.get_stats()["total_cycles"] >= 5:
            best_coin.suggested_leverage = suggestions.get("leverage", best_coin.suggested_leverage)
            best_coin.suggested_grids = suggestions.get("num_grids", best_coin.suggested_grids)

        # Step 3: Deploy grid
        logger.info("📐 Step 2: Deploying grid...")
        grid = await self.engine.quick_deploy(best_coin)

        # Record cycle start
        self.journal.record_cycle_start(
            grid_id=grid.grid_id,
            symbol=grid.symbol,
            upper=grid.upper_price,
            lower=grid.lower_price,
            num_grids=grid.num_grids,
            leverage=grid.leverage,
        )

        # Send alert
        await self.alerter.alert_grid_opened(
            symbol=grid.symbol,
            upper=grid.upper_price,
            lower=grid.lower_price,
            grids=grid.num_grids,
            leverage=grid.leverage,
            score=best_coin.grid_score,
        )

        # Step 4: Monitor until target or timeout
        logger.info("👁️ Step 3: Monitoring trade...")
        await self.monitor.monitor_grid(grid)

        # Wait for the monitor to trigger a close (via callbacks)
        # or timeout after SCAN_INTERVAL_SECONDS * 6 (30 min default)
        timeout = SCAN_INTERVAL_SECONDS * 6
        start = time.time()
        while self._running and grid.is_active:
            await asyncio.sleep(5)
            # Print status every 30 seconds
            if int(time.time()) % 30 == 0:
                status = self.monitor.get_status()
                logger.info(
                    f"📊 Status: price={status['current_price']:.4f} "
                    f"pnl=${status['total_pnl']:.4f} "
                    f"fills={status['fills']}"
                )
            # Timeout check
            if time.time() - start > timeout:
                logger.warning("⏰ Grid timeout — closing")
                await self._close_grid("timeout")
                break

    # ── Callbacks ──────────────────────────────────────────────

    async def _on_target_hit(self, total_pnl: float):
        """Called when target PnL is reached."""
        logger.info(f"🎯 TARGET HIT! PnL = ${total_pnl:.4f}")
        await self._close_grid("target_hit")

    async def _on_drawdown_hit(self, total_pnl: float):
        """Called when max drawdown is hit."""
        logger.warning(f"⚠️ DRAWDOWN HIT! PnL = ${total_pnl:.4f}")
        await self._close_grid("drawdown")

    async def _on_fill(self, execution: dict):
        """Called when an order fills."""
        # Record to journal
        if self.engine.active_grid:
            self.journal.record_fill(
                grid_id=self.engine.active_grid.grid_id,
                symbol=self.engine.active_grid.symbol,
                side=execution.get("side", ""),
                price=float(execution.get("execPrice", 0)),
                qty=float(execution.get("execQty", 0)),
                realized_pnl=float(execution.get("closedPnl", 0)),
                order_id=execution.get("orderLinkId", ""),
            )

    # ── Close Grid ─────────────────────────────────────────────

    async def _close_grid(self, close_reason: str):
        """Close the active grid and log results."""
        grid = self.engine.active_grid
        if not grid or not grid.is_active:
            return

        status = self.monitor.get_status()
        total_pnl = status["total_pnl"]
        realized = status["realized_pnl"]
        unrealized = status["unrealized_pnl"]
        fills = status["fills"]
        duration = time.time() - self._cycle_start_time

        # Close all orders and position
        await self.engine.shutdown_grid()

        # Record cycle close
        self.journal.record_cycle_close(
            grid_id=grid.grid_id,
            total_pnl=total_pnl,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            fills=fills,
            duration=duration,
            close_reason=reason,
        )

        # Alert
        await self.alerter.alert_grid_closed(grid.symbol, total_pnl, reason)

        # Stop monitoring
        self.monitor.stop()

        # Print improvement suggestions for next cycle
        suggestions = self.journal.suggest_params()
        if suggestions["reason"]:
            logger.info("🧠 Improvement suggestions for next cycle:")
            for r in suggestions["reason"]:
                logger.info(f"   → {r}")

        logger.info(
            f"📋 Cycle #{self._cycle_count} complete: pnl=${total_pnl:.4f} | reason={reason} | duration={duration/60:.1f}min"
        )


# ── Entry Point ────────────────────────────────────────────────

async def main():
    trader = GridTraderOrchestrator()
    await trader.start()


if __name__ == "__main__":
    asyncio.run(main())