import logging
import unittest
from types import SimpleNamespace

from dry_run_engine import DryRunEngine, DryRunState
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
    def test_stagnation_never_closes_filled_negative_position(self):
        manager = MultiGridManager.__new__(MultiGridManager)

        losing_reason = manager._stagnation_close_reason(
            age_seconds=3600,
            fills=4,
            total_pnl=-0.25,
            seconds_since_progress=max(LOSING_STAGNANT_TIMEOUT_SECONDS, STAGNANT_GRID_TIMEOUT_SECONDS) + 1,
        )

        self.assertIsNone(losing_reason)

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

    def test_dry_run_drawdown_does_not_deactivate_losing_position(self):
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
        engine.state = DryRunState(
            grid=grid,
            started_at=0.0,
            current_price=100.0,
            position_qty=1.0,
            position_side="Buy",
            entry_price=100.0,
            is_active=True,
        )

        event = engine.on_price_update(90.0)

        self.assertIsNone(event)
        self.assertTrue(engine.state.is_active)
        self.assertLess(engine.state.unrealized_pnl, 0)

    def test_dry_run_drawdown_hold_warning_is_throttled(self):
        engine = DryRunEngine()
        engine._drawdown_hold_alert_cooldown_seconds = 60.0
        grid = GridState(
            symbol="TEST/USDT:USDT",
            upper_price=110.0,
            lower_price=90.0,
            num_grids=2,
            grid_levels=[
                GridLevel(index=0, price=100.0, side="Buy", qty=1.0, order_id=None, status="placed"),
                GridLevel(index=1, price=105.0, side="Sell", qty=1.0, order_id=None, status="placed"),
            ],
            leverage=1,
            order_size_usdt=1.0,
        )
        engine.state = DryRunState(
            grid=grid,
            started_at=0.0,
            current_price=100.0,
            position_qty=1.0,
            position_side="Buy",
            entry_price=100.0,
            is_active=True,
        )

        records = []
        handler = logging.Handler()
        handler.emit = records.append
        log = logging.getLogger("dry_run_engine")
        old_level = log.level
        log.setLevel(logging.WARNING)
        log.addHandler(handler)
        try:
            engine.on_price_update(90.0)
            engine.on_price_update(89.0)
        finally:
            log.removeHandler(handler)
            log.setLevel(old_level)

        drawdown_warnings = [r for r in records if "DRAWDOWN HOLD" in r.getMessage()]
        self.assertEqual(len(drawdown_warnings), 1)
        self.assertTrue(engine.state.is_active)

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


if __name__ == "__main__":
    unittest.main()
