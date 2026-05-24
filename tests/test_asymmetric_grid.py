"""Tests for the restored symmetric two-sided grid deployment behavior."""

import unittest

from coin_scanner import CoinScore
from config import MIN_MEAN_REVERSION
from grid_engine import GridEngine
from multi_grid_manager import MultiGridManager


def _coin_score(price=100.0, upper=110.0, lower=90.0, num_grids=10, leverage=10):
    return CoinScore(
        symbol="TEST/USDT:USDT",
        price=price,
        suggested_upper=upper,
        suggested_lower=lower,
        suggested_grids=num_grids,
        suggested_leverage=leverage,
        grid_score=0.8,
        range_pct=20.0,
        atr_pct=1.0,
        volume_24h_usdt=1e7,
        mean_reversion_score=0.7,
        high_24h=upper,
        low_24h=lower,
    )


class SymmetricGridTests(unittest.TestCase):
    def setUp(self):
        self.mgr = MultiGridManager.__new__(MultiGridManager)
        self.mgr.grid_calc = GridEngine()

    def test_direction_signal_keeps_symmetric_buy_below_sell_above_ladder(self):
        coin = _coin_score(price=100.0, upper=110.0, lower=90.0, num_grids=10)
        grid = self.mgr.grid_calc.calculate_grid_levels(
            symbol=coin.symbol,
            upper=coin.suggested_upper,
            lower=coin.suggested_lower,
            num_grids=coin.suggested_grids,
            current_price=coin.price,
            leverage=coin.suggested_leverage,
            order_size_usdt=1.0,
        )

        below = [l for l in grid.grid_levels if l.price < coin.price]
        above = [l for l in grid.grid_levels if l.price > coin.price]

        self.assertTrue(below, "expected grid levels below current price")
        self.assertTrue(above, "expected grid levels above current price")
        self.assertTrue(all(l.side == "Buy" for l in below))
        self.assertTrue(all(l.side == "Sell" for l in above))

        below_qtys = {round(l.qty, 8) for l in below}
        above_qtys = {round(l.qty, 8) for l in above}
        self.assertEqual(len(below_qtys), 1)
        self.assertEqual(len(above_qtys), 1)
        self.assertEqual(below_qtys, above_qtys)

    def test_neutral_signal_preserves_default_grid(self):
        coin = _coin_score()
        grid = self.mgr.grid_calc.calculate_grid_levels(
            symbol=coin.symbol,
            upper=coin.suggested_upper,
            lower=coin.suggested_lower,
            num_grids=coin.suggested_grids,
            current_price=coin.price,
            leverage=coin.suggested_leverage,
            order_size_usdt=1.0,
        )
        self.assertTrue(all(level.qty > 0 for level in grid.grid_levels))
        self.assertTrue(all(level.side in ("Buy", "Sell") for level in grid.grid_levels))


class MeanReversionThresholdTest(unittest.TestCase):
    def test_min_mr_threshold_tightened(self):
        self.assertGreaterEqual(
            MIN_MEAN_REVERSION,
            0.45,
            f"MIN_MEAN_REVERSION should be >=0.45 to still provide some filtering; got {MIN_MEAN_REVERSION}",
        )


if __name__ == "__main__":
    unittest.main()
