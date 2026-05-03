"""Unit test for WalletTracker.restore_realized_pnl."""
import unittest
from wallet_tracker import WalletTracker


class WalletRestoreTests(unittest.TestCase):
    def test_restore_seeds_balance(self):
        w = WalletTracker(initial_balance=100.0)
        w.restore_realized_pnl(3.6215)
        self.assertAlmostEqual(w.get_balance(), 103.6215, places=4)
        self.assertEqual(w.initial_balance, 100.0)
        # PnL bucket reflects the replay so stats stay consistent.
        self.assertAlmostEqual(w._realized_pnl_total, 3.6215, places=4)

    def test_restore_negative_pnl(self):
        w = WalletTracker(initial_balance=100.0)
        w.restore_realized_pnl(-12.5)
        self.assertAlmostEqual(w.get_balance(), 87.5)

    def test_restore_zero_is_noop(self):
        w = WalletTracker(initial_balance=100.0)
        w.restore_realized_pnl(0.0)
        self.assertEqual(w.get_balance(), 100.0)
        self.assertEqual(w._realized_pnl_total, 0.0)
        self.assertEqual(w._realized_pnls, [])

    def test_restore_then_live_pnl_accumulates(self):
        w = WalletTracker(initial_balance=100.0)
        w.restore_realized_pnl(3.0)
        w.add_realized_pnl(2.0)
        self.assertAlmostEqual(w.get_balance(), 105.0)


if __name__ == "__main__":
    unittest.main()
