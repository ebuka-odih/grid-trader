import os
import sqlite3
import tempfile
import unittest

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
        self.assertFalse(symbol_has_grid_capacity(active_symbols, "DOGE/USDT:USDT"))
        self.assertTrue(symbol_has_grid_capacity(active_symbols, "ZEC/USDT:USDT"))

    def test_grid_density_is_normalized_to_scalping_range(self):
        """Decision/scanner grid counts should be clamped to 10–20 internal levels."""
        self.assertEqual(normalize_grid_density(5), 10)
        self.assertEqual(normalize_grid_density(10), 10)
        self.assertEqual(normalize_grid_density(15), 15)
        self.assertEqual(normalize_grid_density(30), 20)

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
        actions = monitor.check_emergency(active_grids, wallet_balance=100.0)

        self.assertEqual(exposure["total_exposure_pct"], 3.0)
        self.assertEqual(exposure["neutral_exposure_pct"], 3.0)
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
