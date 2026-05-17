import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coin_scanner import CoinScanner, CoinScore


class ScannerEntryShapePhase1Tests(unittest.TestCase):
    def test_coin_score_supports_entry_shape_fields(self):
        score = CoinScore(
            symbol="TEST/USDT:USDT",
            price=100.0,
            high_24h=110.0,
            low_24h=90.0,
            volume_24h_usdt=1_000_000.0,
            atr_pct=1.2,
            range_pct=8.0,
            mean_reversion_score=0.72,
            grid_score=0.84,
            suggested_upper=108.0,
            suggested_lower=94.0,
            suggested_grids=12,
            suggested_leverage=10,
            trend_direction="neutral",
            market_regime="ranging",
            entry_quality_score=0.81,
            range_position=0.35,
            vwap_distance_pct=-0.4,
            pullback_depth_pct=1.1,
            slope_score=0.02,
            acceleration_score=-0.01,
            htf_slope_score=0.01,
            ltf_slope_score=-0.005,
            mtf_alignment_score=0.45,
        )
        self.assertEqual(score.market_regime, "ranging")
        self.assertGreater(score.entry_quality_score, 0.8)
        self.assertGreater(score.mtf_alignment_score, 0.4)

    def test_range_position_is_normalized_between_zero_and_one(self):
        scanner = CoinScanner.__new__(CoinScanner)
        value = scanner._range_position(current_price=100.0, high_lookback=112.0, low_lookback=90.0)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 1.0)

    def test_rolling_vwap_uses_volume_weighted_average(self):
        scanner = CoinScanner.__new__(CoinScanner)
        df = pd.DataFrame(
            {
                "close": [100.0, 101.0, 103.0],
                "volume": [1.0, 2.0, 3.0],
            }
        )
        self.assertAlmostEqual(scanner._rolling_vwap(df), 101.8333333333, places=6)

    def test_vwap_distance_pct_is_signed_percent(self):
        scanner = CoinScanner.__new__(CoinScanner)
        value = scanner._vwap_distance_pct(current_price=102.0, vwap_price=100.0)
        self.assertAlmostEqual(value, 2.0, places=6)

    def test_pullback_depth_pct_is_absolute_percent_from_extreme(self):
        scanner = CoinScanner.__new__(CoinScanner)
        value = scanner._pullback_depth_pct(current_price=99.0, swing_extreme=105.0)
        self.assertAlmostEqual(value, (6.0 / 105.0) * 100.0, places=6)


class EntryShapePlannerPhase23Tests(unittest.TestCase):
    def test_regime_classifier_prefers_ranging_for_high_mr_series(self):
        scanner = CoinScanner.__new__(CoinScanner)
        regime = scanner._classify_market_regime(
            mr_score=0.82,
            slope=0.0001,
            atr_pct=1.2,
            range_position=0.45,
            htf_slope=0.00005,
            ltf_slope=0.00002,
            m15_mr_score=0.82,
            alignment_score=0.10,
        )
        self.assertEqual(regime, "ranging")

    def test_regime_classifier_marks_volatile_when_atr_is_high_and_trend_aligned(self):
        scanner = CoinScanner.__new__(CoinScanner)
        regime = scanner._classify_market_regime(
            mr_score=0.40,
            slope=0.0003,
            atr_pct=3.4,
            range_position=0.45,
            htf_slope=0.00035,
            ltf_slope=0.00025,
            m15_mr_score=0.40,
            alignment_score=0.8,
        )
        self.assertEqual(regime, "volatile")

    def test_regime_classifier_uses_htf_bias_for_trending_up(self):
        scanner = CoinScanner.__new__(CoinScanner)
        regime = scanner._classify_market_regime(
            mr_score=0.48,
            slope=0.00012,
            atr_pct=1.5,
            range_position=0.36,
            htf_slope=0.0003,
            ltf_slope=-0.00001,
            m15_mr_score=0.48,
            alignment_score=0.42,
        )
        self.assertEqual(regime, "trending_up")

    def test_compute_entry_quality_penalizes_ranging_mid_chop(self):
        from entry_shape_planner import compute_entry_quality

        score = compute_entry_quality(
            market_regime="ranging",
            range_position=0.50,
            vwap_distance_pct=0.02,
            pullback_depth_pct=0.10,
            atr_pct=1.0,
        )
        self.assertLess(score, 0.5)

    def test_compute_entry_quality_rewards_trend_pullback(self):
        from entry_shape_planner import compute_entry_quality

        score = compute_entry_quality(
            market_regime="trending_up",
            range_position=0.42,
            vwap_distance_pct=-1.1,
            pullback_depth_pct=1.2,
            atr_pct=1.0,
        )
        self.assertGreater(score, 0.6)

    def test_ranging_plan_anchors_bounds_to_range_edges_not_raw_atr_box(self):
        from entry_shape_planner import plan_entry_shape

        plan = plan_entry_shape(
            current_price=100.0,
            market_regime="ranging",
            atr=2.0,
            swing_high=108.0,
            swing_low=94.0,
            range_position=0.25,
            vwap_price=99.0,
            pullback_depth_pct=1.5,
        )
        self.assertLessEqual(plan.lower, 95.0)
        self.assertGreaterEqual(plan.upper, 106.0)
        self.assertEqual(plan.template_name, "range_reversion")

    def test_trending_up_plan_biases_buy_density_below_price(self):
        from entry_shape_planner import plan_entry_shape

        plan = plan_entry_shape(
            current_price=100.0,
            market_regime="trending_up",
            atr=2.0,
            swing_high=109.0,
            swing_low=92.0,
            range_position=0.38,
            vwap_price=98.8,
            pullback_depth_pct=1.0,
        )
        self.assertEqual(plan.template_name, "trend_pullback_long")
        self.assertEqual(plan.spacing_mode, "buy_weighted")
        self.assertGreater(plan.buy_density_bias, plan.sell_density_bias)
        self.assertLess(plan.lower, 100.0)
        self.assertGreater(plan.upper, 100.0)

    def test_score_coin_uses_entry_shape_plan_for_suggested_bounds(self):
        scanner = CoinScanner.__new__(CoinScanner)
        ticker = {
            "symbol": "TEST/USDT:USDT",
            "last": 100.0,
            "high": 110.0,
            "low": 90.0,
            "quoteVolume": 2_000_000.0,
        }
        df_15m = pd.DataFrame(
            {
                "high": [101.0, 104.0, 106.0, 108.0, 109.0, 110.0, 111.0, 112.0],
                "low": [97.0, 96.0, 95.0, 94.0, 95.0, 96.0, 97.0, 98.0],
                "close": [99.0, 100.0, 101.0, 100.5, 101.5, 102.0, 101.8, 102.5],
                "volume": [10.0, 10.0, 15.0, 20.0, 18.0, 17.0, 16.0, 19.0],
            }
        )
        df_1h = pd.DataFrame(
            {
                "high": [102.0, 104.0, 106.0, 109.0, 111.0, 113.0, 115.0, 116.0],
                "low": [93.0, 94.0, 95.0, 96.0, 97.0, 98.0, 99.0, 100.0],
                "close": [95.0, 97.0, 99.0, 101.0, 103.0, 105.0, 106.0, 107.0],
                "volume": [30.0] * 8,
            }
        )
        df_5m = pd.DataFrame(
            {
                "high": [100.0, 100.8, 101.2, 101.6, 102.0, 102.2, 102.5, 102.9],
                "low": [98.8, 99.1, 99.4, 99.8, 100.0, 100.4, 100.8, 101.0],
                "close": [99.2, 99.8, 100.3, 100.8, 101.1, 101.6, 102.0, 102.4],
                "volume": [6.0] * 8,
            }
        )

        with patch.object(scanner, "_calculate_atr", return_value=2.0), patch.object(
            scanner, "_mean_reversion_score", return_value=0.48
        ), patch("coin_scanner.compute_entry_quality", return_value=0.88), patch(
            "coin_scanner.plan_entry_shape"
        ) as planner:
            planner.return_value.lower = 96.5
            planner.return_value.upper = 109.5
            planner.return_value.num_grids = 12
            planner.return_value.template_name = "trend_pullback_long"
            planner.return_value.spacing_mode = "buy_weighted"
            planner.return_value.buy_density_bias = 0.72
            planner.return_value.sell_density_bias = 0.28
            planner.return_value.notes = "test plan"

            score = scanner._score_coin(ticker, df_15m, df_1h=df_1h, df_5m=df_5m)

        self.assertEqual(score.suggested_lower, 96.5)
        self.assertEqual(score.suggested_upper, 109.5)
        self.assertEqual(score.suggested_grids, 12)
        self.assertEqual(score.market_regime, "trending_up")
        self.assertEqual(score.trend_direction, "long")
        self.assertGreater(score.mtf_alignment_score, 0.0)
        self.assertEqual(score.entry_shape_notes, "test plan")


class GridEngineEntryShapeFlowTests(unittest.TestCase):
    def test_buy_weighted_spacing_creates_more_buy_levels(self):
        from grid_engine import GridEngine

        calc = GridEngine.__new__(GridEngine)
        grid = calc.calculate_grid_levels(
            symbol="TEST/USDT:USDT",
            upper=110.0,
            lower=90.0,
            num_grids=10,
            current_price=100.0,
            leverage=10,
            spacing_mode="buy_weighted",
            buy_density_bias=0.75,
            sell_density_bias=0.35,
        )

        buy_levels = [lvl for lvl in grid.grid_levels if lvl.side == "Buy"]
        sell_levels = [lvl for lvl in grid.grid_levels if lvl.side == "Sell"]
        self.assertGreater(len(buy_levels), len(sell_levels))
        self.assertEqual(grid.spacing_mode, "buy_weighted")
        self.assertAlmostEqual(grid.buy_density_bias, 0.75, places=4)

    def test_buy_weighted_spacing_compresses_buy_levels_near_price(self):
        from grid_engine import GridEngine

        calc = GridEngine.__new__(GridEngine)
        grid = calc.calculate_grid_levels(
            symbol="TEST/USDT:USDT",
            upper=110.0,
            lower=90.0,
            num_grids=10,
            current_price=100.0,
            leverage=10,
            spacing_mode="buy_weighted",
            buy_density_bias=0.80,
            sell_density_bias=0.30,
        )

        buy_prices = sorted([lvl.price for lvl in grid.grid_levels if lvl.side == "Buy"])
        self.assertGreater(len(buy_prices), 1)
        nearest_gap = 100.0 - buy_prices[-1]
        furthest_gap = 100.0 - buy_prices[0]
        self.assertLess(nearest_gap, furthest_gap)


if __name__ == "__main__":
    unittest.main()
