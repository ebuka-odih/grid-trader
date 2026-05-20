import unittest

from coin_scanner import CoinScore
from multi_grid_manager import (
    build_scanner_candidate_decision,
    classify_grid_style,
    prefilter_scanner_candidates_for_deploy,
    rejection_cooldown_key,
)
from decision_supervisor import DecisionSupervisor


def make_coin(**overrides):
    data = {
        "symbol": "TEST/USDT:USDT",
        "price": 100.0,
        "high_24h": 106.0,
        "low_24h": 94.0,
        "volume_24h_usdt": 75_000_000,
        "atr_pct": 1.1,
        "range_pct": 5.0,
        "mean_reversion_score": 0.55,
        "grid_score": 0.42,
        "suggested_upper": 103.0,
        "suggested_lower": 97.0,
        "suggested_grids": 12,
        "suggested_leverage": 10,
        "trend_direction": "neutral",
        "market_regime": "ranging",
        "entry_quality_score": 0.30,
        "range_position": 0.5,
        "vwap_distance_pct": 0.0,
        "pullback_depth_pct": 0.2,
        "slope_score": 0.0,
        "mtf_alignment_score": 0.0,
        "entry_shape_template": "atr_box",
        "entry_shape_spacing": "balanced",
    }
    data.update(overrides)
    return CoinScore(**data)


class StyleAwareSelectionTests(unittest.TestCase):
    def test_low_entry_quality_candidate_is_shaped_not_prefilter_rejected(self):
        coin = make_coin(entry_quality_score=0.12, grid_score=0.31, trend_direction="long", market_regime="trending_up")

        deployable = prefilter_scanner_candidates_for_deploy(
            [coin],
            token_profile_by_symbol={coin.symbol: {"leverage": 10, "max_leverage": 10, "min_entry_quality": 0.35}},
            wallet_balance=500.0,
            decision_supervisor=DecisionSupervisor(),
            active_symbols=[],
            max_active_per_symbol=1,
        )

        self.assertEqual([c.symbol for c in deployable], [coin.symbol])
        self.assertEqual(getattr(coin, "grid_style"), "micro_scalp")
        self.assertLessEqual(coin.suggested_grids, 10)

    def test_trending_coin_gets_directional_pullback_style(self):
        coin = make_coin(
            trend_direction="long",
            market_regime="trending_up",
            entry_quality_score=0.64,
            pullback_depth_pct=0.8,
            range_position=0.34,
            vwap_distance_pct=-0.7,
        )

        style = classify_grid_style(coin)
        decision = build_scanner_candidate_decision(coin, token_profile={"leverage": 10, "max_leverage": 10}, wallet_balance=500.0)

        self.assertEqual(style, "long_pullback_grid")
        self.assertEqual(decision.direction, "long")
        self.assertEqual(getattr(coin, "grid_style"), "long_pullback_grid")
        self.assertEqual(getattr(coin, "entry_shape_spacing"), "buy_weighted")

    def test_rejection_cooldown_key_is_style_scoped_for_quality_but_symbol_scoped_for_safety(self):
        self.assertEqual(
            rejection_cooldown_key("BTC/USDT:USDT", "neutral_scalp", "supervisor", ["Entry quality below minimum"]),
            "BTC/USDT:USDT|neutral_scalp|supervisor",
        )
        self.assertEqual(
            rejection_cooldown_key("BTC/USDT:USDT", "long_pullback_grid", "deploy", ["precision invalid"]),
            "BTC/USDT:USDT|safety|deploy",
        )


if __name__ == "__main__":
    unittest.main()
