import unittest

from coin_scanner import CoinScore
from multi_grid_manager import build_filter_decisions
from trade_close_optimizer import GridStatus, TradeCloseOptimizer


def coin(symbol="AAVE/USDT:USDT", *, score=0.82, atr=0.8, range_pct=5.0, mean_reversion=0.8, price=100.0):
    return CoinScore(
        symbol=symbol,
        price=price,
        high_24h=price * (1 + range_pct / 200),
        low_24h=price * (1 - range_pct / 200),
        volume_24h_usdt=50_000_000,
        atr_pct=atr,
        range_pct=range_pct,
        mean_reversion_score=mean_reversion,
        grid_score=score,
        suggested_upper=price * 1.025,
        suggested_lower=price * 0.975,
        suggested_grids=14,
        suggested_leverage=50,
    )


class FilterStrategyAndProfitLockTests(unittest.TestCase):
    def test_filter_decisions_pick_scanner_ranked_coins_without_llm(self):
        picks = build_filter_decisions(
            available=[
                coin("LOW/USDT:USDT", score=0.35),
                coin("BEST/USDT:USDT", score=0.91),
                coin("MID/USDT:USDT", score=0.72),
            ],
            num_to_pick=2,
            get_token_profile=lambda symbol: {"leverage": 50, "max_leverage": 100, "min_confidence": 0.25},
            wallet_balance=100.0,
            max_trade_exposure_pct=2.0,
        )

        self.assertEqual([p.symbol for p in picks], ["BEST/USDT:USDT", "MID/USDT:USDT"])
        self.assertTrue(all(p.reasoning.startswith("Deterministic scanner/filter") for p in picks))
        self.assertTrue(all(p.market_regime == "ranging" for p in picks))

    def test_filter_decisions_reject_poor_mean_reversion_or_excessive_atr(self):
        picks = build_filter_decisions(
            available=[
                coin("BADTREND/USDT:USDT", score=0.95, mean_reversion=0.05),
                coin("TOOWILD/USDT:USDT", score=0.9, atr=9.0),
                coin("GOOD/USDT:USDT", score=0.75, atr=1.2, mean_reversion=0.7),
            ],
            num_to_pick=3,
            get_token_profile=lambda symbol: {"leverage": 50, "max_leverage": 100, "min_confidence": 0.25},
            wallet_balance=100.0,
            max_trade_exposure_pct=2.0,
        )

        self.assertEqual([p.symbol for p in picks], ["GOOD/USDT:USDT"])

    def test_filter_decisions_reject_negative_historical_expectancy(self):
        picks = build_filter_decisions(
            available=[
                coin("LOSER/USDT:USDT", score=0.95, mean_reversion=0.9),
                coin("WINNER/USDT:USDT", score=0.75, mean_reversion=0.7),
            ],
            num_to_pick=2,
            get_token_profile=lambda symbol: {"leverage": 50, "max_leverage": 100, "min_confidence": 0.25},
            wallet_balance=100.0,
            max_trade_exposure_pct=2.0,
            symbol_performance={
                "LOSER/USDT:USDT": {"closed_trades": 4, "avg_pnl": -0.025, "win_rate": 0.0},
                "WINNER/USDT:USDT": {"closed_trades": 4, "avg_pnl": 0.01, "win_rate": 75.0},
            },
        )

        self.assertEqual([p.symbol for p in picks], ["WINNER/USDT:USDT"])

    def test_profit_lock_closes_only_when_net_pnl_covers_fees_and_buffer(self):
        optimizer = TradeCloseOptimizer(target_pnl_pct_low=2.0, min_net_profit_usdt=0.05, fee_buffer_multiplier=1.25)
        status = GridStatus(
            fills=4,
            realized_pnl=0.025,
            unrealized_pnl=0.02,
            allocated_margin=2.0,
            order_size_usdt=0.18,
            avg_entry_price=100.0,
            current_bid=100.0,
            current_ask=100.02,
            current_mark=100.01,
            direction="neutral",
            grid_levels=10,
            filled_levels=4,
            age_seconds=300,
        )

        decision = optimizer.should_close(status)

        self.assertFalse(decision.should_close)
        self.assertIn("net pnl", decision.reason.lower())

        status.realized_pnl = 0.12
        status.unrealized_pnl = 0.02
        decision = optimizer.should_close(status)

        self.assertTrue(decision.should_close)
        self.assertGreater(decision.net_pnl, 0.05)
        self.assertIn("net target", decision.reason.lower())


if __name__ == "__main__":
    unittest.main()
