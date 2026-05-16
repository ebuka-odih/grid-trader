import unittest

from adaptive_grid import AdaptiveConfig
from backtest_engine import GridConfig, simulate_grid


class BacktestRecenterFillOrderingTests(unittest.TestCase):
    def test_records_crossing_fill_before_recenter(self):
        config = GridConfig(
            symbol="TESTUSDT",
            upper=110.0,
            lower=90.0,
            num_grids=2,
            leverage=1,
            order_size_usdt=10.0,
            target_pnl_pct=1000.0,
            max_drawdown_pct=1000.0,
            timeout_hours=1.0,
        )
        adaptive_config = AdaptiveConfig(
            recenter_trigger_pct=40.0,
            recenter_cooldown_sec=0.0,
            recenter_range_shrink=1.0,
            recenter_range_min_pct=0.1,
            trailing_enabled=False,
            spike_threshold_pct=999.0,
            max_same_side_fills=10,
        )
        ticks = [
            (0.0, 100.0),
            (60.0, 90.0),
        ]

        result = simulate_grid(ticks, config, strategy="v3", adaptive_config=adaptive_config)

        self.assertEqual(result.total_fills, 1)
        self.assertEqual(result.buy_fills, 1)
        self.assertEqual(result.sell_fills, 0)
        self.assertEqual(result.fills[0].level_index, 0)
        self.assertEqual(result.fills[0].side, "Buy")
        self.assertGreaterEqual(result.recenters, 1)


if __name__ == "__main__":
    unittest.main()
