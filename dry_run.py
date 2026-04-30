"""
Dry-Run Orchestrator — runs the full agentic loop in simulation mode.
Uses REAL market data (WebSocket) but SIMULATED order fills.
Perfect for testing the system before risking real money.
"""

import asyncio
import json
import logging
import time
from datetime import datetime

import websockets

from config import (
    BYBIT_WS_PUBLIC, SCAN_INTERVAL_SECONDS,
    TARGET_PNL_LOW, TARGET_PNL_HIGH,
    BYBIT_API_KEY,
)
from coin_scanner import CoinScanner
from dry_run_engine import DryRunEngine, DryRunState
from improvement_loop import ImprovementLoop
from telegram_alerter import TelegramAlerter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("dry_run.log"),
    ],
)
logger = logging.getLogger("dry_run_orchestrator")


class DryRunOrchestrator:
    """Runs the full agentic loop with simulated trading."""

    def __init__(self):
        self.scanner = CoinScanner()
        self.engine = DryRunEngine()
        self.journal = ImprovementLoop(db_path="sqlite:///dry_run_trades.db")
        self.alerter = TelegramAlerter()

        self._cycle_count = 0
        self._running = False
        self._ws = None

    async def start(self):
        """Start the dry-run loop."""
        logger.info("=" * 60)
        logger.info("🧪 DRY-RUN AGETIC GRID TRADER")
        logger.info(f"   Target PnL: ${TARGET_PNL_LOW}-${TARGET_PNL_HIGH}")
        logger.info(f"   No real orders will be placed!")
        logger.info("=" * 60)

        if not BYBIT_API_KEY or BYBIT_API_KEY == "your_api_key_here":
            logger.error("❌ API keys not set!")
            return

        self._running = True

        try:
            while self._running:
                await self._run_cycle()
                logger.info(f"⏳ Waiting {SCAN_INTERVAL_SECONDS}s before next scan...")
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("🛑 Stopped by user")
        except Exception as e:
            logger.error(f"❌ Error: {e}", exc_info=True)

    async def _run_cycle(self):
        """One full scan → simulate → monitor cycle."""
        self._cycle_count += 1
        cycle_start = time.time()
        logger.info(f"\n{'='*60}")
        logger.info(f"🧪 DRY-RUN CYCLE #{self._cycle_count}")
        logger.info(f"{'='*60}")

        # Step 1: Improvement suggestions
        suggestions = self.journal.suggest_params()
        for r in suggestions.get("reason", []):
            logger.info(f"🧠 {r}")

        # Step 2: Scan for best coin
        logger.info("🔍 Scanning for best coin...")
        best_coin = await self.scanner.get_best_coin()
        if not best_coin:
            logger.warning("⚠️ No suitable coins found. Retrying next cycle.")
            return

        logger.info(f"🏆 Best coin: {best_coin.symbol} (score={best_coin.grid_score:.3f})")
        logger.info(f"   Price: ${best_coin.price:.4f}")
        logger.info(f"   Range: {best_coin.range_pct:.2f}% | ATR: {best_coin.atr_pct:.2f}%")
        logger.info(f"   Suggested: {best_coin.suggested_grids} grids, {best_coin.suggested_leverage}x leverage")
        logger.info(f"   Grid: ${best_coin.suggested_lower:.4f} — ${best_coin.suggested_upper:.4f}")

        # Apply improvement suggestions
        if self.journal.get_stats()["total_cycles"] >= 5:
            best_coin.suggested_leverage = suggestions.get("leverage", best_coin.suggested_leverage)
            best_coin.suggested_grids = suggestions.get("num_grids", best_coin.suggested_grids)

        # Step 3: Deploy simulated grid
        logger.info("📐 Deploying simulated grid...")
        state = self.engine.deploy_grid(best_coin)

        # Record cycle start
        self.journal.record_cycle_start(
            grid_id=state.grid.grid_id, symbol=state.grid.symbol,
            upper=state.grid.upper_price, lower=state.grid.lower_price,
            num_grids=state.grid.num_grids, leverage=state.grid.leverage,
        )

        # Print grid levels
        logger.info("📊 Grid levels:")
        for lvl in state.grid.grid_levels:
            marker = "🟢" if lvl.side == "Buy" else "🔴"
            logger.info(f"   {marker} [{lvl.index:2d}] {lvl.side:4s} {lvl.qty:.6f} @ ${lvl.price:.4f}")

        # Step 4: Monitor via WebSocket (real price data, simulated fills)
        logger.info("👁️ Monitoring with real-time prices (simulated fills)...")

        # Extract WS symbol format
        ws_symbol = state.grid.symbol.replace("/", "").replace(":USDT", "")

        close_reason = "timeout"
        try:
            async with websockets.connect(
                BYBIT_WS_PUBLIC,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                # Subscribe to ticker
                topic = f"tickers.{ws_symbol}"
                await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                logger.info(f"📡 Subscribed to {topic}")

                # Listen for price updates
                timeout = SCAN_INTERVAL_SECONDS * 6  # 30 min default
                start = time.time()
                status_interval = 30  # print status every 30s
                last_status = 0

                while self._running and state.is_active:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(msg)

                        if "data" in data and "lastPrice" in data.get("data", {}):
                            price = float(data["data"]["lastPrice"])

                            # Feed price to dry-run engine
                            event = self.engine.on_price_update(price)
                            if event == "target_hit":
                                close_reason = "target_hit"
                                break
                            elif event == "drawdown":
                                close_reason = "drawdown"
                                break

                    except asyncio.TimeoutError:
                        pass

                    # Periodic status
                    now = time.time()
                    if now - last_status >= status_interval:
                        status = self.engine.get_status()
                        logger.info(
                            f"📊 Status: price=${status['current_price']:.4f} | "
                            f"pnl=${status['total_pnl']:.4f} "
                            f"(real=${status['realized_pnl']:.4f} + unreal=${status['unrealized_pnl']:.4f}) | "
                            f"fills={status['fills']} | pos={status.get('position_side','')} {status.get('position_qty',0):.6f}"
                        )
                        last_status = now

                    # Timeout
                    if time.time() - start > timeout:
                        logger.warning("⏰ Grid monitoring timeout")
                        close_reason = "timeout"
                        break

        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            close_reason = "ws_error"

        # Step 5: Close and log
        status = self.engine.get_status()
        total_pnl = status["total_pnl"]
        realized = status["realized_pnl"]
        unrealized = status["unrealized_pnl"]
        fills = status["fills"]
        duration = time.time() - cycle_start

        self.journal.record_cycle_close(
            grid_id=state.grid.grid_id, total_pnl=total_pnl,
            realized_pnl=realized, unrealized_pnl=unrealized,
            fills=fills, duration=duration, close_reason=close_reason,
        )

        emoji = "✅" if total_pnl > 0 else "❌"
        logger.info(f"\n{emoji} CYCLE #{self._cycle_count} COMPLETE")
        logger.info(f"   Symbol: {state.grid.symbol}")
        logger.info(f"   PnL: ${total_pnl:.4f} (realized=${realized:.4f}, unrealized=${unrealized:.4f})")
        logger.info(f"   Fills: {fills} | Duration: {duration/60:.1f}min")
        logger.info(f"   Reason: {close_reason}")

        # Improvement suggestions
        suggestions = self.journal.suggest_params()
        if suggestions["reason"]:
            logger.info("🧠 Next cycle suggestions:")
            for r in suggestions["reason"]:
                logger.info(f"   → {r}")

    async def close(self):
        self._running = False
        await self.scanner.close()
        logger.info("🧪 Dry-run orchestrator stopped")


async def main():
    bot = DryRunOrchestrator()
    try:
        await bot.start()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
