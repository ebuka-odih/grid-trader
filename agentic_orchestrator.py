"""
Agentic Orchestrator — the LLM-driven trading loop.

Decision flow:
  1. Scanner → top-5 coins
  2. AGENT decides: which coin, direction, grid params (PRE-TRADE)
  3. Engine deploys grid based on agent's decision
  4. AGENT checks every 2-3 min: adjust? shift? close? (MID-TRADE)
  5. AGENT evaluates close triggers: close now? (CLOSE)
  6. AGENT analyzes results: what to learn? (POST-TRADE)
  7. Repeat with improved strategy

Uses REAL market data + SIMULATED fills (dry-run mode).
No real orders placed.
"""

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime

import websockets

from config import (
    BYBIT_WS_PUBLIC, SCAN_INTERVAL_SECONDS,
    TARGET_PNL_LOW, TARGET_PNL_HIGH, MAX_DRAWDOWN_PCT,
    BASE_ORDER_SIZE_USDT, BYBIT_API_KEY,
)
from coin_scanner import CoinScanner, CoinScore
from dry_run_engine import DryRunEngine
from trading_agent import PreTradeDecision, MidTradeDecision
from rule_agent import RuleBasedAgent
from improvement_loop import ImprovementLoop
from telegram_alerter import TelegramAlerter
from grid_engine import GridEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("agentic_run.log"),
    ],
)
logger = logging.getLogger("agentic_orchestrator")


def coin_score_to_dict(coin: CoinScore) -> dict:
    """Convert CoinScore to a dict the LLM can reason about."""
    return {
        "symbol": coin.symbol,
        "price": coin.price,
        "high_24h": coin.high_24h,
        "low_24h": coin.low_24h,
        "volume_24h_usdt": round(coin.volume_24h_usdt, 0),
        "atr_pct": coin.atr_pct,
        "range_pct": coin.range_pct,
        "mean_reversion_score": coin.mean_reversion_score,
        "grid_score": coin.grid_score,
        "suggested_upper": coin.suggested_upper,
        "suggested_lower": coin.suggested_lower,
        "suggested_grids": coin.suggested_grids,
        "suggested_leverage": coin.suggested_leverage,
    }


class AgenticOrchestrator:
    """LLM-driven agentic grid trading loop."""

    def __init__(self):
        self.scanner = CoinScanner()
        self.engine = DryRunEngine()
        self.agent = RuleBasedAgent()
        self.journal = ImprovementLoop(db_path="sqlite:///agentic_trades.db")
        self.alerter = TelegramAlerter()
        self.grid_calc = GridEngine()

        self._cycle_count = 0
        self._running = False
        self._mid_trade_interval = 120  # seconds between agent mid-trade checks

    async def start(self):
        """Start the agentic trading loop."""
        logger.info("=" * 60)
        logger.info("🤖 AGETIC GRID TRADER (Dry-Run)")
        logger.info(f"   Target PnL: ${TARGET_PNL_LOW}-${TARGET_PNL_HIGH}")
        logger.info(f"   Max drawdown: {MAX_DRAWDOWN_PCT}%")
        logger.info(f"   Agent model: {self.agent.model}")
        logger.info(f"   Agent decides: coin, direction, grid params, adjustments, exits")
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
            logger.error(f"❌ Fatal error: {e}", exc_info=True)
        finally:
            await self.close()

    async def close(self):
        self._running = False
        await self.scanner.close()
        summary = self.agent.get_session_summary()
        logger.info(f"🤖 Agent session summary: {summary}")
        logger.info("🤖 Agentic orchestrator stopped")

    # ── Main Cycle ───────────────────────────────────────────────

    async def _run_cycle(self):
        """One full agentic trading cycle."""
        self._cycle_count += 1
        cycle_start = time.time()
        logger.info(f"\n{'='*60}")
        logger.info(f"🤖 AGETIC CYCLE #{self._cycle_count}")
        logger.info(f"{'='*60}")

        # ── Step 1: Scan ──────────────────────────────────────────
        logger.info("🔍 Scanning market...")
        scores = await self.scanner.scan()
        if not scores:
            logger.warning("⚠️ No suitable coins found. Retrying next cycle.")
            return

        # Prepare top-5 for the agent
        top_5 = [coin_score_to_dict(s) for s in scores[:5]]
        logger.info(f"📊 Sending top-{len(top_5)} to agent for decision...")

        # ── Step 2: AGENT PRE-TRADE DECISION ──────────────────────
        agent_decision = self.agent.decide_pre_trade(top_5)

        if not agent_decision:
            # Fallback to algorithmic top-1
            logger.warning("🤖 Agent failed, falling back to algorithmic selection")
            best = scores[0]
            agent_decision = PreTradeDecision(
                symbol=best.symbol,
                direction="neutral",
                confidence=0.3,
                upper=best.suggested_upper,
                lower=best.suggested_lower,
                num_grids=best.suggested_grids,
                leverage=best.suggested_leverage,
                reasoning="Algorithmic fallback (agent unavailable)",
                market_regime="ranging",
                narrative="Fallback selection",
            )

        # Find the matching CoinScore (agent may have picked any of top-5)
        coin_score = next(
            (s for s in scores if s.symbol == agent_decision.symbol),
            scores[0]  # fallback to top-1 if agent picked something weird
        )

        # Override grid params with agent's decision
        coin_score.suggested_upper = agent_decision.upper
        coin_score.suggested_lower = agent_decision.lower
        coin_score.suggested_grids = agent_decision.num_grids
        coin_score.suggested_leverage = agent_decision.leverage

        logger.info(f"")
        logger.info(f"🤖 AGENT DECISION:")
        logger.info(f"   Coin: {agent_decision.symbol}")
        logger.info(f"   Direction: {agent_decision.direction.upper()}")
        logger.info(f"   Market: {agent_decision.market_regime}")
        logger.info(f"   Confidence: {agent_decision.confidence:.0%}")
        logger.info(f"   Grid: ${agent_decision.lower:.4f} — ${agent_decision.upper:.4f}")
        logger.info(f"   Levels: {agent_decision.num_grids} | Leverage: {agent_decision.leverage}x")
        logger.info(f"   Reason: {agent_decision.reasoning}")
        logger.info(f"")

        # ── Step 3: Deploy symmetric edge grid ───────────────────
        state = self._deploy_edge_grid(coin_score, agent_decision.direction)

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

        # ── Step 4: Monitor with AGENT mid-trade checks ───────────
        ws_symbol = state.grid.symbol.replace("/", "").replace(":USDT", "")
        close_reason = "timeout"
        last_agent_check = time.time()

        try:
            async with websockets.connect(
                BYBIT_WS_PUBLIC,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                topic = f"tickers.{ws_symbol}"
                await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                logger.info(f"📡 Subscribed to {topic}")

                timeout = SCAN_INTERVAL_SECONDS * 6
                start = time.time()
                status_interval = 30
                last_status = 0

                while self._running and state.is_active:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(msg)

                        if "data" in data and "lastPrice" in data.get("data", {}):
                            price = float(data["data"]["lastPrice"])
                            event = self.engine.on_price_update(price)
                            if event == "target_hit":
                                close_reason = "target_hit"
                                break
                            elif event == "drawdown":
                                close_reason = "drawdown"
                                break

                    except asyncio.TimeoutError:
                        pass

                    now = time.time()

                    # Periodic status
                    if now - last_status >= status_interval:
                        status = self.engine.get_status()
                        logger.info(
                            f"📊 price=${status['current_price']:.4f} | "
                            f"pnl=${status['total_pnl']:.4f} "
                            f"(real=${status['realized_pnl']:.4f} + unreal=${status['unrealized_pnl']:.4f}) | "
                            f"fills={status['fills']} | pos={status.get('position_side','')} {status.get('position_qty',0):.6f}"
                        )
                        last_status = now

                    # ── AGENT MID-TRADE CHECK ──────────────────────
                    if now - last_agent_check >= self._mid_trade_interval:
                        logger.info("🤖 Asking agent for mid-trade adjustment...")
                        mid_decision = self.agent.decide_mid_trade(self.engine.get_status())

                        if mid_decision.action == "close":
                            logger.info(
                                "🤖 Agent requested close but was ignored; engine close rules remain authoritative. "
                                f"{mid_decision.reasoning}"
                            )
                        elif mid_decision.action != "hold":
                            logger.info(f"🤖 Agent suggests: {mid_decision.action} — {mid_decision.reasoning}")
                            # In dry-run, we log the suggestion but don't reconfigure
                            # (In live mode, this would actually shift/adjust the grid)
                            if mid_decision.action == "shift_up":
                                logger.info(f"   ↗️ Would shift grid up {mid_decision.shift_pct:.1f}%")
                            elif mid_decision.action == "shift_down":
                                logger.info(f"   ↘️ Would shift grid down {mid_decision.shift_pct:.1f}%")
                            elif mid_decision.action == "tighten":
                                logger.info(f"   📐 Would tighten grid range")
                            elif mid_decision.action == "widen":
                                logger.info(f"   📐 Would widen grid range")
                            elif mid_decision.action == "hedge":
                                logger.info(f"   🛡️ Would open hedge position")

                        last_agent_check = now

                    # Timeout
                    if now - start > timeout:
                        logger.warning("⏰ Grid monitoring timeout")
                        close_reason = "timeout"
                        break

        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            close_reason = "ws_error"

        # ── Step 5: AGENT CLOSE DECISION ──────────────────────────
        if close_reason in ("target_hit", "drawdown"):
            close_decision = self.agent.decide_close(self.engine.get_status(), close_reason)
            if not close_decision.should_close and close_reason != "drawdown":
                logger.info(f"🤖 Agent says DON'T CLOSE yet: {close_decision.reasoning}")
                # Could continue monitoring, but for safety we close on triggers
            else:
                logger.info(f"🤖 Agent confirms CLOSE: {close_decision.reasoning}")

        # ── Step 6: Record results ────────────────────────────────
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
        logger.info(f"   Direction: {agent_decision.direction}")
        logger.info(f"   PnL: ${total_pnl:.4f} (realized=${realized:.4f}, unrealized=${unrealized:.4f})")
        logger.info(f"   Fills: {fills} | Duration: {duration/60:.1f}min")
        logger.info(f"   Close reason: {close_reason}")

        # ── Step 7: AGENT POST-TRADE LEARNING ─────────────────────
        cycle_result = {
            "cycle": self._cycle_count,
            "symbol": state.grid.symbol,
            "direction": agent_decision.direction,
            "market_regime": agent_decision.market_regime,
            "confidence": agent_decision.confidence,
            "grid_range": f"${state.grid.lower_price:.4f}-{state.grid.upper_price:.4f}",
            "leverage": state.grid.leverage,
            "num_grids": state.grid.num_grids,
            "total_pnl": total_pnl,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "fills": fills,
            "duration_min": round(duration / 60, 1),
            "close_reason": close_reason,
        }

        learning = self.agent.analyze_post_trade(cycle_result)
        if learning:
            logger.info(f"🧠 LEARNING: {learning.suggestion}")

    # ── Symmetric edge grid deployment ───────────────────────────

    def _deploy_edge_grid(self, coin_score: CoinScore, direction: str) -> 'DryRunState':
        """
        Deploy a symmetric two-sided edge grid.

        Direction remains metadata for agent analysis/logging only; the
        executable ladder itself always keeps buys below price and sells above
        price so inventory is managed by edge participation rather than by
        one-sided side reassignment.
        """
        from dry_run_engine import DryRunState
        import time as _time

        grid = self.grid_calc.calculate_grid_levels(
            symbol=coin_score.symbol,
            upper=coin_score.suggested_upper,
            lower=coin_score.suggested_lower,
            num_grids=coin_score.suggested_grids,
            current_price=coin_score.price,
            leverage=coin_score.suggested_leverage,
        )

        buy_levels = sum(1 for l in grid.grid_levels if l.side == "Buy")
        sell_levels = sum(1 for l in grid.grid_levels if l.side == "Sell")
        logger.info(
            f"↔️ EDGE grid deployed with symmetric ladder: {buy_levels} buys below / "
            f"{sell_levels} sells above | agent_direction={direction}"
        )

        # Deploy to dry-run engine
        state = DryRunState(
            grid=grid,
            started_at=_time.time(),
            current_price=coin_score.price,
        )
        for level in grid.grid_levels:
            level.status = "placed"

        self.engine.state = state
        logger.info(f"🧪 DRY-RUN Grid deployed: {grid.symbol} | "
                    f"{grid.lower_price:.4f}-{grid.upper_price:.4f} | "
                    f"{len(grid.grid_levels)} levels | dir={direction} | lev={grid.leverage}x")
        return state


async def main():
    bot = AgenticOrchestrator()
    try:
        await bot.start()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
