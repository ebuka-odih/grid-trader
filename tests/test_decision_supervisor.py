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
            token_profile={"leverage": 50, "max_leverage": 100, "min_confidence": 0.6},
            active_symbols=set(),
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.reasons, [])

    def test_low_confidence_decision_is_rejected(self):
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(confidence=0.2),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 100, "min_confidence": 0.6},
            active_symbols=set(),
        )

        self.assertFalse(result.approved)
        self.assertIn("confidence", result.reasons[0].lower())

    def test_decision_with_invalid_grid_range_is_rejected(self):
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(lower=100.5, upper=101.0),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 100, "min_confidence": 0.6},
            active_symbols=set(),
        )

        self.assertFalse(result.approved)
        self.assertTrue(any("current price" in reason.lower() for reason in result.reasons))

    def test_duplicate_active_symbol_is_rejected_by_default(self):
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 100, "min_confidence": 0.6},
            active_symbols={"AAVE/USDT:USDT"},
        )

        self.assertFalse(result.approved)
        self.assertTrue(any("already active" in reason.lower() for reason in result.reasons))

    def test_duplicate_active_symbol_allowed_until_capacity_when_configured(self):
        result = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 100, "min_confidence": 0.6},
            active_symbols=["AAVE/USDT:USDT", "DOGE/USDT:USDT"],
            max_active_per_symbol=4,
        )

        self.assertTrue(result.approved)

        full = DecisionSupervisor().review_pre_trade_decision(
            decision=decision(),
            coin_score=coin(),
            token_profile={"leverage": 50, "max_leverage": 100, "min_confidence": 0.6},
            active_symbols=["AAVE/USDT:USDT"] * 4,
            max_active_per_symbol=4,
        )
        self.assertFalse(full.approved)
        self.assertTrue(any("capacity" in reason.lower() for reason in full.reasons))


if __name__ == "__main__":
    unittest.main()
