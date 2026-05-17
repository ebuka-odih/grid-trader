import unittest

from coin_scanner import CoinScore
from decision_supervisor import DecisionSupervisor
from trading_agent import PreTradeDecision


def coin():
    return CoinScore(
        symbol="AAVE/USDT:USDT",
        price=100.0,
        high_24h=103.0,
        low_24h=97.0,
        volume_24h_usdt=50_000_000,
        atr_pct=0.6,
        range_pct=6.0,
        mean_reversion_score=0.9,
        grid_score=0.8,
        suggested_upper=103.0,
        suggested_lower=97.0,
        suggested_grids=10,
        suggested_leverage=50,
        entry_quality_score=0.82,
        entry_shape_template="trend_pullback",
        entry_shape_spacing="buy_weighted",
        entry_buy_density_bias=0.72,
        entry_sell_density_bias=0.38,
        pullback_depth_pct=0.9,
    )


def decision(**overrides):
    data = {
        "symbol": "AAVE/USDT:USDT",
        "direction": "long",
        "confidence": 0.75,
        "upper": 103.0,
        "lower": 97.0,
        "num_grids": 10,
        "leverage": 50,
        "reasoning": "ranging market with upward bias",
        "market_regime": "trending_up",
        "narrative": "good liquidity and controlled volatility",
    }
    data.update(overrides)
    return PreTradeDecision(**data)


class DecisionSupervisorTests(unittest.TestCase):
    def test_valid_pre_trade_decision_is_approved(self):
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 50, "min_confidence": 0.6},
            active_symbols=set(),
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.reasons, [])

    def test_low_confidence_decision_is_rejected(self):
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(confidence=0.2),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 50, "min_confidence": 0.6},
            active_symbols=set(),
        )

        self.assertFalse(result.approved)
        self.assertIn("confidence", result.reasons[0].lower())

    def test_decision_with_invalid_grid_range_is_rejected(self):
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(lower=100.5, upper=101.0),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 50, "min_confidence": 0.6},
            active_symbols=set(),
        )

        self.assertFalse(result.approved)
        self.assertTrue(any("current price" in reason.lower() for reason in result.reasons))

    def test_duplicate_active_symbol_is_rejected_by_default(self):
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 50, "min_confidence": 0.6},
            active_symbols={"AAVE/USDT:USDT"},
        )

        self.assertFalse(result.approved)
        self.assertTrue(any("already active" in reason.lower() for reason in result.reasons))

    def test_duplicate_active_symbol_allowed_until_capacity_when_configured(self):
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 50, "min_confidence": 0.6},
            active_symbols=["AAVE/USDT:USDT", "DOGE/USDT:USDT"],
            max_active_per_symbol=4,
        )

        self.assertTrue(result.approved)

        full = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 50, "min_confidence": 0.6},
            active_symbols=["AAVE/USDT:USDT"] * 4,
            max_active_per_symbol=4,
        )
        self.assertFalse(full.approved)
        self.assertTrue(any("capacity" in reason.lower() for reason in full.reasons))

    def test_low_entry_quality_is_rejected(self):
        weak_coin = coin()
        weak_coin.entry_quality_score = 0.21
        weak_coin.entry_shape_template = "atr_box"
        weak_coin.entry_shape_spacing = "balanced"

        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(),
            coin_score=weak_coin,
            token_profile={
                "leverage": 50,
                "max_leverage": 50,
                "min_confidence": 0.6,
                "min_entry_quality": 0.35,
            },
            active_symbols=set(),
        )

        self.assertFalse(result.approved)
        self.assertTrue(any("entry quality" in reason.lower() for reason in result.reasons))

    def test_borderline_confidence_is_auto_raised_for_strong_entry(self):
        draft = decision(confidence=0.57)
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=draft,
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 50, "min_confidence": 0.6},
            active_symbols=set(),
        )

        self.assertTrue(result.approved)
        self.assertAlmostEqual(draft.confidence, 0.6, places=6)
        self.assertTrue(any("auto-raised" in warning.lower() for warning in result.warnings))

    def test_borderline_grid_width_is_auto_tightened(self):
        draft = decision(lower=92.0, upper=108.2)
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=draft,
            coin_score=coin(),
            token_profile={
                "leverage": 50,
                "max_leverage": 50,
                "min_confidence": 0.6,
                "max_grid_width_pct": 15.0,
            },
            active_symbols=set(),
        )

        self.assertTrue(result.approved)
        self.assertAlmostEqual(draft.upper - draft.lower, 15.0, places=5)
        self.assertTrue(any("auto-tightened" in warning.lower() for warning in result.warnings))

    def test_borderline_entry_quality_can_pass_when_pullback_aligns(self):
        aligned_coin = coin()
        aligned_coin.entry_quality_score = 0.30
        aligned_coin.pullback_depth_pct = 0.8
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(direction="long", market_regime="trending_up"),
            coin_score=aligned_coin,
            token_profile={
                "leverage": 50,
                "max_leverage": 50,
                "min_confidence": 0.6,
                "min_entry_quality": 0.35,
            },
            active_symbols=set(),
        )

        self.assertTrue(result.approved)
        self.assertTrue(any("borderline entry quality" in warning.lower() for warning in result.warnings))

    def test_strong_entry_shape_emits_warning_context(self):
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 50, "min_confidence": 0.6},
            active_symbols=set(),
        )

        self.assertTrue(result.approved)
        self.assertTrue(any("strong entry shape" in warning.lower() for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
