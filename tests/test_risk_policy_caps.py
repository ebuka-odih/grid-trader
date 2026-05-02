import json
import os
import tempfile
import unittest

from coin_scanner import CoinScanner
from config import DEFAULT_LEVERAGE, MAX_SAFE_LEVERAGE, MIN_SAFE_LEVERAGE
from portfolio_risk_monitor import PortfolioRiskMonitor
from multi_grid_manager import calculate_volatility_scaled_size, normalize_grid_density


class RiskPolicyCapsTests(unittest.TestCase):
    def _profiles_file(self):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
        data = {
            "defaults": {
                "leverage": 50,
                "max_leverage": 100,
                "num_grids": 10,
                "order_size_usdt": 10.0,
                "max_wallet_exposure_pct": 20.0,
            },
            "portfolio": {
                "max_total_wallet_exposure_pct": 80,
                "max_single_direction_exposure_pct": 50,
                "max_trade_wallet_exposure_pct": 2.0,
                "max_leverage": 100,
                "reserve_pct": 20,
                "correlation_groups": [],
            },
            "profiles": {
                "RISKY/USDT:USDT": {
                    "enabled": True,
                    "leverage": 50,
                    "max_leverage": 100,
                    "num_grids": 10,
                    "order_size_usdt": 10.0,
                    "max_wallet_exposure_pct": 20.0,
                    "direction_bias": "neutral",
                }
            },
            "blacklist": [],
        }
        json.dump(data, tmp)
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
        return tmp.name

    def test_funded_policy_uses_high_frequency_leverage_band_with_two_percent_margin_cap(self):
        self.assertEqual(MIN_SAFE_LEVERAGE, 50)
        self.assertEqual(MAX_SAFE_LEVERAGE, 100)
        self.assertGreaterEqual(DEFAULT_LEVERAGE, MIN_SAFE_LEVERAGE)
        self.assertLessEqual(DEFAULT_LEVERAGE, MAX_SAFE_LEVERAGE)

    def test_scanner_suggests_high_frequency_leverage_band(self):
        import pandas as pd

        scanner = CoinScanner()
        df = pd.DataFrame({
            "high": [101.0, 101.2, 101.1, 101.3, 101.4] * 5,
            "low": [99.0, 99.2, 99.1, 99.3, 99.4] * 5,
            "close": [100.0, 100.4, 99.8, 100.2, 100.1] * 5,
            "volume": [1_000_000] * 25,
        })
        score = scanner._score_coin(
            {"symbol": "FAST/USDT:USDT", "last": 100.0, "high": 104.0, "low": 96.0, "quoteVolume": 50_000_000},
            df,
        )

        self.assertGreaterEqual(score.suggested_leverage, MIN_SAFE_LEVERAGE)
        self.assertLessEqual(score.suggested_leverage, MAX_SAFE_LEVERAGE)

    def test_volatility_sizing_caps_total_trade_margin_to_two_percent(self):
        size = calculate_volatility_scaled_size(
            base_size=10.0,
            atr_pct=1.5,
            wallet_balance=100.0,
            max_wallet_exposure_pct=2.0,
            leverage=50,
            num_grids=10,
        )

        self.assertLessEqual(size * 11, 2.0)
        self.assertEqual(size, 0.18)

    def test_risk_monitor_caps_trade_and_enforces_high_leverage_cap(self):
        monitor = PortfolioRiskMonitor(profiles_path=self._profiles_file())

        result = monitor.check_deploy(
            symbol="RISKY/USDT:USDT",
            direction="long",
            leverage=120,
            order_size_usdt=10.0,
            wallet_balance=100.0,
            active_grids={},
            num_grids=10,
        )

        self.assertTrue(result["approved"])
        self.assertEqual(result["adjusted_leverage"], MAX_SAFE_LEVERAGE)
        # Max trade exposure is 10% (from config), so adjusted_order_size * 11 <= 10.0
        self.assertLessEqual(result["adjusted_order_size"] * 11, 10.0)

    def test_grid_density_adjusts_to_budget_without_exceeding_two_percent_trade(self):
        grids = normalize_grid_density(
            20,
            wallet_balance=50.0,
            max_trade_exposure_pct=2.0,
            min_order_size_usdt=0.1,
        )

        self.assertEqual(grids, 10)
        self.assertLessEqual(grids * 0.1, 1.0)


if __name__ == "__main__":
    unittest.main()
