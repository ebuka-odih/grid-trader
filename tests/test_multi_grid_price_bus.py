import asyncio
import time
import unittest
from dataclasses import dataclass
from types import SimpleNamespace

from multi_grid_manager import MultiGridManager, GridSlot
from scanner_learning import ScannerLearning
from trading_agent import PreTradeDecision


class FakeEngine:
    def __init__(self):
        self.prices = []

    def on_price_update(self, price):
        self.prices.append(price)
        return "target_hit"

    def get_status(self):
        return {
            "current_price": self.prices[-1] if self.prices else 0.0,
            "total_pnl": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "fills": 0,
            "position_side": "",
            "position_qty": 0.0,
        }


class FakePriceBus:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.subscribed = []
        self.unsubscribed = []

    async def subscribe(self, symbol):
        self.subscribed.append(symbol)
        return self.queue

    async def unsubscribe(self, symbol, queue):
        self.unsubscribed.append(symbol)


@dataclass
class FakeState:
    is_active: bool = True


class MultiGridPriceBusTests(unittest.TestCase):
    def test_monitor_grid_consumes_shared_price_bus_tick(self):
        async def scenario():
            manager = MultiGridManager.__new__(MultiGridManager)
            manager._running = True
            manager.price_bus = FakePriceBus()
            manager.wallet_tracker = None
            manager._closed = None

            async def on_closed(slot, reason):
                manager._closed = (slot.symbol, reason)
                manager._running = False

            manager._on_grid_closed = on_closed

            decision = PreTradeDecision(
                symbol="AAVE/USDT:USDT",
                direction="long",
                confidence=0.8,
                upper=101.0,
                lower=99.0,
                num_grids=5,
                leverage=5,
                reasoning="test",
                market_regime="ranging",
                narrative="test",
            )
            engine = FakeEngine()
            slot = GridSlot(
                slot_id=1,
                symbol="AAVE/USDT:USDT",
                engine=engine,
                agent=None,
                decision=decision,
                state=FakeState(),
                started_at=time.time(),
                token_profile={"grid_timeout_minutes": 1},
                adjusted_leverage=5,
                adjusted_order_size=1.0,
            )

            task = asyncio.create_task(manager._monitor_grid(slot))
            await asyncio.sleep(0)
            await manager.price_bus.queue.put(94.25)
            await asyncio.wait_for(task, timeout=1.0)

            self.assertEqual(engine.prices, [94.25])
            self.assertEqual(manager.price_bus.subscribed, ["AAVE/USDT:USDT"])
            self.assertEqual(manager.price_bus.unsubscribed, ["AAVE/USDT:USDT"])
            self.assertEqual(manager._closed, ("AAVE/USDT:USDT", "target_hit"))

        asyncio.run(scenario())


    def test_closed_grid_records_scanner_learning_outcome(self):
        async def scenario():
            manager = MultiGridManager.__new__(MultiGridManager)
            manager._journal_close = None
            manager.journal = SimpleNamespace(
                record_cycle_close=lambda **kwargs: setattr(manager, "_journal_close", kwargs),
                record_learning=lambda **kwargs: None,
            )
            manager.wallet_tracker = SimpleNamespace(
                remove_position=lambda *args, **kwargs: None,
                get_wallet_state=lambda: {"balance": 100.0, "exposure_pct": 0.0},
            )
            manager.alerter = SimpleNamespace(alert_grid_closed=lambda *args, **kwargs: asyncio.sleep(0))
            manager.scanner = SimpleNamespace(learning=ScannerLearning(now_fn=lambda: 1_000.0, state_path=None))
            manager.slots = {1: "occupied"}
            manager.max_grids = 20
            manager._total_trades = 0
            manager._total_pnl = 0.0
            manager._wins = 0
            manager._losses = 0
            manager._completed_trades = []

            decision = PreTradeDecision(
                symbol="FAST/USDT:USDT",
                direction="long",
                confidence=0.8,
                upper=101.0,
                lower=99.0,
                num_grids=5,
                leverage=50,
                reasoning="test",
                market_regime="ranging",
                narrative="test",
            )
            engine = FakeEngine()
            engine.get_status = lambda: {
                "total_pnl": -0.25,
                "realized_pnl": -0.25,
                "unrealized_pnl": 0.0,
                "fills": 2,
            }
            state = SimpleNamespace(
                grid=SimpleNamespace(grid_id="grid_FAST_1", lower_price=99.0, upper_price=101.0, num_grids=5),
                is_active=False,
            )
            agent = SimpleNamespace(analyze_post_trade=lambda result: None)
            slot = GridSlot(
                slot_id=1,
                symbol="FAST/USDT:USDT",
                engine=engine,
                agent=agent,
                decision=decision,
                state=state,
                started_at=time.time() - 30,
                token_profile={"grid_timeout_minutes": 1},
                adjusted_leverage=50,
                adjusted_order_size=5.0,
            )

            await manager._on_grid_closed(slot, "timeout")
            learned = manager.scanner.learning.get_state("FAST/USDT:USDT")
            self.assertEqual(learned.trades, 1)
            self.assertEqual(learned.losses, 1)
            self.assertEqual(learned.recent_failures, 1)
            self.assertEqual(manager._journal_close["direction"], "long")
            self.assertEqual(manager._journal_close["adjusted_leverage"], 50)
            self.assertEqual(manager._journal_close["adjusted_order_size"], 5.0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
