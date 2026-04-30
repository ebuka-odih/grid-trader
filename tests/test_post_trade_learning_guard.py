import asyncio
import time
import unittest
from types import SimpleNamespace

from multi_grid_manager import GridSlot, MultiGridManager
from trading_agent import PreTradeDecision


class FakeEngine:
    def get_status(self):
        return {
            "total_pnl": 0.05,
            "realized_pnl": 0.05,
            "unrealized_pnl": 0.0,
            "fills": 2,
        }


class FakeJournal:
    def record_cycle_close(self, **kwargs):
        self.closed = kwargs

    def record_learning(self, **kwargs):
        self.learning = kwargs


class FakeWalletTracker:
    def __init__(self):
        self.removed = []

    def get_wallet_state(self):
        return {"balance": 100.05, "exposure_pct": 0.0}

    def remove_position(self, symbol, realized_pnl=0.0):
        self.removed.append((symbol, realized_pnl))


class FakeAlerter:
    async def alert_grid_closed(self, *args, **kwargs):
        self.alerted = (args, kwargs)


class PostTradeLearningGuardTests(unittest.TestCase):
    def test_grid_close_skips_post_trade_learning_when_llm_agent_disabled(self):
        manager = MultiGridManager.__new__(MultiGridManager)
        manager.journal = FakeJournal()
        manager.wallet_tracker = FakeWalletTracker()
        manager.alerter = FakeAlerter()
        manager._total_trades = 0
        manager._total_pnl = 0.0
        manager._wins = 0
        manager._losses = 0
        manager._completed_trades = []
        manager.scanner = SimpleNamespace(learning=None)
        manager.max_grids = 10

        decision = PreTradeDecision(
            symbol="TEST/USDT:USDT",
            direction="neutral",
            confidence=0.8,
            upper=1.1,
            lower=0.9,
            num_grids=10,
            leverage=10,
            reasoning="test",
            market_regime="ranging",
            narrative="test",
        )
        state = SimpleNamespace(
            grid=SimpleNamespace(
                grid_id="grid_test_1",
                lower_price=0.9,
                upper_price=1.1,
                num_grids=10,
            )
        )
        slot = GridSlot(
            slot_id=1,
            symbol="TEST/USDT:USDT",
            engine=FakeEngine(),
            agent=None,
            decision=decision,
            state=state,
            started_at=time.time() - 60,
            adjusted_leverage=10,
            adjusted_order_size=1.0,
        )
        manager.slots = {1: slot}

        with self.assertNoLogs("multi_grid_manager", level="ERROR"):
            asyncio.run(manager._on_grid_closed(slot, "unit_test_close"))

        self.assertNotIn(1, manager.slots)
        self.assertEqual(manager._total_trades, 1)
        self.assertEqual(manager.wallet_tracker.removed, [("TEST/USDT:USDT", 0.05)])


if __name__ == "__main__":
    unittest.main()
