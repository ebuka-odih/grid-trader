import unittest

from coin_scanner import CoinScanner
from config import BASE_ORDER_SIZE_USDT, DEFAULT_LEVERAGE, MIN_SAFE_LEVERAGE, MAX_SAFE_LEVERAGE
from dry_run_engine import DryRunEngine, DryRunState
from grid_engine import GridEngine
from portfolio_risk_monitor import PortfolioRiskMonitor


class MemeFastGridProfileTests(unittest.TestCase):
    def test_big_coins_are_blacklisted_but_meme_style_symbols_are_allowed(self):
        scanner = CoinScanner()
        self.assertTrue(scanner._is_blacklisted("BTC/USDT:USDT"))
        self.assertTrue(scanner._is_blacklisted("ETH/USDT:USDT"))
        self.assertTrue(scanner._is_blacklisted("SOL/USDT:USDT"))
        self.assertTrue(scanner._is_blacklisted("BNB/USDT:USDT"))
        self.assertFalse(scanner._is_blacklisted("1000PEPE/USDT:USDT"))
        self.assertFalse(scanner._is_blacklisted("DOGE/USDT:USDT"))

    def test_default_unknown_token_profile_uses_safe_cross_margin_caps(self):
        risk = PortfolioRiskMonitor()
        profile = risk.get_token_profile("NEWFASTMEME/USDT:USDT")
        self.assertGreaterEqual(profile["leverage"], MIN_SAFE_LEVERAGE)
        self.assertLessEqual(profile["leverage"], MAX_SAFE_LEVERAGE)
        self.assertLessEqual(profile["max_wallet_exposure_pct"], 2.0)
        self.assertIn("target_pnl_pct", profile)

    def test_order_size_is_margin_and_leverage_controls_notional_quantity(self):
        grid = GridEngine().calculate_grid_levels(
            symbol="NEWFASTMEME/USDT:USDT",
            upper=102.0,
            lower=98.0,
            num_grids=4,
            current_price=100.0,
            leverage=50,
            order_size_usdt=5.0,
        )
        self.assertTrue(grid.grid_levels)
        self.assertEqual(grid.order_size_usdt, 5.0)
        self.assertTrue(all(level.qty == 2.5 for level in grid.grid_levels))

    def test_risk_monitor_caps_default_token_to_two_percent_and_safe_leverage(self):
        risk = PortfolioRiskMonitor()
        result = risk.check_deploy(
            symbol="NEWFASTMEME/USDT:USDT",
            direction="neutral",
            leverage=50,
            order_size_usdt=5.0,
            wallet_balance=100.0,
            active_grids={},
            num_grids=10,
        )
        self.assertTrue(result["approved"], result)
        self.assertLessEqual(result["adjusted_order_size"] * 11, 2.0)
        self.assertGreaterEqual(result["adjusted_leverage"], MIN_SAFE_LEVERAGE)
        self.assertLessEqual(result["adjusted_leverage"], MAX_SAFE_LEVERAGE)

    def test_dry_run_closes_on_percentage_profit_not_static_one_dollar(self):
        engine = DryRunEngine()
        grid = GridEngine().calculate_grid_levels(
            symbol="NEWFASTMEME/USDT:USDT",
            upper=102.0,
            lower=98.0,
            num_grids=10,
            current_price=100.0,
            leverage=50,
            order_size_usdt=5.0,
        )
        engine.state = DryRunState(grid=grid, started_at=0, current_price=100.0)
        engine.state.realized_pnl = 0.85
        engine.state.fills = [object(), object(), object(), object()]

        event = engine.on_price_update(100.0)

        self.assertEqual(event, "target_hit")
        self.assertFalse(engine.state.is_active)
        status = engine.get_status()
        self.assertLess(status["target_pnl_low"], 1.0)
        self.assertIn("target_pnl_pct_low", status)


if __name__ == "__main__":
    unittest.main()
