"""
Tests for grid_core: shared logic, fill processing, smart close.
Replaces stale test_filter_strategy_and_profit_lock.py.
"""

import time
import unittest

from grid_core import (
    GridPosition, FillEvent, PnLResult, CycleState, GridImbalance,
    SmartCloseEngine, SmartCloseConfig, CloseReason,
    process_fill, check_close_conditions, reset_position, reset_imbalance,
    allocated_margin_usdt, target_pnl_usdt, drawdown_limit_usdt,
)


class TestGridPosition(unittest.TestCase):
    def test_flat_position(self):
        pos = GridPosition()
        self.assertTrue(pos.is_flat)
        self.assertEqual(pos.age_hours, 0.0)

    def test_open_long(self):
        pos = GridPosition()
        fill = FillEvent(level_index=0, side="Buy", price=100.0, qty=0.1, timestamp=time.time())
        pnl = process_fill(fill, pos, GridImbalance())
        self.assertEqual(pnl, 0.0)
        self.assertFalse(pos.is_flat)
        self.assertEqual(pos.side, "Buy")
        self.assertAlmostEqual(pos.qty, 0.1)
        self.assertAlmostEqual(pos.entry_price, 100.0)

    def test_close_long_profit(self):
        pos = GridPosition()
        imb = GridImbalance()
        # Open long
        process_fill(FillEvent(0, "Buy", 100.0, 0.1, time.time()), pos, imb)
        # Close long at profit
        pnl = process_fill(FillEvent(1, "Sell", 110.0, 0.1, time.time()), pos, imb)
        self.assertAlmostEqual(pnl, 1.0)  # (110-100)*0.1
        self.assertTrue(pos.is_flat)
        self.assertAlmostEqual(pos.realized_pnl, 1.0)

    def test_close_long_loss(self):
        pos = GridPosition()
        imb = GridImbalance()
        process_fill(FillEvent(0, "Buy", 100.0, 0.1, time.time()), pos, imb)
        pnl = process_fill(FillEvent(1, "Sell", 90.0, 0.1, time.time()), pos, imb)
        self.assertAlmostEqual(pnl, -1.0)  # (90-100)*0.1
        self.assertTrue(pos.is_flat)
        self.assertAlmostEqual(pos.realized_pnl, -1.0)

    def test_averaging_down(self):
        pos = GridPosition()
        imb = GridImbalance()
        process_fill(FillEvent(0, "Buy", 100.0, 0.1, time.time()), pos, imb)
        process_fill(FillEvent(1, "Buy", 90.0, 0.1, time.time()), pos, imb)
        # avg = (100*0.1 + 90*0.1) / 0.2 = 95
        self.assertAlmostEqual(pos.entry_price, 95.0)
        self.assertAlmostEqual(pos.qty, 0.2)


class TestCheckCloseConditions(unittest.TestCase):
    def test_target_hit(self):
        pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0, opened_at=time.time())
        pos.realized_pnl = 0.5
        pos.unrealized_pnl = 0.5
        result = check_close_conditions(pos, 110.0, 10.0, 2.0, 4.0, 8.0, total_fills=4)
        self.assertTrue(result.should_close)
        self.assertEqual(result.close_reason, "target_hit")

    def test_drawdown_breach(self):
        pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0, opened_at=time.time())
        result = check_close_conditions(pos, 90.0, 10.0, 2.0, 4.0, 8.0, total_fills=4)
        self.assertTrue(result.should_close)
        self.assertEqual(result.close_reason, "drawdown")

    def test_hold_within_limits(self):
        pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0, opened_at=time.time())
        result = check_close_conditions(pos, 99.0, 10.0, 2.0, 4.0, 8.0, total_fills=4)
        self.assertFalse(result.should_close)


class TestGridImbalance(unittest.TestCase):
    def test_balanced(self):
        imb = GridImbalance()
        imb.record_fill("Buy")
        imb.record_fill("Sell")
        self.assertAlmostEqual(imb.imbalance_ratio, 1.0)

    def test_imbalanced(self):
        imb = GridImbalance()
        for _ in range(6):
            imb.record_fill("Buy")
        imb.record_fill("Sell")
        self.assertEqual(imb.dominant_side, "Buy")
        self.assertGreater(imb.imbalance_ratio, 3.0)


class TestSmartClose(unittest.TestCase):
    def test_time_decay_triggers(self):
        config = SmartCloseConfig(time_decay_hours=1.0, time_decay_min_loss_pct=0.5)
        engine = SmartCloseEngine(config)
        pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0, opened_at=time.time() - 7200)
        imb = GridImbalance()
        reason = engine.check_smart_close(pos, 98.0, 10.0, imb, 10)
        self.assertEqual(reason, CloseReason.TIME_DECAY)

    def test_time_decay_no_trigger_when_profit(self):
        config = SmartCloseConfig(time_decay_hours=1.0)
        engine = SmartCloseEngine(config)
        pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0, opened_at=time.time() - 7200)
        reason = engine.check_smart_close(pos, 102.0, 10.0, GridImbalance(), 10)
        self.assertIsNone(reason)

    def test_imbalance_close(self):
        config = SmartCloseConfig(imbalance_ratio_threshold=3.0, imbalance_min_fills=4)
        engine = SmartCloseEngine(config)
        pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0, opened_at=time.time() - 3600)
        imb = GridImbalance(buy_fills=8, sell_fills=1, last_side="Buy", consecutive_same_side=7)
        reason = engine.check_smart_close(pos, 98.5, 10.0, imb, 10)
        self.assertEqual(reason, CloseReason.GRID_IMBALANCE)

    def test_no_close_when_flat(self):
        engine = SmartCloseEngine()
        reason = engine.check_smart_close(GridPosition(), 100.0, 10.0, GridImbalance(), 5)
        self.assertIsNone(reason)


class TestCycleState(unittest.TestCase):
    def test_single_cycle(self):
        cs = CycleState(max_cycles=1)
        done = cs.complete_cycle(5.0)
        self.assertTrue(done)
        self.assertEqual(cs.cycles_completed, 1)
        self.assertAlmostEqual(cs.cumulative_pnl, 5.0)

    def test_multi_cycle(self):
        cs = CycleState(max_cycles=3)
        self.assertFalse(cs.complete_cycle(1.0))
        self.assertFalse(cs.complete_cycle(2.0))
        self.assertTrue(cs.complete_cycle(3.0))
        self.assertAlmostEqual(cs.cumulative_pnl, 6.0)


class TestMarginCalculations(unittest.TestCase):
    def test_allocated_margin(self):
        self.assertAlmostEqual(allocated_margin_usdt(10.0, 5), 50.0)

    def test_target_pnl(self):
        self.assertAlmostEqual(target_pnl_usdt(10.0, 5, 2.0), 1.0)

    def test_drawdown_limit(self):
        self.assertAlmostEqual(drawdown_limit_usdt(10.0, 5, 8.0), 4.0)


if __name__ == "__main__":
    unittest.main()
