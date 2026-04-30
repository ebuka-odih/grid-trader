import asyncio
import unittest

from grid_engine import GridState
from trade_monitor import TradeMonitor


class FakeWS:
    def __init__(self):
        self.ticker_calls = 0
        self.position_calls = 0
        self.execution_calls = 0

    async def subscribe_ticker(self, symbol, callback):
        self.ticker_calls += 1

    async def subscribe_position(self, callback):
        self.position_calls += 1

    async def subscribe_execution(self, callback):
        self.execution_calls += 1


class TradeMonitorDryRunTests(unittest.TestCase):
    def test_monitor_grid_in_dry_run_subscribes_only_to_public_ticker(self):
        ws = FakeWS()
        monitor = TradeMonitor(ws)
        grid = GridState(
            symbol="TEST/USDT:USDT",
            upper_price=102.0,
            lower_price=98.0,
            num_grids=4,
            leverage=5,
            is_active=True,
            grid_id="grid_TEST",
        )

        asyncio.run(monitor.monitor_grid(grid))

        self.assertEqual(ws.ticker_calls, 1)
        self.assertEqual(ws.position_calls, 0)
        self.assertEqual(ws.execution_calls, 0)


if __name__ == "__main__":
    unittest.main()
