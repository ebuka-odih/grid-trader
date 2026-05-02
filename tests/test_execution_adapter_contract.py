import unittest

from execution_adapters.base import (
    GridDeployRequest,
    GridExecutionAdapter,
    GridExecutionState,
)


class FakeAdapter:
    async def deploy_grid(self, request: GridDeployRequest) -> GridExecutionState:
        return GridExecutionState(
            grid_id=f"fake:{request.symbol}",
            symbol=request.symbol,
            active=True,
            leverage=request.leverage,
            grid_levels=[{"side": "buy", "price": request.lower, "status": "open"}],
            metadata={"exchange": request.exchange, "cross_margin": request.cross_margin},
        )

    async def get_status(self, grid_id: str) -> GridExecutionState:
        return GridExecutionState(grid_id=grid_id, symbol="BTC/USDC:USDC", active=True)

    async def stop_grid(self, grid_id: str, reason: str = "manual") -> GridExecutionState:
        return GridExecutionState(grid_id=grid_id, symbol="BTC/USDC:USDC", active=False, close_reason=reason)

    async def close(self) -> None:
        return None


class ExecutionAdapterContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_adapter_satisfies_runtime_protocol(self):
        adapter = FakeAdapter()
        self.assertIsInstance(adapter, GridExecutionAdapter)

        request = GridDeployRequest(
            symbol="BTC/USDC:USDC",
            lower=90000,
            upper=100000,
            num_grids=12,
            leverage=10,
            margin_per_level_usdt=1.25,
        )

        state = await adapter.deploy_grid(request)

        self.assertTrue(state.active)
        self.assertEqual(state.grid_id, "fake:BTC/USDC:USDC")
        self.assertEqual(state.symbol, request.symbol)
        self.assertEqual(state.leverage, 10)
        self.assertEqual(state.grid_levels[0]["price"], 90000)
        self.assertTrue(state.metadata["cross_margin"])

    def test_grid_levels_default_to_empty_list_not_shared_none(self):
        one = GridExecutionState(grid_id="one", symbol="BTC/USDC:USDC", active=True)
        two = GridExecutionState(grid_id="two", symbol="ETH/USDC:USDC", active=True)

        one.grid_levels.append({"side": "buy"})

        self.assertEqual(two.grid_levels, [])


if __name__ == "__main__":
    unittest.main()
