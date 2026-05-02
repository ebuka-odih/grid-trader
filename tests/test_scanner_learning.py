import tempfile
import time
import unittest

from coin_scanner import CoinScore
from scanner_learning import ScannerLearning, TokenLearningState


def coin(symbol="FAST/USDT:USDT", score=0.70):
    return CoinScore(
        symbol=symbol,
        price=1.0,
        high_24h=1.05,
        low_24h=0.95,
        volume_24h_usdt=25_000_000,
        atr_pct=1.5,
        range_pct=10.0,
        mean_reversion_score=0.7,
        grid_score=score,
        suggested_upper=1.03,
        suggested_lower=0.97,
        suggested_grids=8,
        suggested_leverage=50,
    )


class ScannerLearningTests(unittest.TestCase):
    def test_recent_failures_penalize_candidate_without_permanent_ban(self):
        learning = ScannerLearning(now_fn=lambda: 1_000.0, state_path=None)
        learning.record_trade("FAST/USDT:USDT", total_pnl=-0.2, close_reason="timeout", duration_seconds=120)
        learning.record_trade("FAST/USDT:USDT", total_pnl=-0.3, close_reason="drawdown", duration_seconds=80)

        adjusted = learning.score_candidate(coin())

        self.assertLess(adjusted.final_score, adjusted.market_score)
        self.assertGreater(adjusted.final_score, 0.0)
        self.assertFalse(adjusted.cooldown_active)

    def test_repeated_bad_results_trigger_temporary_cooldown(self):
        learning = ScannerLearning(now_fn=lambda: 1_000.0, state_path=None)
        for reason in ["timeout", "drawdown", "spike_close"]:
            learning.record_trade("FAST/USDT:USDT", total_pnl=-0.1, close_reason=reason, duration_seconds=60)

        adjusted = learning.score_candidate(coin())

        self.assertTrue(adjusted.cooldown_active)
        self.assertEqual(adjusted.final_score, 0.0)
        self.assertIn("cooldown", adjusted.skip_reason)

    def test_cooldown_expires_and_token_can_be_scanned_again(self):
        now = [1_000.0]
        learning = ScannerLearning(now_fn=lambda: now[0], cooldown_seconds=60, state_path=None)
        for _ in range(3):
            learning.record_trade("FAST/USDT:USDT", total_pnl=-0.1, close_reason="timeout", duration_seconds=60)
        now[0] = 1_061.0

        adjusted = learning.score_candidate(coin())

        self.assertFalse(adjusted.cooldown_active)
        self.assertGreater(adjusted.final_score, 0.0)

    def test_fast_profitable_results_boost_candidate(self):
        learning = ScannerLearning(now_fn=lambda: 1_000.0, state_path=None)
        learning.record_trade("FAST/USDT:USDT", total_pnl=0.3, close_reason="target_hit", duration_seconds=45)
        learning.record_trade("FAST/USDT:USDT", total_pnl=0.2, close_reason="target_hit", duration_seconds=50)

        adjusted = learning.score_candidate(coin(score=0.5))

        self.assertGreater(adjusted.final_score, adjusted.market_score)
        self.assertGreater(adjusted.learning_score, 0.0)
    def test_quality_gate_blocks_negative_expectancy_low_sharpe_symbols(self):
        learning = ScannerLearning(now_fn=lambda: 1_000.0, failure_threshold=99, state_path=None)
        for pnl in [0.01, 0.01, 0.01, -0.08, -0.08]:
            learning.record_trade(
                "BAD/USDT:USDT",
                total_pnl=pnl,
                close_reason="target_hit" if pnl > 0 else "timeout",
                duration_seconds=60,
            )

        adjusted = learning.score_candidate(coin(symbol="BAD/USDT:USDT", score=0.95))

        self.assertTrue(adjusted.cooldown_active)
        self.assertEqual(adjusted.final_score, 0.0)
        self.assertIn("quality gate", adjusted.skip_reason)
        self.assertIn("expectancy", adjusted.skip_reason)

    def test_quality_gate_allows_high_win_positive_expectancy_symbols(self):
        learning = ScannerLearning(now_fn=lambda: 1_000.0, state_path=None)
        for pnl in [0.04, 0.03, 0.03, 0.02, -0.05]:
            learning.record_trade(
                "GOOD/USDT:USDT",
                total_pnl=pnl,
                close_reason="target_hit" if pnl > 0 else "timeout",
                duration_seconds=55,
            )

        adjusted = learning.score_candidate(coin(symbol="GOOD/USDT:USDT", score=0.5))

        self.assertFalse(adjusted.cooldown_active)
        self.assertGreater(adjusted.final_score, adjusted.market_score)


if __name__ == "__main__":
    unittest.main()
