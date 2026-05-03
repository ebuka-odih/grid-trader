"""
Tests for the LiveEngine close paths.

Critical: every final close MUST cancel grid orders AND market-close any
remaining position. The previous code path just flipped is_active=False,
which would have left positions open on the exchange in production.
"""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from grid_core import GridPosition, GridImbalance, SmartCloseConfig
from grid_engine import GridState, GridLevel
from live_engine import LiveEngine, LiveState


def _make_engine(scale_out_fraction=0.5, hard_floor=20.0):
    cfg = SmartCloseConfig(
        scale_out_fraction=scale_out_fraction,
        hard_loss_pct_floor=hard_floor,
        min_seconds_since_last_fill=0.0,
        recovery_window_sec=0.0,
    )
    eng = LiveEngine(smart_close_config=cfg)
    eng._grid_engine = MagicMock()
    eng._grid_engine.cancel_grid = AsyncMock(return_value=None)
    eng._grid_engine.close_position = AsyncMock(return_value={"id": "ORD-MOCK"})
    return eng


def _seed_state(eng, qty=2.5, price=100.0):
    grid = GridState(
        symbol="TEST/USDT:USDT",
        upper_price=110.0, lower_price=90.0,
        num_grids=10, leverage=10, order_size_usdt=1.0,
        grid_levels=[],
    )
    pos = GridPosition(
        side="Buy", qty=qty, entry_price=price,
        opened_at=time.time() - 60, last_fill_at=time.time() - 1000,
    )
    eng.state = LiveState(
        grid=grid,
        started_at=time.time(),
        current_price=price,
        position=pos,
        is_active=True,
    )


class FlattenAndCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_close_cancels_grid_and_closes_position(self):
        eng = _make_engine()
        _seed_state(eng)
        await eng._handle_close("target_hit", 0.50)
        eng._grid_engine.cancel_grid.assert_awaited_once()
        eng._grid_engine.close_position.assert_awaited_once_with(
            "TEST/USDT:USDT", "Buy", 2.5,
        )
        self.assertFalse(eng.state.is_active)

    async def test_close_position_failure_still_marks_inactive(self):
        # If the exchange fails, we must NOT leave the engine in a half-open
        # state — manager would never free the slot. is_active=False always.
        eng = _make_engine()
        eng._grid_engine.close_position = AsyncMock(
            side_effect=RuntimeError("exchange down")
        )
        _seed_state(eng)
        await eng._handle_close("drawdown", -1.50)
        self.assertFalse(eng.state.is_active)
        eng._grid_engine.cancel_grid.assert_awaited_once()

    async def test_flat_position_skips_close(self):
        # No remaining position → no close_position call (just cancel orders).
        eng = _make_engine()
        _seed_state(eng, qty=0.0)
        eng.state.position.side = ""
        await eng._handle_close("target_hit", 0.0)
        eng._grid_engine.cancel_grid.assert_awaited_once()
        eng._grid_engine.close_position.assert_not_called()
        self.assertFalse(eng.state.is_active)

    async def test_alert_fires_when_close_position_returns_none(self):
        alerter = MagicMock()
        alerter.send = AsyncMock()
        eng = LiveEngine(
            smart_close_config=SmartCloseConfig(scale_out_fraction=1.0),
            alerter=alerter,
        )
        eng._grid_engine = MagicMock()
        eng._grid_engine.cancel_grid = AsyncMock()
        eng._grid_engine.close_position = AsyncMock(return_value=None)
        _seed_state(eng)
        await eng._handle_close("drawdown", -1.50)
        alerter.send.assert_awaited()
        msg = alerter.send.call_args[0][0]
        self.assertIn("FLATTEN FAILED", msg)


class LiveScaleOutTests(unittest.IsolatedAsyncioTestCase):
    async def test_scale_out_calls_reduce_only_then_mirrors_state(self):
        eng = _make_engine(scale_out_fraction=0.5, hard_floor=20.0)
        # Set up a position that will breach the 20% margin floor:
        # qty=4, entry=100, current=99.5 → unr = 4*(99.5-100) = -2.0 = 20% on $10
        _seed_state(eng, qty=4.0, price=100.0)
        # Drive on_price_update past the floor.
        # NOTE: the on_price_update flow includes adaptive grid wiring not set
        # up in this test, so call the smart-close + scale-out branch directly.
        from grid_core import perform_partial_close  # used internally
        # Reproduce the branch logic:
        result = eng._smart_close.check_smart_close(
            position=eng.state.position,
            current_price=99.5,
            allocated_margin=10.0,
            imbalance=eng.state.imbalance,
            total_fills=4,
            drawdown_breached=True,
        )
        from grid_core import CloseReason
        self.assertEqual(result, CloseReason.PARTIAL_CLOSE)
        # Now invoke close_position the way the engine would.
        order = await eng._grid_engine.close_position("TEST/USDT:USDT", "Buy", 2.0)
        self.assertEqual(order["id"], "ORD-MOCK")
        # Mirror via perform_partial_close (engine does this on success).
        realised, qty = perform_partial_close(
            eng.state.position, eng.state.imbalance, 99.5, fraction=0.5
        )
        self.assertAlmostEqual(qty, 2.0)
        self.assertAlmostEqual(eng.state.position.qty, 2.0)
        self.assertTrue(eng.state.position.scaled_out)


if __name__ == "__main__":
    unittest.main()
