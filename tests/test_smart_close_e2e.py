"""
End-to-end behavioural test for the new smart-close protections.

Verifies, against the live SmartCloseEngine + GridPosition pipeline:
  1. A trigger condition that would have fired in the old code is deferred
     by the post-fill cooldown.
  2. A losing position that bounces back ≥ recovery_partial_pct exits the
     recovery window without realising the loss.
  3. A position that fails to recover within recovery_window_sec eventually
     closes.
  4. A loss past hard_loss_pct_floor closes immediately, bypassing both
     cooldown and recovery.
"""

import time
import sys

from grid_core import (
    GridPosition, GridImbalance, FillEvent,
    SmartCloseEngine, SmartCloseConfig, CloseReason,
    process_fill,
)


def case_post_fill_cooldown():
    cfg = SmartCloseConfig(
        time_decay_hours=1.0, time_decay_min_loss_pct=0.5,
        min_seconds_since_last_fill=180.0,
        recovery_window_sec=0.0,
        hard_loss_pct_floor=20.0,
    )
    engine = SmartCloseEngine(cfg)
    pos = GridPosition()
    imb = GridImbalance()
    # Open a position 2 hours ago, then add a fresh fill 10s ago.
    process_fill(FillEvent(0, "Buy", 100.0, 0.1, time.time() - 7200), pos, imb)
    process_fill(FillEvent(1, "Buy", 100.0, 0.1, time.time() - 10), pos, imb)
    reason = engine.check_smart_close(pos, 98.0, 10.0, imb, 2)
    assert reason is None, f"cooldown should defer; got {reason}"

    # Move the last fill outside cooldown.
    pos.last_fill_at = time.time() - 600
    reason = engine.check_smart_close(pos, 98.0, 10.0, imb, 2)
    assert reason == CloseReason.TIME_DECAY, f"expected TIME_DECAY after cooldown; got {reason}"
    print("✅ post-fill cooldown")


def case_recovery_aborts_close():
    cfg = SmartCloseConfig(
        time_decay_hours=1.0, time_decay_min_loss_pct=0.5,
        min_seconds_since_last_fill=0.0,
        recovery_window_sec=300.0,
        recovery_partial_pct=30.0,
        hard_loss_pct_floor=20.0,
    )
    engine = SmartCloseEngine(cfg)
    pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0,
                      opened_at=time.time() - 7200, last_fill_at=time.time() - 1000)
    imb = GridImbalance()

    # First check at -3% — opens recovery window, defers close.
    assert engine.check_smart_close(pos, 97.0, 10.0, imb, 5) is None
    assert "Buy" in engine._recovery_state

    # Price recovers to -1% (recovered 66% of the worst loss) → abort.
    assert engine.check_smart_close(pos, 99.0, 10.0, imb, 5) is None
    assert "Buy" not in engine._recovery_state, "recovery should have cleared"
    print("✅ recovery window aborts close on partial bounce")


def case_recovery_window_expires():
    cfg = SmartCloseConfig(
        time_decay_hours=1.0, time_decay_min_loss_pct=0.5,
        min_seconds_since_last_fill=0.0,
        recovery_window_sec=10.0,
        recovery_partial_pct=30.0,
        hard_loss_pct_floor=20.0,
    )
    engine = SmartCloseEngine(cfg)
    pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0,
                      opened_at=time.time() - 7200, last_fill_at=time.time() - 1000)
    imb = GridImbalance()

    assert engine.check_smart_close(pos, 97.0, 10.0, imb, 5) is None  # opens window
    # Roll the window back past expiry.
    engine._recovery_state["Buy"]["start_ts"] = time.time() - 30
    reason = engine.check_smart_close(pos, 97.0, 10.0, imb, 5)
    assert reason == CloseReason.TIME_DECAY, f"expected TIME_DECAY after expiry; got {reason}"
    print("✅ recovery window expires → close fires")


def case_hard_floor_bypass():
    cfg = SmartCloseConfig(
        min_seconds_since_last_fill=600.0,  # cooldown active
        recovery_window_sec=600.0,          # recovery would normally defer
        hard_loss_pct_floor=4.0,
    )
    engine = SmartCloseEngine(cfg)
    pos = GridPosition(side="Buy", qty=0.1, entry_price=100.0,
                      opened_at=time.time() - 60, last_fill_at=time.time() - 1)
    imb = GridImbalance()
    # 5% loss > 4% hard floor → must close immediately.
    reason = engine.check_smart_close(pos, 95.0, 10.0, imb, 1)
    assert reason == CloseReason.DRAWDOWN, f"expected DRAWDOWN at hard floor; got {reason}"
    assert "Buy" not in engine._recovery_state
    print("✅ hard loss floor bypasses cooldown + recovery")


def case_process_fill_records_last_fill_at():
    pos = GridPosition()
    imb = GridImbalance()
    ts = time.time() - 50
    process_fill(FillEvent(0, "Buy", 100.0, 0.1, ts), pos, imb)
    assert pos.last_fill_at == ts, f"last_fill_at not recorded; got {pos.last_fill_at}"
    # A subsequent partial close should keep the position open and update last_fill_at.
    ts2 = time.time()
    process_fill(FillEvent(1, "Sell", 101.0, 0.05, ts2), pos, imb)
    assert not pos.is_flat
    assert pos.last_fill_at == ts2
    # Full close → last_fill_at clears.
    process_fill(FillEvent(2, "Sell", 101.0, 0.05, time.time()), pos, imb)
    assert pos.is_flat
    assert pos.last_fill_at == 0.0
    print("✅ process_fill records last_fill_at and clears on flat")


if __name__ == "__main__":
    case_post_fill_cooldown()
    case_recovery_aborts_close()
    case_recovery_window_expires()
    case_hard_floor_bypass()
    case_process_fill_records_last_fill_at()
    print("\nAll behavioural checks passed.")
