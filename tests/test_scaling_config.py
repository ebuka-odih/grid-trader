import unittest

from multi_grid_manager import determine_deployment_pick_count


class ScalingConfigTests(unittest.TestCase):
    def test_pick_count_uses_free_slots_when_cycle_cap_is_zero_or_negative(self):
        self.assertEqual(determine_deployment_pick_count(12, 20, 0), 12)
        self.assertEqual(determine_deployment_pick_count(12, 20, -1), 12)

    def test_pick_count_is_limited_by_available_and_cycle_cap(self):
        self.assertEqual(determine_deployment_pick_count(20, 18, 15), 15)
        self.assertEqual(determine_deployment_pick_count(20, 8, 15), 8)
        self.assertEqual(determine_deployment_pick_count(4, 20, 15), 4)

    def test_pick_count_never_goes_negative(self):
        self.assertEqual(determine_deployment_pick_count(0, 10, 15), 0)
        self.assertEqual(determine_deployment_pick_count(10, 0, 15), 0)

    def test_pick_count_can_be_driven_by_remaining_wallet_exposure(self):
        # 1.2% used, 80% target, 2% per grid => about 40 grids needed,
        # then bounded by free slots / available candidates / cycle cap.
        self.assertEqual(
            determine_deployment_pick_count(
                50,
                100,
                45,
                current_total_exposure_pct=1.2,
                target_wallet_exposure_pct=80.0,
                per_grid_exposure_pct=2.0,
            ),
            40,
        )
        self.assertEqual(
            determine_deployment_pick_count(
                50,
                100,
                45,
                current_total_exposure_pct=79.0,
                target_wallet_exposure_pct=80.0,
                per_grid_exposure_pct=2.0,
            ),
            1,
        )
        self.assertEqual(
            determine_deployment_pick_count(
                50,
                100,
                45,
                current_total_exposure_pct=80.0,
                target_wallet_exposure_pct=80.0,
                per_grid_exposure_pct=2.0,
            ),
            0,
        )


if __name__ == '__main__':
    unittest.main()
