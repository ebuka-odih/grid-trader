import tempfile
import unittest
from pathlib import Path

from execution_adapters.base import GridDeployRequest
from execution_adapters.hummingbot_adapter import HummingbotExecutionAdapter, HummingbotSafetyError


class HummingbotAdapterPaperTests(unittest.IsolatedAsyncioTestCase):
    def _write_client_config(self, home: str, paper_enabled: bool):
        conf = Path(home) / "conf"
        conf.mkdir(parents=True, exist_ok=True)
        (conf / "conf_client.yml").write_text(
            f"instance_id: test\npaper_trade_enabled: {'true' if paper_enabled else 'false'}\n"
        )

    async def test_deploy_writes_config_and_signal_when_paper_mode_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_client_config(tmpdir, paper_enabled=True)
            adapter = HummingbotExecutionAdapter(hummingbot_home=tmpdir, allow_live=False)
            request = GridDeployRequest(
                symbol="BTC/USDC:USDC",
                lower=90000,
                upper=100000,
                num_grids=12,
                leverage=20,
                margin_per_level_usdt=1.5,
                exchange="hyperliquid_perpetual",
            )

            state = await adapter.deploy_grid(request)

            self.assertTrue(state.active)
            self.assertEqual(state.symbol, "BTC/USDC:USDC")
            self.assertEqual(state.leverage, 20)
            self.assertEqual(state.metadata["backend"], "hummingbot_paper")
            self.assertTrue(Path(state.metadata["config_path"]).exists())
            self.assertTrue((Path(tmpdir) / "data" / "grid_trader_hummingbot_signals.json").exists())

            status = await adapter.get_status(state.grid_id)
            self.assertTrue(status.active)
            self.assertEqual(status.grid_id, state.grid_id)

    async def test_refuses_deploy_when_paper_mode_disabled_and_live_not_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_client_config(tmpdir, paper_enabled=False)
            adapter = HummingbotExecutionAdapter(hummingbot_home=tmpdir, allow_live=False)
            request = GridDeployRequest(
                symbol="ETH/USDC:USDC",
                lower=3000,
                upper=3300,
                num_grids=10,
                leverage=10,
                margin_per_level_usdt=1.0,
            )

            with self.assertRaises(HummingbotSafetyError):
                await adapter.deploy_grid(request)

    async def test_stop_grid_keeps_negative_filled_grid_active_without_emergency(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_client_config(tmpdir, paper_enabled=True)
            adapter = HummingbotExecutionAdapter(hummingbot_home=tmpdir)
            state = await adapter.deploy_grid(
                GridDeployRequest(
                    symbol="SOL/USDC:USDC",
                    lower=100,
                    upper=110,
                    num_grids=10,
                    leverage=10,
                    margin_per_level_usdt=1.0,
                )
            )
            adapter._states[state.grid_id].fills = 2
            adapter._states[state.grid_id].total_pnl = -0.25

            held = await adapter.stop_grid(state.grid_id, reason="stale_timeout")

            self.assertTrue(held.active)
            self.assertEqual(held.close_reason, "hold_negative_pnl")

            stopped = await adapter.stop_grid(state.grid_id, reason="emergency")
            self.assertFalse(stopped.active)
            self.assertEqual(stopped.close_reason, "emergency")


if __name__ == "__main__":
    unittest.main()
