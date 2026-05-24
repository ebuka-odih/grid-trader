import asyncio
import unittest
from unittest.mock import patch

from coin_scanner import CoinScore
from grid_engine import GridEngine


class FakeExchange:
    def __init__(self):
        self.set_leverage_calls = 0
        self.limit_order_calls = 0

    async def set_leverage(self, leverage, symbol):
        self.set_leverage_calls += 1
        raise AssertionError("DRY_RUN must not call exchange.set_leverage")

    async def create_limit_order(self, symbol, side, amount, price):
        self.limit_order_calls += 1
        raise AssertionError("DRY_RUN must not call exchange.create_limit_order")

    async def close(self):
        pass


def sample_coin():
    return CoinScore(
        symbol="TEST/USDT:USDT",
        price=100.0,
        high_24h=102.0,
        low_24h=98.0,
        volume_24h_usdt=10_000_000,
        atr_pct=1.0,
        range_pct=4.0,
        mean_reversion_score=0.8,
        grid_score=0.9,
        suggested_upper=102.0,
        suggested_lower=98.0,
        suggested_grids=4,
        suggested_leverage=5,
    )


class GridEngineSafetyTests(unittest.TestCase):
    def test_calculate_grid_levels_uses_explicit_order_size_usdt_as_margin(self):
        engine = GridEngine()
        grid = engine.calculate_grid_levels(
            symbol="TEST/USDT:USDT",
            upper=102.0,
            lower=98.0,
            num_grids=4,
            current_price=100.0,
            leverage=5,
            order_size_usdt=20.0,
        )

        self.assertTrue(grid.grid_levels)
        self.assertTrue(all(level.qty == 1.0 for level in grid.grid_levels))

    def test_calculate_grid_levels_generates_unique_grid_ids_for_redeploys(self):
        engine = GridEngine()

        first = engine.calculate_grid_levels(
            symbol="TEST/USDT:USDT",
            upper=102.0,
            lower=98.0,
            num_grids=4,
            current_price=100.0,
            leverage=5,
        )
        second = engine.calculate_grid_levels(
            symbol="TEST/USDT:USDT",
            upper=102.0,
            lower=98.0,
            num_grids=4,
            current_price=100.0,
            leverage=5,
        )

        self.assertNotEqual(first.grid_id, second.grid_id)

    def test_quick_deploy_in_dry_run_never_calls_private_exchange_methods(self):
        # This test verifies the grid engine's dry-run behavior. The system is
        # currently configured as LIVE (DRY_RUN=false) — simulate DRY_RUN for
        # this test by temporarily patching.
        with patch('grid_engine.DRY_RUN', True):
            engine = GridEngine()
            fake_exchange = FakeExchange()
            engine.exchange = fake_exchange

            grid = asyncio.run(engine.quick_deploy(sample_coin()))

            self.assertEqual(fake_exchange.set_leverage_calls, 0)
            self.assertEqual(fake_exchange.limit_order_calls, 0)
            self.assertTrue(grid.is_active)
            self.assertTrue(grid.grid_levels)
            self.assertTrue(all(level.status == "placed" for level in grid.grid_levels))


if __name__ == "__main__":
    unittest.main()
