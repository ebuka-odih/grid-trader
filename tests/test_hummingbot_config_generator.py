import tempfile
import unittest
from pathlib import Path

from execution_adapters.base import GridDeployRequest
from execution_adapters.hummingbot_config import HummingbotConfigGenerator


class HummingbotConfigGeneratorTests(unittest.TestCase):
    def test_generates_isolated_pmm_config_with_leverage_cap_and_grid_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generator = HummingbotConfigGenerator(hummingbot_home=tmpdir)
            request = GridDeployRequest(
                symbol="AAVE/USDC:USDC",
                lower=180.0,
                upper=200.0,
                num_grids=15,
                leverage=125,
                margin_per_level_usdt=2.5,
                exchange="hyperliquid_perpetual",
            )

            result = generator.write_strategy_config(request)

            self.assertTrue(result.path.exists())
            self.assertIn("generated", result.path.parts)
            self.assertEqual(result.market, "AAVE-USDC")
            self.assertLessEqual(result.leverage, 100)

            text = result.path.read_text()
            self.assertIn("strategy: perpetual_market_making", text)
            self.assertIn("exchange: hyperliquid_perpetual", text)
            self.assertIn("market: AAVE-USDC", text)
            self.assertIn("leverage: 100", text)
            self.assertIn("grid_levels: 15", text)
            self.assertIn("order_amount: 2.5", text)
            self.assertIn("lower_price: 180.0", text)
            self.assertIn("upper_price: 200.0", text)

    def test_symbol_conversion_supports_bybit_perpetual_usdt_markets(self):
        generator = HummingbotConfigGenerator(hummingbot_home=Path("/tmp/hb"))
        self.assertEqual(
            generator.convert_market("DOGE/USDT:USDT", "bybit_perpetual"),
            "DOGE-USDT",
        )

    def test_spread_conversion_is_centered_on_grid_bounds(self):
        generator = HummingbotConfigGenerator(hummingbot_home=Path("/tmp/hb"))
        spreads = generator.calculate_spreads(lower=90.0, upper=110.0)
        self.assertAlmostEqual(spreads.bid_spread, 10.0, places=4)
        self.assertAlmostEqual(spreads.ask_spread, 10.0, places=4)


if __name__ == "__main__":
    unittest.main()
