import unittest

from multi_grid_manager import (
    MultiGridManager,
    NO_FILL_GRID_TIMEOUT_SECONDS,
    LOSING_STAGNANT_TIMEOUT_SECONDS,
    STAGNANT_GRID_TIMEOUT_SECONDS,
)


class GridStagnationHygieneTests(unittest.TestCase):
    def setUp(self):
        self.manager = MultiGridManager.__new__(MultiGridManager)

    def test_closes_grid_with_no_fills_after_timeout(self):
        reason = self.manager._stagnation_close_reason(
            age_seconds=NO_FILL_GRID_TIMEOUT_SECONDS + 1,
            fills=0,
            total_pnl=0.0,
            seconds_since_progress=10,
        )
        self.assertEqual(reason, "no_fills_timeout")

    def test_closes_losing_grid_with_no_progress(self):
        reason = self.manager._stagnation_close_reason(
            age_seconds=600,
            fills=4,
            total_pnl=-0.25,
            seconds_since_progress=LOSING_STAGNANT_TIMEOUT_SECONDS + 1,
        )
        self.assertEqual(reason, "losing_stagnant")

    def test_closes_any_grid_with_extended_no_progress(self):
        reason = self.manager._stagnation_close_reason(
            age_seconds=STAGNANT_GRID_TIMEOUT_SECONDS + 1,
            fills=2,
            total_pnl=0.0,
            seconds_since_progress=STAGNANT_GRID_TIMEOUT_SECONDS + 1,
        )
        self.assertEqual(reason, "stagnant_no_progress")

    def test_keeps_active_grid_when_progress_recent(self):
        reason = self.manager._stagnation_close_reason(
            age_seconds=300,
            fills=2,
            total_pnl=-0.1,
            seconds_since_progress=30,
        )
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
