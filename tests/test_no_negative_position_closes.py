import logging
import time
import unittest
from types import SimpleNamespace

from dry_run_engine import DryRunEngine, DryRunState
from grid_core import GridPosition
from grid_engine import GridLevel, GridState
from heartbeat_regulator import HeartbeatRegulator
from multi_grid_manager import MultiGridManager, LOSING_STAGNANT_TIMEOUT_SECONDS, STAGNANT_GRID_TIMEOUT_SECONDS
from trade_close_optimizer import GridStatus, TradeCloseOptimizer


class DummyTask:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


def status(**overrides):
    data = dict(
        fills=4,
        realized_pnl=0.0,
        unrealized_pnl=-0.25,
        allocated_margin=10.0,
        order_size_usdt=1.0,
        avg_entry_price=100.0,
        current_bid=99.0,
        current_ask=99.02,
        current_mark=99.01,
        direction="long",
        grid_levels=10,
        filled_levels=4,
        age_seconds=3600,
    )
    data.update(overrides)
    return GridStatus(**data)


class NoNegativePositionCloseTests(unittest.TestCase):
    def test_stagnation_closes_filled_negative_position_after_timeout(self):
        manager = MultiGridManager.__new__(MultiGridManager)

        losing_reason = manager._stagnation_close_reason(
            age_seconds=3600,
            fills=4,
            total_pnl=-0.25,
            seconds_since_progress=max(LOSING_STAGNANT_TIMEOUT_SECONDS, STAGNANT_GRID_TIMEOUT_SECONDS) + 1,
        )

        self.assertEqual(losing_reason, "losing_stagnant")

    def test_stagnation_can_close_profitable_or_empty_slots(self):
        manager = MultiGridManager.__new__(MultiGridManager)

        profitable_reason = manager._stagnation_close_reason(
            age_seconds=3600,
            fills=4,
            total_pnl=0.05,
            seconds_since_progress=STAGNANT_GRID_TIMEOUT_SECONDS + 1,
        )
        empty_reason = manager._stagnation_close_reason(
            age_seconds=3600,
            fills=0,
            total_pnl=0.0,
            seconds_since_progress=10,
        )

        self.assertEqual(profitable_reason, "stagnant_no_progress")
        self.assertEqual(empty_reason, "no_fills_timeout")

    def test_profit_optimizer_holds_negative_drawdown_and_stale_positions(self):
        optimizer = TradeCloseOptimizer(max_drawdown_pct=1.0)

        drawdown_decision = optimizer.should_close(status(unrealized_pnl=-0.50, age_seconds=60))
        stale_decision = optimizer.should_close(status(unrealized_pnl=-0.10, age_seconds=3600, filled_levels=1))

        self.assertFalse(drawdown_decision.should_close)
        self.assertIn("negative", drawdown_decision.reason.lower())
        self.assertFalse(stale_decision.should_close)
        self.assertIn("negative", stale_decision.reason.lower())

    def test_profit_optimizer_can_close_realized_positive_position(self):
        optimizer = TradeCloseOptimizer(target_pnl_pct_low=2.0, min_net_profit_usdt=0.02)

        decision = optimizer.should_close(status(realized_pnl=0.35, unrealized_pnl=0.05, allocated_margin=10.0, age_seconds=300))

        self.assertTrue(decision.should_close)
        self.assertGreater(decision.net_pnl, 0)

    def test_dry_run_drawdown_closes_losing_position(self):
        engine = DryRunEngine()
        grid = GridState(
            symbol="TEST/USDT:USDT",
            upper_price=110.0,
            lower_price=90.0,
            num_grids=2,
            grid_levels=[
                GridLevel(index=0, price=100.0, side="Buy", qty=1.0, order_id=None, status="filled"),
                GridLevel(index=1, price=105.0, side="Sell", qty=1.0, order_id=None, status="placed"),
            ],
            leverage=1,
            order_size_usdt=1.0,
        )
        pos = GridPosition(side="Buy", qty=1.0, entry_price=100.0)
        pos.update_unrealized(100.0)
        engine.state = DryRunState(
            grid=grid,
            started_at=0.0,
            current_price=100.0,
            position=pos,
            is_active=True,
        )

        # Margin-based hard floor + scale-out: first breach halves the
        # position (returns "partial_close"), keeping the grid alive for
        # recovery. A subsequent breach below the floor closes the rest.
        first = engine.on_price_update(90.0)
        self.assertEqual(first, "partial_close")
        self.assertTrue(engine.state.is_active)
        self.assertTrue(engine.state.position.scaled_out)

        second = engine.on_price_update(85.0)
        self.assertEqual(second, "drawdown")
        self.assertFalse(engine.state.is_active)
        self.assertLess(engine.state.position.realized_pnl, 0)

    def test_dry_run_drawdown_closes_before_hold_warning(self):
        engine = DryRunEngine()
        grid = GridState(
            symbol="TEST/USDT:USDT",
            upper_price=110.0,
            lower_price=90.0,
            num_grids=2,
            grid_levels=[
                GridLevel(index=0, price=100.0, side="Buy", qty=1.0, order_id=None, status="filled"),
                GridLevel(index=1, price=105.0, side="Sell", qty=1.0, order_id=None, status="placed"),
            ],
            leverage=1,
            order_size_usdt=1.0,
        )
        pos = GridPosition(side="Buy", qty=1.0, entry_price=100.0)
        pos.update_unrealized(100.0)
        engine.state = DryRunState(
            grid=grid,
            started_at=0.0,
            current_price=100.0,
            position=pos,
            is_active=True,
        )

        # First deep-drawdown tick scales out half (kept alive for recovery).
        # A second breach closes the rest as drawdown.
        first = engine.on_price_update(90.0)
        self.assertEqual(first, "partial_close")
        second = engine.on_price_update(85.0)
        self.assertEqual(second, "drawdown")
        self.assertFalse(engine.state.is_active)

    def test_heartbeat_holds_stale_negative_position_instead_of_cancelling(self):
        manager = SimpleNamespace(slots={})
        task = DummyTask()
        slot = SimpleNamespace(
            slot_id=7,
            symbol="NEG/USDT:USDT",
            task=task,
            close_reason="",
            state=SimpleNamespace(is_active=True),
            engine=SimpleNamespace(get_status=lambda: {"fills": 3, "total_pnl": -0.12}),
        )
        manager.slots = {7: slot}
        hb = HeartbeatRegulator(manager)

        actions = []
        hb._close_stale_slots(["NEG/USDT:USDT"], actions)

        self.assertTrue(slot.state.is_active)
        self.assertFalse(task.cancelled)
        self.assertNotEqual(slot.close_reason, "heartbeat_stale_price")
        self.assertIn("hold_negative_stale_grid:7:NEG/USDT:USDT", actions)
    def test_dry_run_does_not_close_small_negative_position(self):
        engine = DryRunEngine()
        grid = GridState(
            symbol="SPIKE/USDT:USDT",
            upper_price=110.0,
            lower_price=90.0,
            num_grids=2,
            grid_levels=[
                GridLevel(index=0, price=100.0, side="Buy", qty=1.0, order_id=None, status="filled"),
                GridLevel(index=1, price=95.0, side="Buy", qty=1.0, order_id=None, status="filled"),
            ],
            leverage=50,
            order_size_usdt=1.0,
        )
        pos = GridPosition(side="Buy", qty=2.0, entry_price=97.5)
        pos.update_unrealized(100.0)
        engine.state = DryRunState(
            grid=grid,
            started_at=0.0,
            current_price=100.0,
            position=pos,
            is_active=True,
            filled_levels={0, 1},  # Mark both levels as already filled
        )

        # Small negative move that doesn't breach drawdown should not close
        event = engine.on_price_update(99.5)

        self.assertIsNone(event)
        self.assertTrue(engine.state.is_active)


if __name__ == "__main__":
    unittest.main()
