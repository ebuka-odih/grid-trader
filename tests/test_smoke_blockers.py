import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from coin_scanner import CoinScore
from improvement_loop import ImprovementLoop
from portfolio_risk_monitor import PortfolioRiskMonitor
import multi_grid_manager
from multi_grid_manager import normalize_grid_density, symbol_grid_count, symbol_has_grid_capacity


class SmokeBlockerRegressionTests(unittest.TestCase):
    def test_symbol_capacity_enforces_one_dense_grid_per_symbol(self):
        """Exchange-style mode should use one dense grid engine per symbol."""
        active_symbols = [
            "DOGE/USDT:USDT",
            "HYPE/USDT:USDT",
        ]

        self.assertEqual(symbol_grid_count(active_symbols, "DOGE/USDT:USDT"), 1)
        # With max_per_symbol=1, already-active DOGE has no capacity left
        self.assertFalse(symbol_has_grid_capacity(active_symbols, "DOGE/USDT:USDT", max_per_symbol=1))
        self.assertTrue(symbol_has_grid_capacity(active_symbols, "ZEC/USDT:USDT", max_per_symbol=1))

    def test_symbol_capacity_allows_multi_grid_per_symbol(self):
        """When max_per_symbol=3, up to 3 grids fit on the same symbol."""
        active_symbols = [
            "DOGE/USDT:USDT",
        ]
        self.assertEqual(symbol_grid_count(active_symbols, "DOGE/USDT:USDT"), 1)
        self.assertTrue(symbol_has_grid_capacity(active_symbols, "DOGE/USDT:USDT", max_per_symbol=3))
        
        # With 3 DOGE slots, capacity exhausted
        active_3 = ["DOGE/USDT:USDT", "DOGE/USDT:USDT", "DOGE/USDT:USDT"]
        self.assertFalse(symbol_has_grid_capacity(active_3, "DOGE/USDT:USDT", max_per_symbol=3))

    def test_grid_density_is_normalized_to_scalping_range(self):
        """Decision/scanner grid counts should be clamped to 10–20 internal levels."""
        self.assertEqual(normalize_grid_density(5), 6)
        self.assertEqual(normalize_grid_density(10), 10)
        self.assertEqual(normalize_grid_density(15), 10)
        self.assertEqual(normalize_grid_density(30), 10)

    def test_improvement_loop_migrates_existing_grid_cycles_table(self):
        """Existing DBs from v1 must be altered instead of breaking v2 journaling."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "legacy_trades.db")
            con = sqlite3.connect(db_file)
            con.execute(
                """
                CREATE TABLE grid_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    grid_id VARCHAR(100) UNIQUE,
                    symbol VARCHAR(50),
                    upper_price FLOAT,
                    lower_price FLOAT,
                    num_grids INTEGER,
                    leverage INTEGER,
                    total_pnl FLOAT,
                    realized_pnl FLOAT DEFAULT 0.0,
                    unrealized_pnl_at_close FLOAT DEFAULT 0.0,
                    fills_count INTEGER DEFAULT 0,
                    duration_seconds FLOAT DEFAULT 0.0,
                    started_at DATETIME,
                    closed_at DATETIME,
                    close_reason VARCHAR(50),
                    was_profitable BOOLEAN DEFAULT 0
                )
                """
            )
            con.commit()
            con.close()

            journal = ImprovementLoop(f"sqlite:///{db_file}")
            journal.record_cycle_start(
                grid_id="grid_1",
                symbol="ZEC/USDT:USDT",
                upper=358.0,
                lower=351.0,
                num_grids=8,
                leverage=5,
            )
            journal.record_cycle_close(
                grid_id="grid_1",
                total_pnl=0.25,
                realized_pnl=0.2,
                unrealized_pnl=0.05,
                fills=2,
                duration=12.0,
                close_reason="target_hit",
                wallet_balance=100.25,
                wallet_exposure_pct=5.0,
                direction="neutral",
                adjusted_leverage=5,
                adjusted_order_size=1.0,
            )

            con = sqlite3.connect(db_file)
            cols = {row[1] for row in con.execute("PRAGMA table_info(grid_cycles)")}
            row = con.execute(
                "SELECT wallet_balance_at_close, wallet_exposure_pct_at_close, direction, adjusted_leverage, adjusted_order_size "
                "FROM grid_cycles WHERE grid_id='grid_1'"
            ).fetchone()
            con.close()

            self.assertIn("wallet_balance_at_close", cols)
            self.assertEqual(row, (100.25, 5.0, "neutral", 5, 1.0))

    def test_cycle_start_records_adjusted_runtime_metadata_for_open_grids(self):
        """Open dry-run grids should carry direction/adjusted sizing before they close."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "runtime_metadata.db")
            journal = ImprovementLoop(f"sqlite:///{db_file}")

            journal.record_cycle_start(
                grid_id="grid_meta",
                symbol="ZEC/USDT:USDT",
                upper=358.0,
                lower=351.0,
                num_grids=8,
                leverage=8,
                direction="neutral",
                adjusted_leverage=8,
                adjusted_order_size=1.0,
            )

            con = sqlite3.connect(db_file)
            row = con.execute(
                "SELECT direction, adjusted_leverage, adjusted_order_size "
                "FROM grid_cycles WHERE grid_id='grid_meta'"
            ).fetchone()
            con.close()

            self.assertEqual(row, ("neutral", 8, 1.0))

    def test_cycle_start_and_close_persist_entry_shape_and_fill_danger_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "entry_shape_fill_danger.db")
            journal = ImprovementLoop(f"sqlite:///{db_file}")

            journal.record_cycle_start(
                grid_id="grid_shape",
                symbol="BTC/USDT:USDT",
                upper=108.0,
                lower=96.0,
                num_grids=12,
                leverage=7,
                direction="long",
                adjusted_leverage=7,
                adjusted_order_size=2.5,
                entry_shape_template="trend_pullback",
                entry_shape_spacing="buy_weighted",
                entry_shape_confidence=0.82,
                entry_buy_density_bias=0.7,
                entry_sell_density_bias=0.3,
                entry_shape_notes="pullback ladder",
            )
            journal.record_cycle_close(
                grid_id="grid_shape",
                total_pnl=1.25,
                realized_pnl=0.9,
                unrealized_pnl=0.35,
                fills=4,
                duration=180.0,
                close_reason="target_hit",
                wallet_balance=101.25,
                wallet_exposure_pct=12.0,
                direction="long",
                adjusted_leverage=7,
                adjusted_order_size=2.5,
                fill_danger="high",
                fill_danger_score=0.8,
            )

            con = sqlite3.connect(db_file)
            cols = {row[1] for row in con.execute("PRAGMA table_info(grid_cycles)")}
            row = con.execute(
                "SELECT entry_shape_template, entry_shape_spacing, entry_quality_score, entry_shape_confidence, "
                "entry_buy_density_bias, entry_sell_density_bias, entry_shape_notes, "
                "fill_danger, fill_danger_score "
                "FROM grid_cycles WHERE grid_id='grid_shape'"
            ).fetchone()
            con.close()

            self.assertIn("entry_quality_score", cols)
            self.assertEqual(
                row,
                ("trend_pullback", "buy_weighted", 0.82, 0.82, 0.7, 0.3, "pullback ladder", "high", 0.8),
            )

    def test_migration_backfills_entry_quality_score_from_legacy_entry_shape_confidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "legacy_entry_shape_confidence.db")
            con = sqlite3.connect(db_file)
            con.execute(
                """
                CREATE TABLE grid_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    grid_id VARCHAR(100) UNIQUE,
                    symbol VARCHAR(50),
                    upper_price FLOAT,
                    lower_price FLOAT,
                    num_grids INTEGER,
                    leverage INTEGER,
                    entry_shape_template VARCHAR(50) DEFAULT 'atr_box',
                    entry_shape_spacing VARCHAR(50) DEFAULT 'balanced',
                    entry_shape_confidence FLOAT DEFAULT 0.0,
                    entry_buy_density_bias FLOAT DEFAULT 0.5,
                    entry_sell_density_bias FLOAT DEFAULT 0.5,
                    entry_shape_notes TEXT DEFAULT ''
                )
                """
            )
            con.execute(
                """
                INSERT INTO grid_cycles (
                    grid_id, symbol, upper_price, lower_price, num_grids, leverage,
                    entry_shape_template, entry_shape_spacing, entry_shape_confidence,
                    entry_buy_density_bias, entry_sell_density_bias, entry_shape_notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy_grid",
                    "SOL/USDT:USDT",
                    170.0,
                    150.0,
                    10,
                    5,
                    "trend_pullback",
                    "buy_weighted",
                    0.67,
                    0.65,
                    0.35,
                    "legacy confidence only",
                ),
            )
            con.commit()
            con.close()

            ImprovementLoop(f"sqlite:///{db_file}")

            con = sqlite3.connect(db_file)
            cols = {row[1] for row in con.execute("PRAGMA table_info(grid_cycles)")}
            row = con.execute(
                "SELECT entry_quality_score, entry_shape_confidence FROM grid_cycles WHERE grid_id='legacy_grid'"
            ).fetchone()
            con.close()

            self.assertIn("entry_quality_score", cols)
            self.assertEqual(row, (0.67, 0.67))

    def test_serialize_slots_exports_entry_shape_and_fill_danger_metadata(self):
        coin_score = CoinScore(
            symbol="BTC/USDT:USDT",
            price=100.0,
            high_24h=110.0,
            low_24h=90.0,
            volume_24h_usdt=1_000_000.0,
            atr_pct=1.2,
            range_pct=8.0,
            mean_reversion_score=0.55,
            grid_score=0.78,
            suggested_upper=108.0,
            suggested_lower=96.0,
            suggested_grids=12,
            suggested_leverage=7,
            trend_direction="long",
            market_regime="trending_up",
            entry_quality_score=0.81,
            range_position=0.35,
            vwap_distance_pct=-0.8,
            pullback_depth_pct=2.1,
            slope_score=0.4,
            acceleration_score=0.1,
            entry_shape_template="trend_pullback",
            entry_shape_spacing="buy_weighted",
            entry_buy_density_bias=0.72,
            entry_sell_density_bias=0.28,
            entry_shape_notes="deeper bids below market",
        )
        slot = SimpleNamespace(
            slot_id=1,
            symbol="BTC/USDT:USDT",
            decision=SimpleNamespace(direction="long"),
            adjusted_leverage=7,
            adjusted_order_size=2.5,
            started_at=0.0,
            close_reason=None,
            coin_score=coin_score,
            state=SimpleNamespace(
                grid=SimpleNamespace(
                    grid_id="grid_shape",
                    upper_price=108.0,
                    lower_price=96.0,
                    num_grids=12,
                )
            ),
            engine=SimpleNamespace(
                get_status=lambda: {
                    "total_pnl": 1.0,
                    "realized_pnl": 0.6,
                    "unrealized_pnl": 0.4,
                    "fills": 4,
                    "current_price": 101.0,
                    "fill_log": [],
                    "grid_levels": [],
                    "allocated_margin_usdt": 2.5,
                    "target_pnl_low": 0.5,
                    "target_pnl_high": 1.2,
                    "target_pnl_pct_low": 1.0,
                    "target_pnl_pct_high": 2.0,
                    "max_drawdown_pct": -0.8,
                    "duration_sec": 120.0,
                    "filled_levels": 3,
                    "position_qty": 0.1,
                    "position_side": "Buy",
                    "entry_price": 100.5,
                    "imbalance_ratio": 1.7,
                },
                _adaptive=SimpleNamespace(
                    exposure_cap=SimpleNamespace(
                        exposure=SimpleNamespace(consecutive_same_side=4)
                    ),
                    config=SimpleNamespace(max_same_side_fills=5),
                ),
            ),
        )

        with patch("multi_grid_manager.time.time", return_value=120.0):
            payload = multi_grid_manager._serialize_slots({1: slot})

        row = payload["1"]
        self.assertEqual(row["entry_shape_template"], "trend_pullback")
        self.assertEqual(row["entry_shape_spacing"], "buy_weighted")
        self.assertEqual(row["entry_quality_score"], 0.81)
        self.assertEqual(row["entry_buy_density_bias"], 0.72)
        self.assertEqual(row["entry_sell_density_bias"], 0.28)
        self.assertEqual(row["entry_shape_notes"], "deeper bids below market")
        self.assertEqual(row["fill_danger"], "high")
        self.assertEqual(row["fill_danger_score"], 0.8)
        self.assertEqual(row["fill_danger_same_side_fills"], 4)
        self.assertEqual(row["fill_danger_max_same_side_fills"], 5)

    def test_state_writer_loads_closed_trade_stats_from_database_source_of_truth(self):
        """Dashboard state stats must use durable DB counts, not restart-local memory counters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "source_of_truth.db")
            con = sqlite3.connect(db_file)
            con.execute(
                """
                CREATE TABLE grid_cycles (
                    grid_id VARCHAR(100) UNIQUE,
                    symbol VARCHAR(50),
                    upper_price FLOAT,
                    lower_price FLOAT,
                    num_grids INTEGER,
                    leverage INTEGER,
                    total_pnl FLOAT,
                    realized_pnl FLOAT DEFAULT 0.0,
                    fills_count INTEGER DEFAULT 0,
                    duration_seconds FLOAT DEFAULT 0.0,
                    started_at DATETIME,
                    closed_at DATETIME,
                    close_reason VARCHAR(50),
                    was_profitable BOOLEAN DEFAULT 0
                )
                """
            )
            rows = [
                ("g1", "BTC/USDT:USDT", 100.0, 90.0, 10, 5, 0.30, 0.25, 4, 60.0, "s1", "c1", "target", 1),
                ("g2", "ETH/USDT:USDT", 100.0, 90.0, 10, 5, -0.10, -0.10, 3, 45.0, "s2", "c2", "stop", 0),
            ]
            con.executemany(
                """
                INSERT INTO grid_cycles (
                    grid_id, symbol, upper_price, lower_price, num_grids, leverage,
                    total_pnl, realized_pnl, fills_count, duration_seconds,
                    started_at, closed_at, close_reason, was_profitable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            con.commit()
            con.close()

            old_db = multi_grid_manager.TRADE_DB_FILE
            multi_grid_manager.TRADE_DB_FILE = db_file
            try:
                stats, trades = multi_grid_manager._load_closed_trade_source_of_truth()
            finally:
                multi_grid_manager.TRADE_DB_FILE = old_db

            self.assertEqual(stats["total_trades"], 2)
            self.assertEqual(stats["wins"], 1)
            self.assertEqual(stats["losses"], 1)
            self.assertEqual(stats["total_pnl"], 0.2)
            self.assertEqual(len(trades), 2)
            self.assertEqual(trades[0]["slot_id"], "g2")

    def test_wallet_tracker_reports_margin_exposure_not_leveraged_notional(self):
        """Wallet/dashboard exposure should use reserved margin, not 50x notional."""
        from wallet_tracker import WalletTracker

        wallet = WalletTracker(initial_balance=100.0)
        wallet.update_position(
            symbol="XMR/USDT:USDT",
            direction="neutral",
            order_size_usdt=1.0,
            leverage=50,
            unrealized_pnl=0.0,
            num_fills=3,
        )

        state = wallet.get_wallet_state()

        self.assertEqual(state["total_exposure_usdt"], 3.0)
        self.assertEqual(state["total_margin_used"], 3.0)
        self.assertEqual(state["exposure_pct"], 3.0)
        self.assertEqual(wallet.get_position_exposure_pct("XMR/USDT:USDT"), 3.0)

    def test_portfolio_exposure_uses_margin_not_leveraged_notional(self):
        """Cross-margin risk exposure should reserve filled margin, not leveraged notional."""
        monitor = PortfolioRiskMonitor(profiles_path="/tmp/nonexistent-token-profiles.json")
        active_grids = {
            1: {
                "symbol": "ZEC/USDT:USDT",
                "direction": "neutral",
                "adjusted_order_size": 1.0,
                "adjusted_leverage": 50,
                "fills": 3,
            }
        }

        exposure = monitor.get_portfolio_exposure(active_grids, wallet_balance=100.0)
        actions = monitor.check_emergency(wallet_balance=100.0, active_grids=active_grids)

        self.assertEqual(exposure["total_exposure_pct"], 3.0)
        self.assertEqual(exposure["neutral_exposure_pct"], 3.0)
        self.assertEqual(actions, {"emergency": False})

    def test_check_deploy_accepts_live_grid_slot_objects(self):
        """Risk monitor must not crash when active grids are GridSlot-like objects."""
        monitor = PortfolioRiskMonitor(profiles_path="/tmp/nonexistent-token-profiles.json")
        monitor.portfolio_config = {
            "max_trade_wallet_exposure_pct": 10,
            "max_total_wallet_exposure_pct": 80,
        }
        live_slot = SimpleNamespace(
            adjusted_order_size=1.0,
            fills=0,
            decision=SimpleNamespace(direction="neutral", num_grids=10),
        )

        result = monitor.check_deploy(
            symbol="AAVE/USDT:USDT",
            direction="neutral",
            leverage=50,
            order_size_usdt=1.0,
            wallet_balance=100.0,
            active_grids={1: live_slot},
            num_grids=10,
        )

        self.assertTrue(result["approved"], result)

    def test_build_scanner_candidate_decision_matches_algorithmic_defaults(self):
        candidate = CoinScore(
            symbol="BTC/USDT:USDT",
            price=100.0,
            high_24h=108.0,
            low_24h=96.0,
            volume_24h_usdt=50_000_000,
            atr_pct=1.2,
            range_pct=7.0,
            mean_reversion_score=0.35,
            grid_score=0.84,
            suggested_upper=108.0,
            suggested_lower=96.0,
            suggested_grids=14,
            suggested_leverage=50,
            trend_direction="long",
        )

        decision = multi_grid_manager.build_scanner_candidate_decision(
            candidate,
            token_profile={"leverage": 12},
            wallet_balance=100.0,
        )

        self.assertEqual(decision.symbol, "BTC/USDT:USDT")
        self.assertEqual(decision.direction, "long")
        self.assertEqual(decision.market_regime, "ranging")
        self.assertEqual(decision.leverage, 12)
        self.assertEqual(decision.num_grids, 6)
        self.assertEqual(decision.confidence, 0.84)

    def test_scanner_candidate_prefilter_rejects_obvious_supervisor_failures(self):
        valid = CoinScore(
            symbol="BTC/USDT:USDT",
            price=100.0,
            high_24h=106.0,
            low_24h=94.0,
            volume_24h_usdt=75_000_000,
            atr_pct=1.1,
            range_pct=6.0,
            mean_reversion_score=0.75,
            grid_score=0.82,
            suggested_upper=106.0,
            suggested_lower=94.0,
            suggested_grids=12,
            suggested_leverage=50,
            trend_direction="neutral",
            entry_quality_score=0.72,
        )
        low_quality = CoinScore(
            symbol="DOGE/USDT:USDT",
            price=100.0,
            high_24h=106.0,
            low_24h=94.0,
            volume_24h_usdt=60_000_000,
            atr_pct=1.0,
            range_pct=6.0,
            mean_reversion_score=0.7,
            grid_score=0.81,
            suggested_upper=106.0,
            suggested_lower=94.0,
            suggested_grids=12,
            suggested_leverage=50,
            trend_direction="neutral",
            entry_quality_score=0.20,
        )
        too_wide = CoinScore(
            symbol="SOL/USDT:USDT",
            price=100.0,
            high_24h=109.0,
            low_24h=91.0,
            volume_24h_usdt=55_000_000,
            atr_pct=1.3,
            range_pct=18.0,
            mean_reversion_score=0.65,
            grid_score=0.83,
            suggested_upper=109.0,
            suggested_lower=91.0,
            suggested_grids=12,
            suggested_leverage=50,
            trend_direction="neutral",
            entry_quality_score=0.70,
        )

        filtered = multi_grid_manager.prefilter_scanner_candidates_for_deploy(
            [valid, low_quality, too_wide],
            token_profile_by_symbol={
                "BTC/USDT:USDT": {"min_entry_quality": 0.35, "max_grid_width_pct": 15.0},
                "DOGE/USDT:USDT": {"min_entry_quality": 0.35, "max_grid_width_pct": 15.0},
                "SOL/USDT:USDT": {"min_entry_quality": 0.35, "max_grid_width_pct": 15.0},
            },
            wallet_balance=100.0,
        )

        self.assertEqual([coin.symbol for coin in filtered], ["BTC/USDT:USDT", "DOGE/USDT:USDT", "SOL/USDT:USDT"])
        shaped = next(coin for coin in filtered if coin.symbol == "DOGE/USDT:USDT")
        self.assertEqual(getattr(shaped, "grid_style"), "micro_scalp")
        self.assertLessEqual(shaped.suggested_grids, 10)
        repaired = next(coin for coin in filtered if coin.symbol == "SOL/USDT:USDT")
        self.assertAlmostEqual(repaired.suggested_upper - repaired.suggested_lower, 15.0, places=5)

    # Removed: test_hummingbot_backend_factory_is_noop_for_default_dry_run
    # (is_hummingbot_execution_backend and create_execution_adapter were removed)

    # Removed: test_build_grid_deploy_request_preserves_risk_adjusted_trade_shape
    # (build_grid_deploy_request was removed)

    # Removed: test_hummingbot_backend_factory_requires_paper_config_but_does_not_place_orders
    # (HUMMINGBOT_HOME was removed)


if __name__ == "__main__":
    unittest.main()
