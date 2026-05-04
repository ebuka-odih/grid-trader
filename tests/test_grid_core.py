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
    perform_partial_close, compute_atr_bucketed_floor,
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
        # check_close_conditions only flags drawdown — SmartCloseEngine
        # gets first chance to evaluate (the engine wraps the fallback close).
        pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0, opened_at=time.time())
        result = check_close_conditions(pos, 90.0, 10.0, 2.0, 4.0, 8.0, total_fills=4)
        self.assertFalse(result.should_close)
        self.assertTrue(result.drawdown_breached)
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
        config = SmartCloseConfig(
            time_decay_hours=1.0, time_decay_min_loss_pct=0.5,
            recovery_window_sec=0.0,  # disable deferral so trigger fires directly
        )
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
        config = SmartCloseConfig(
            imbalance_ratio_threshold=3.0, imbalance_min_fills=4,
            recovery_window_sec=0.0,
        )
        engine = SmartCloseEngine(config)
        pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0, opened_at=time.time() - 3600)
        imb = GridImbalance(buy_fills=8, sell_fills=1, last_side="Buy", consecutive_same_side=7)
        reason = engine.check_smart_close(pos, 98.5, 10.0, imb, 10)
        self.assertEqual(reason, CloseReason.GRID_IMBALANCE)

    def test_post_fill_cooldown_defers_close(self):
        # A fresh fill should defer all smart-close triggers until cooldown elapses.
        config = SmartCloseConfig(
            time_decay_hours=1.0, time_decay_min_loss_pct=0.5,
            min_seconds_since_last_fill=180.0,
            recovery_window_sec=0.0,
            hard_loss_pct_floor=20.0,  # high enough not to trip
        )
        engine = SmartCloseEngine(config)
        pos = GridPosition(
            side="Buy", qty=0.1, entry_price=100.0,
            opened_at=time.time() - 7200,
            last_fill_at=time.time() - 30,  # filled 30s ago
        )
        # Cooldown active → no close even though time-decay would otherwise fire.
        self.assertIsNone(engine.check_smart_close(pos, 98.0, 10.0, GridImbalance(), 10))
        # Simulate cooldown expiry.
        pos.last_fill_at = time.time() - 600
        self.assertEqual(
            engine.check_smart_close(pos, 98.0, 10.0, GridImbalance(), 10),
            CloseReason.TIME_DECAY,
        )

    def test_recovery_window_aborts_close_on_partial_recovery(self):
        config = SmartCloseConfig(
            time_decay_hours=1.0, time_decay_min_loss_pct=0.5,
            min_seconds_since_last_fill=0.0,
            recovery_window_sec=300.0,
            recovery_partial_pct=30.0,
            hard_loss_pct_floor=20.0,
        )
        engine = SmartCloseEngine(config)
        pos = GridPosition(
            side="Buy", qty=0.1, entry_price=100.0,
            opened_at=time.time() - 7200,
        )
        # First fire opens recovery window — close deferred.
        self.assertIsNone(engine.check_smart_close(pos, 97.0, 10.0, GridImbalance(), 10))
        # Price recovers more than 30% of the 3% drawdown → recovery aborts close.
        self.assertIsNone(engine.check_smart_close(pos, 99.0, 10.0, GridImbalance(), 10))
        # Recovery state should be cleared.
        self.assertNotIn("Buy", engine._recovery_state)

    def test_recovery_window_expires_then_closes(self):
        config = SmartCloseConfig(
            time_decay_hours=1.0, time_decay_min_loss_pct=0.5,
            min_seconds_since_last_fill=0.0,
            recovery_window_sec=10.0,
            recovery_partial_pct=30.0,
            hard_loss_pct_floor=20.0,
        )
        engine = SmartCloseEngine(config)
        pos = GridPosition(
            side="Buy", qty=0.1, entry_price=100.0,
            opened_at=time.time() - 7200,
        )
        self.assertIsNone(engine.check_smart_close(pos, 97.0, 10.0, GridImbalance(), 10))
        # Roll the recovery start timestamp back past the window.
        engine._recovery_state["Buy"]["start_ts"] = time.time() - 30
        # Loss has not recovered → close should fire.
        self.assertEqual(
            engine.check_smart_close(pos, 97.0, 10.0, GridImbalance(), 10),
            CloseReason.TIME_DECAY,
        )

    def test_drawdown_breach_routes_through_cooldown(self):
        # A drawdown breach during the post-fill cooldown must be deferred,
        # not force-closed. (This is the regression that turned losses into
        # the dominant close reason in production.)
        config = SmartCloseConfig(
            min_seconds_since_last_fill=180.0,
            recovery_window_sec=300.0,
            recovery_partial_pct=30.0,
            hard_loss_pct_floor=10.0,
        )
        engine = SmartCloseEngine(config)
        pos = GridPosition(
            side="Buy", qty=0.1, entry_price=100.0,
            opened_at=time.time() - 60, last_fill_at=time.time() - 30,
        )
        # 3% loss: under hard floor, but a real drawdown breach.
        # Cooldown is active -> deferred.
        self.assertIsNone(
            engine.check_smart_close(pos, 97.0, 10.0, GridImbalance(), 4,
                                     drawdown_breached=True)
        )

    def test_drawdown_breach_routes_through_recovery_window(self):
        # Past cooldown, drawdown breach opens recovery window (deferred),
        # then a partial bounce aborts the close.
        config = SmartCloseConfig(
            min_seconds_since_last_fill=0.0,
            recovery_window_sec=300.0,
            recovery_partial_pct=30.0,
            hard_loss_pct_floor=10.0,
        )
        engine = SmartCloseEngine(config)
        pos = GridPosition(
            side="Buy", qty=0.1, entry_price=100.0,
            opened_at=time.time() - 60, last_fill_at=time.time() - 1000,
        )
        # First call: opens recovery window, deferred.
        self.assertIsNone(
            engine.check_smart_close(pos, 97.0, 10.0, GridImbalance(), 4,
                                     drawdown_breached=True)
        )
        # Bounce: 50% recovered → abort close.
        self.assertIsNone(
            engine.check_smart_close(pos, 98.5, 10.0, GridImbalance(), 4,
                                     drawdown_breached=True)
        )

    def test_drawdown_breach_closes_after_window_expires(self):
        config = SmartCloseConfig(
            min_seconds_since_last_fill=0.0,
            recovery_window_sec=10.0,
            recovery_partial_pct=30.0,
            hard_loss_pct_floor=10.0,
            min_position_age_sec=0.0,
        )
        engine = SmartCloseEngine(config)
        pos = GridPosition(
            side="Buy", qty=0.1, entry_price=100.0,
            opened_at=time.time() - 60, last_fill_at=time.time() - 1000,
        )
        self.assertIsNone(
            engine.check_smart_close(pos, 97.0, 10.0, GridImbalance(), 4,
                                     drawdown_breached=True)
        )
        engine._recovery_state["Buy"]["start_ts"] = time.time() - 30
        # No bounce → drawdown candidate fires after window expires.
        self.assertEqual(
            engine.check_smart_close(pos, 97.0, 10.0, GridImbalance(), 4,
                                     drawdown_breached=True),
            CloseReason.DRAWDOWN,
        )

    def test_hard_loss_floor_bypasses_cooldown_and_recovery(self):
        config = SmartCloseConfig(
            min_seconds_since_last_fill=600.0,
            recovery_window_sec=600.0,
            hard_loss_pct_floor=4.0,
            scale_out_fraction=1.0,  # disable scale-out: floor breach = full close
            min_position_age_sec=0.0,
        )
        engine = SmartCloseEngine(config)
        # qty=2.5, entry=100, current=98 → unrealized = -5.0 = 50% margin loss
        # which exceeds the 4% hard floor → immediate close, no deferral.
        pos = GridPosition(
            side="Buy", qty=2.5, entry_price=100.0,
            opened_at=time.time() - 60,
            last_fill_at=time.time() - 1,
        )
        self.assertEqual(
            engine.check_smart_close(pos, 98.0, 10.0, GridImbalance(), 1),
            CloseReason.DRAWDOWN,
        )

    def test_no_close_when_flat(self):
        engine = SmartCloseEngine()
        reason = engine.check_smart_close(GridPosition(), 100.0, 10.0, GridImbalance(), 5)
        self.assertIsNone(reason)


class TestAtrBucketedFloor(unittest.TestCase):
    def test_calm_coin(self):
        # ATR<0.5%: clamped to MIN (15%).
        self.assertEqual(
            compute_atr_bucketed_floor(0.3, base_pct=20, min_pct=15, max_pct=30),
            15.0,
        )

    def test_normal_coin(self):
        self.assertEqual(
            compute_atr_bucketed_floor(0.7, base_pct=20, min_pct=15, max_pct=30),
            20.0,
        )

    def test_active_coin(self):
        self.assertEqual(
            compute_atr_bucketed_floor(1.5, base_pct=20, min_pct=15, max_pct=30),
            25.0,
        )

    def test_volatile_coin(self):
        self.assertEqual(
            compute_atr_bucketed_floor(2.5, base_pct=20, min_pct=15, max_pct=30),
            30.0,
        )

    def test_max_clamp(self):
        # Even huge ATR can't exceed max.
        self.assertEqual(
            compute_atr_bucketed_floor(10.0, base_pct=20, min_pct=15, max_pct=30),
            30.0,
        )


class TestMarginBasedLossPct(unittest.TestCase):
    def test_loss_pct_uses_margin_not_price(self):
        # Position: -$2.00 unrealized on $10 allocated margin = 20% margin loss.
        # The old price-% calc would have given (100-99.6)/100 = 0.4% — far below
        # the 5% legacy floor. Margin-% correctly fires the 20% floor.
        cfg = SmartCloseConfig(
            hard_loss_pct_floor=20.0, hard_loss_pct_floor_min=15.0, hard_loss_pct_floor_max=30.0,
            scale_out_fraction=1.0,  # disable scale-out for this test
            min_seconds_since_last_fill=0.0,
            recovery_window_sec=0.0,
            min_position_age_sec=0.0,
        )
        engine = SmartCloseEngine(cfg)
        pos = GridPosition(side="Buy", qty=5.0, entry_price=100.0,
                          opened_at=time.time() - 60, last_fill_at=time.time() - 1000)
        # qty=5, entry=100, current=99.6 → unrealized = 5 * (99.6 - 100) = -2.0
        reason = engine.check_smart_close(pos, 99.6, 10.0, GridImbalance(), 4)
        self.assertEqual(reason, CloseReason.DRAWDOWN)


class TestScaleOut(unittest.TestCase):
    def test_first_breach_returns_partial_close(self):
        cfg = SmartCloseConfig(
            hard_loss_pct_floor=20.0, hard_loss_pct_floor_min=15.0, hard_loss_pct_floor_max=30.0,
            scale_out_fraction=0.5,
            min_seconds_since_last_fill=0.0,
            recovery_window_sec=0.0,
            min_position_age_sec=0.0,
        )
        engine = SmartCloseEngine(cfg)
        pos = GridPosition(side="Buy", qty=5.0, entry_price=100.0,
                          opened_at=time.time() - 60, last_fill_at=time.time() - 1000,
                          scaled_out=False)
        # 25% margin loss > 20% floor.
        reason = engine.check_smart_close(pos, 99.5, 10.0, GridImbalance(), 4)
        self.assertEqual(reason, CloseReason.PARTIAL_CLOSE)

    def test_second_breach_full_close(self):
        cfg = SmartCloseConfig(
            hard_loss_pct_floor=20.0,
            scale_out_fraction=0.5,
            min_seconds_since_last_fill=0.0,
            recovery_window_sec=0.0,
            min_position_age_sec=0.0,
        )
        engine = SmartCloseEngine(cfg)
        pos = GridPosition(side="Buy", qty=2.5, entry_price=100.0,
                          opened_at=time.time() - 60, last_fill_at=time.time() - 1000,
                          scaled_out=True)  # already partial-closed once
        # 25% margin loss again.
        reason = engine.check_smart_close(pos, 99.0, 10.0, GridImbalance(), 4)
        self.assertEqual(reason, CloseReason.DRAWDOWN)

    def test_post_scale_out_uses_unrealized_only_for_floor(self):
        # After scale-out, the realized portion of the loss is a sunk cost.
        # The floor on the remainder must measure ONLY unrealized PnL on the
        # smaller remaining position — otherwise it fires again on the next
        # tick (since realized + unrealized still adds up to the original loss).
        cfg = SmartCloseConfig(
            hard_loss_pct_floor=15.0,
            scale_out_fraction=0.5,
            min_seconds_since_last_fill=0.0,
            recovery_window_sec=300.0,
            min_position_age_sec=0.0,
        )
        engine = SmartCloseEngine(cfg)
        # Position has already been scaled out: half qty, with -$1 realized
        # already booked from the first half-close at the floor price.
        pos = GridPosition(
            side="Buy", qty=2.5, entry_price=100.0,
            opened_at=time.time() - 600, last_fill_at=time.time() - 1000,
            scaled_out=True,
        )
        pos.realized_pnl = -1.5  # banked from the first half-close
        # Current price = 99.4 → unrealized on remaining 2.5 = 2.5 * (99.4 - 100) = -1.5
        # OLD (broken): loss_pct = -(realized + unrealized)/$10 * 100 = 30% → fires floor
        # NEW: loss_pct = -unrealized / ($10/2) * 100 = 1.5/5*100 = 30% — still fires
        # Test: with current price = 99.85 → unrealized = -0.375, half-margin = $5
        #       new loss_pct = 7.5% (under 15% floor) → HOLD (was firing before fix)
        result = engine.check_smart_close(pos, 99.85, 10.0, GridImbalance(), 4)
        self.assertIsNone(result)

    def test_min_position_age_blocks_close(self):
        # No close fires (not even hard floor) on a position younger than
        # min_position_age_sec — protects against new-deploy noise.
        cfg = SmartCloseConfig(
            hard_loss_pct_floor=10.0,
            scale_out_fraction=1.0,
            min_seconds_since_last_fill=0.0,
            recovery_window_sec=0.0,
            min_position_age_sec=90.0,
        )
        engine = SmartCloseEngine(cfg)
        # 30s old, deep loss → still hold.
        pos_young = GridPosition(
            side="Buy", qty=5.0, entry_price=100.0,
            opened_at=time.time() - 30, last_fill_at=time.time() - 1000,
        )
        self.assertIsNone(
            engine.check_smart_close(pos_young, 98.0, 10.0, GridImbalance(), 4)
        )
        # 100s old, same loss → fires.
        pos_old = GridPosition(
            side="Buy", qty=5.0, entry_price=100.0,
            opened_at=time.time() - 100, last_fill_at=time.time() - 1000,
        )
        self.assertEqual(
            engine.check_smart_close(pos_old, 98.0, 10.0, GridImbalance(), 4),
            CloseReason.DRAWDOWN,
        )

    def test_perform_partial_close_halves_qty(self):
        pos = GridPosition()
        imb = GridImbalance()
        process_fill(FillEvent(0, "Buy", 100.0, 1.0, time.time()), pos, imb)
        self.assertAlmostEqual(pos.qty, 1.0)
        realised, qty = perform_partial_close(pos, imb, 95.0, fraction=0.5)
        self.assertAlmostEqual(qty, 0.5)
        self.assertAlmostEqual(pos.qty, 0.5)
        self.assertAlmostEqual(realised, -2.5)  # (95-100)*0.5
        self.assertTrue(pos.scaled_out)

    def test_atr_pct_overrides_floor(self):
        # Calm coin (ATR=0.3%): floor clamps to 15%.
        cfg = SmartCloseConfig(
            hard_loss_pct_floor=20.0, hard_loss_pct_floor_min=15.0, hard_loss_pct_floor_max=30.0,
            scale_out_fraction=1.0,
            min_seconds_since_last_fill=0.0,
            recovery_window_sec=0.0,
            min_position_age_sec=0.0,
        )
        engine = SmartCloseEngine(cfg)
        pos = GridPosition(side="Buy", qty=4.0, entry_price=100.0,
                          opened_at=time.time() - 60, last_fill_at=time.time() - 1000)
        # 16% margin loss: above 15% (calm-coin floor) but below 20% default.
        # qty=4, entry=100, current=99.6 → unr = 4*(99.6-100) = -1.6 → 16% margin loss
        reason = engine.check_smart_close(pos, 99.6, 10.0, GridImbalance(), 4, atr_pct=0.3)
        self.assertEqual(reason, CloseReason.DRAWDOWN)
        # Same loss with volatile-coin ATR (2.5% → 30% floor) holds.
        engine2 = SmartCloseEngine(cfg)
        self.assertIsNone(
            engine2.check_smart_close(pos, 99.6, 10.0, GridImbalance(), 4, atr_pct=2.5)
        )


class TestImbalanceEmergencyBypass(unittest.TestCase):
    """Option A: severe imbalance + meaningful loss should bypass cooldown."""

    def _engine(self, **overrides):
        cfg = SmartCloseConfig(
            min_seconds_since_last_fill=180.0,    # cooldown active
            recovery_window_sec=0.0,
            min_position_age_sec=0.0,
            hard_loss_pct_floor=20.0,             # don't trip floor
            scale_out_fraction=1.0,
            imbalance_emergency_ratio=5.0,
            imbalance_emergency_min_fills=5,
            imbalance_emergency_min_loss_pct=5.0,
            imbalance_close_enabled=True,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return SmartCloseEngine(cfg)

    def test_emergency_bypass_fires_on_severe_imbalance(self):
        # 6 Buy fills, 1 Sell = 6:1 ratio (>=5). 8% loss (>=5). Cooldown active.
        # Position is on the dominant (Buy) side. Should fire even within cooldown.
        engine = self._engine()
        pos = GridPosition(
            side="Buy", qty=4.0, entry_price=100.0,
            opened_at=time.time() - 200, last_fill_at=time.time() - 1,  # cooldown active
        )
        imb = GridImbalance(
            buy_fills=6, sell_fills=1, last_side="Buy", consecutive_same_side=5,
        )
        # qty=4, current=99.8 → unr = 4 * -0.2 = -0.8 = 8% margin loss
        result = engine.check_smart_close(pos, 99.8, 10.0, imb, total_fills=7)
        self.assertEqual(result, CloseReason.GRID_IMBALANCE)

    def test_emergency_bypass_does_not_fire_below_loss_threshold(self):
        # Same imbalance but only 3% loss — below emergency threshold → defer to cooldown.
        engine = self._engine()
        pos = GridPosition(
            side="Buy", qty=4.0, entry_price=100.0,
            opened_at=time.time() - 200, last_fill_at=time.time() - 1,
        )
        imb = GridImbalance(buy_fills=6, sell_fills=1, last_side="Buy", consecutive_same_side=5)
        # 3% loss
        result = engine.check_smart_close(pos, 99.925, 10.0, imb, total_fills=7)
        self.assertIsNone(result)  # cooldown blocks regular imbalance check

    def test_emergency_bypass_does_not_fire_on_minority_side(self):
        # Position on the MINORITY side — opposite of imbalance — should hold.
        engine = self._engine()
        pos = GridPosition(
            side="Sell", qty=4.0, entry_price=100.0,
            opened_at=time.time() - 200, last_fill_at=time.time() - 1,
        )
        imb = GridImbalance(buy_fills=6, sell_fills=1, last_side="Buy", consecutive_same_side=5)
        # 8% loss but Sell side = minority
        result = engine.check_smart_close(pos, 100.2, 10.0, imb, total_fills=7)
        self.assertIsNone(result)

    def test_emergency_bypass_does_not_fire_below_min_fills(self):
        # Severe ratio but only 4 total fills — under min — defer.
        engine = self._engine(imbalance_emergency_min_fills=5)
        pos = GridPosition(
            side="Buy", qty=4.0, entry_price=100.0,
            opened_at=time.time() - 200, last_fill_at=time.time() - 1,
        )
        imb = GridImbalance(buy_fills=4, sell_fills=0, last_side="Buy", consecutive_same_side=4)
        result = engine.check_smart_close(pos, 99.8, 10.0, imb, total_fills=4)
        self.assertIsNone(result)


class TestDynamicTakeProfit(unittest.TestCase):
    def _engine(self, **overrides) -> SmartCloseEngine:
        cfg = SmartCloseConfig(
            tp_floor_pct=3.0,
            tp_min_age_full_target_min=15.0,
            tp_decay_to_zero_at_min=60.0,
            tp_dust_floor_usdt=0.05,
            tp_momentum_max_age_min=30.0,
            tp_momentum_velocity_pct_per_min=1.5,
            tp_momentum_extend_max_pct=5.0,
            tp_momentum_trailing_giveback_pct=0.5,
            tp_min_fills=2,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return SmartCloseEngine(cfg)

    def _pos(self, age_min: float, qty: float = 0.1) -> GridPosition:
        return GridPosition(
            side="Buy", qty=qty, entry_price=100.0,
            opened_at=time.time() - age_min * 60,
        )

    def test_no_close_when_negative_pnl(self):
        engine = self._engine()
        pos = self._pos(age_min=5)
        # 5 min old, -1% PnL → no close
        self.assertIsNone(engine.evaluate_take_profit(pos, 99.0, 10.0, total_pnl=-0.10, total_fills=4))

    def test_no_close_below_min_fills(self):
        engine = self._engine(tp_min_fills=3)
        pos = self._pos(age_min=5)
        # 4% PnL but only 2 fills → hold
        self.assertIsNone(engine.evaluate_take_profit(pos, 104.0, 10.0, total_pnl=0.40, total_fills=2))

    def test_closes_at_floor_when_young_no_momentum(self):
        # Young (<15 min), velocity ~ 0 (no price history): hits floor → close.
        engine = self._engine()
        pos = self._pos(age_min=5)
        result = engine.evaluate_take_profit(pos, 103.0, 10.0, total_pnl=0.30, total_fills=4)
        self.assertEqual(result, CloseReason.TARGET_HIT)

    def test_holds_below_floor_when_young(self):
        engine = self._engine()
        pos = self._pos(age_min=5)
        # 2% PnL — below 3% floor and not yet decayed.
        self.assertIsNone(engine.evaluate_take_profit(pos, 102.0, 10.0, total_pnl=0.20, total_fills=4))

    def test_time_decay_closes_after_30min_below_floor(self):
        engine = self._engine()
        pos = self._pos(age_min=37)  # past momentum window
        # Linear ramp 3% at 15min → 0% at 60min. At 37 min: target ≈ 1.53%.
        # 2% PnL exceeds the time-decayed target → close.
        result = engine.evaluate_take_profit(pos, 102.0, 10.0, total_pnl=0.20, total_fills=4)
        self.assertEqual(result, CloseReason.TARGET_HIT)

    def test_time_decay_holds_when_below_decayed_target(self):
        engine = self._engine()
        pos = self._pos(age_min=37)
        # 1% PnL is below the 1.53% decayed target.
        self.assertIsNone(engine.evaluate_take_profit(pos, 101.0, 10.0, total_pnl=0.10, total_fills=4))

    def test_dust_floor_holds_at_60min(self):
        engine = self._engine()
        pos = self._pos(age_min=65)  # past decay_to_zero
        # 0.4% PnL = $0.04 — below the $0.05 dust floor → hold.
        self.assertIsNone(engine.evaluate_take_profit(pos, 100.4, 10.0, total_pnl=0.04, total_fills=4))
        # $0.10 — above dust → close.
        self.assertEqual(
            engine.evaluate_take_profit(pos, 101.0, 10.0, total_pnl=0.10, total_fills=4),
            CloseReason.TARGET_HIT,
        )

    def test_momentum_extends_target_above_floor_when_velocity_strong(self):
        engine = self._engine()
        pos = self._pos(age_min=2)
        # Velocity helper window is the last 60s with strict t>last-60s, so
        # boundary points are excluded. Seed three points strictly inside.
        now = time.time()
        engine._price_history = [(now - 50, 100.0), (now - 25, 102.0), (now, 104.0)]
        # 4% PnL at 2 min, peak crosses floor, velocity ~4%/min → hold.
        self.assertIsNone(
            engine.evaluate_take_profit(pos, 104.0, 10.0, total_pnl=0.40, total_fills=4)
        )

    def test_momentum_caps_at_extend_max(self):
        engine = self._engine()
        pos = self._pos(age_min=3)
        now = time.time()
        engine._price_history = [(now - 60, 100.0), (now, 105.0)]
        # First call: peak hits 5% (the extend cap) → close.
        self.assertEqual(
            engine.evaluate_take_profit(pos, 105.0, 10.0, total_pnl=0.50, total_fills=4),
            CloseReason.TARGET_HIT,
        )

    def test_trailing_giveback_locks_in_after_peak(self):
        engine = self._engine()
        pos = self._pos(age_min=3)
        now = time.time()
        engine._price_history = [(now - 50, 100.0), (now - 25, 102.0), (now, 104.0)]
        # First tick: peak = 4%, hold (velocity strong).
        self.assertIsNone(
            engine.evaluate_take_profit(pos, 104.0, 10.0, total_pnl=0.40, total_fills=4)
        )
        # Second tick: PnL retreats to 3.4% — giveback 0.6% > 0.5% threshold → lock in.
        self.assertEqual(
            engine.evaluate_take_profit(pos, 103.4, 10.0, total_pnl=0.34, total_fills=4),
            CloseReason.TARGET_HIT,
        )

    def test_velocity_faded_closes_at_floor(self):
        engine = self._engine()
        pos = self._pos(age_min=10)
        now = time.time()
        # Flat price history → zero velocity.
        engine._price_history = [(now - 60, 103.0), (now - 30, 103.0), (now, 103.0)]
        # Peak just at floor (3%), velocity=0 → close at floor.
        self.assertEqual(
            engine.evaluate_take_profit(pos, 103.0, 10.0, total_pnl=0.30, total_fills=4),
            CloseReason.TARGET_HIT,
        )

    def test_peak_resets_on_negative_pnl(self):
        # If price moves against us and PnL turns negative, the peak for that
        # side should be cleared so a future bounce-back tracks fresh.
        engine = self._engine()
        pos = self._pos(age_min=3)
        engine.evaluate_take_profit(pos, 102.0, 10.0, total_pnl=0.20, total_fills=4)
        self.assertIn("Buy", engine._tp_peak_pct)
        # Now the position swings negative (e.g. price drops below entry).
        engine.evaluate_take_profit(pos, 99.0, 10.0, total_pnl=-0.10, total_fills=4)
        self.assertNotIn("Buy", engine._tp_peak_pct)

    def test_step_curve_matches_spec(self):
        # User-specified curve:
        #   < 10 min   → full 3% target
        #   10-20 min  → < 3% (steps to 2%, ramps down)
        #   < 30 min   → break even (target ~0)
        cfg = SmartCloseConfig(
            tp_floor_pct=3.0,
            tp_min_age_full_target_min=10.0,
            tp_decay_step_pct=2.0,
            tp_decay_to_zero_at_min=30.0,
            tp_momentum_max_age_min=30.0,
            tp_dust_floor_usdt=0.05,
            tp_min_fills=2,
        )

        # 9 min, 2.5% PnL → below 3% floor → hold.
        engine = SmartCloseEngine(cfg)
        self.assertIsNone(engine.evaluate_take_profit(
            self._pos(age_min=9), 102.5, 10.0, total_pnl=0.25, total_fills=4
        ))

        # 9 min, 3% PnL → meets floor → close.
        engine = SmartCloseEngine(cfg)
        self.assertEqual(
            engine.evaluate_take_profit(self._pos(age_min=9), 103.0, 10.0,
                                         total_pnl=0.30, total_fills=4),
            CloseReason.TARGET_HIT,
        )

        # 15 min, 1.6% PnL → curve target ≈ 2% * (1 - 5/20) = 1.5% → close.
        engine = SmartCloseEngine(cfg)
        self.assertEqual(
            engine.evaluate_take_profit(self._pos(age_min=15), 101.6, 10.0,
                                         total_pnl=0.16, total_fills=4),
            CloseReason.TARGET_HIT,
        )

        # 20 min, 1.1% PnL → curve target ≈ 2% * (1 - 10/20) = 1.0% → close.
        engine = SmartCloseEngine(cfg)
        self.assertEqual(
            engine.evaluate_take_profit(self._pos(age_min=20), 101.1, 10.0,
                                         total_pnl=0.11, total_fills=4),
            CloseReason.TARGET_HIT,
        )

        # 29 min, +$0.10 (very small positive) → past decay point but above
        # dust floor → close (any positive).
        engine = SmartCloseEngine(cfg)
        self.assertEqual(
            engine.evaluate_take_profit(self._pos(age_min=29), 100.1, 10.0,
                                         total_pnl=0.10, total_fills=4),
            CloseReason.TARGET_HIT,
        )

        # 35 min, +$0.02 → past decay AND below dust → still hold.
        engine = SmartCloseEngine(cfg)
        self.assertIsNone(engine.evaluate_take_profit(
            self._pos(age_min=35), 100.0, 10.0, total_pnl=0.02, total_fills=4
        ))

    def test_reset_tp_peak_clears_state(self):
        engine = self._engine()
        pos = self._pos(age_min=3)
        engine.evaluate_take_profit(pos, 102.0, 10.0, total_pnl=0.20, total_fills=4)
        self.assertIn("Buy", engine._tp_peak_pct)
        engine.reset_tp_peak()
        self.assertEqual(engine._tp_peak_pct, {})


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
