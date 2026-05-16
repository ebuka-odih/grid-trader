import unittest

from backtest_engine import GridConfig, simulate_grid


class BacktestCloseSnapshotTests(unittest.TestCase):
    def test_target_hit_keeps_close_tick_snapshot(self):
        config = GridConfig(
            symbol="TESTUSDT",
            upper=110.0,
            lower=90.0,
            num_grids=2,
            leverage=1,
            order_size_usdt=10.0,
            target_pnl_pct=2.0,
            timeout_hours=10.0,
        )
        ticks = [
            (0.0, 100.0),
            (60.0, 89.0),
            (120.0, 100.0),
            (180.0, 50.0),
        ]

        result = simulate_grid(ticks, config, strategy="v2")
        close_tick_only = simulate_grid(ticks[:3], config, strategy="v2")

        self.assertEqual(result.close_reason, "target_hit")
        self.assertEqual(result.close_timestamp, 120.0)
        self.assertEqual(result.end_price, 100.0)
        self.assertGreater(result.total_pnl, 0.0)
        self.assertGreater(result.unrealized_pnl, 0.0)
        self.assertEqual(result.total_fills, 2)

        # Regression guard: later dataset ticks must not change the already-closed result.
        self.assertEqual(result.close_reason, close_tick_only.close_reason)
        self.assertEqual(result.close_timestamp, close_tick_only.close_timestamp)
        self.assertEqual(result.end_price, close_tick_only.end_price)
        self.assertAlmostEqual(result.realized_pnl, close_tick_only.realized_pnl, places=6)
        self.assertAlmostEqual(result.unrealized_pnl, close_tick_only.unrealized_pnl, places=6)
        self.assertAlmostEqual(result.total_pnl, close_tick_only.total_pnl, places=6)


if __name__ == "__main__":
    unittest.main()
