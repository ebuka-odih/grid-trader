import unittest
import asyncio
from unittest.mock import patch

import grid_api


class GridApiStateNormalizationTests(unittest.TestCase):
    def test_coerce_state_restores_missing_top_level_keys_without_retaining_stale_shape(self):
        raw = {
            'mode': 'running',
            'started_at': 1777214400,
            'wallet': {
                'balance': 100.1,
                'exposure_pct': 0.61,
            },
            'slots': {
                '1': {'slot_id': 1, 'symbol': 'BASED/USDT:USDT', 'status': 'active'}
            },
            'heartbeat': {
                'active': 5,
                'stale': 3,
            },
            'stats': {
                'total_trades': 99,
            },
            'last_update': 1777214430.61,
        }

        normalized = grid_api._coerce_state(raw)

        self.assertEqual(normalized['mode'], 'running')
        self.assertEqual(normalized['wallet']['balance'], 100.1)
        self.assertEqual(normalized['wallet']['exposure_pct'], 0.61)
        self.assertIn('scanner_candidates', normalized)
        self.assertEqual(normalized['scanner_candidates'], [])
        self.assertIn('completed_trades', normalized)
        self.assertEqual(normalized['completed_trades'], [])
        self.assertIn('current_prices', normalized)
        self.assertEqual(normalized['current_prices'], {})
        self.assertIn('portfolio', normalized)
        self.assertIn('pause_reason', normalized['heartbeat'])
        self.assertEqual(normalized['stats']['total_trades'], 99)
        self.assertEqual(normalized['slots']['1']['symbol'], 'BASED/USDT:USDT')
        self.assertEqual(normalized['last_update'], 1777214430.61)

    def test_state_metadata_balance_uses_state_wallet_as_source_of_truth(self):
        original_state = grid_api._state
        try:
            grid_api._state = grid_api._coerce_state({
                'mode': 'running',
                'wallet': {
                    'initial_balance': 100.0,
                    'balance': 100.0,
                },
                'slots': {
                    '1': {
                        'symbol': 'BTC/USDT:USDT',
                        'realized_pnl': 0.25,
                        'unrealized_pnl': -0.05,
                    }
                },
                'last_update': 1777214430.61,
            })
            db_stats = {
                'total_trades': 3,
                'wins': 2,
                'losses': 1,
                'win_rate': 66.67,
                'total_pnl': 1.5,
            }

            with patch.object(grid_api, '_load_db_performance', return_value=(db_stats, [])):
                state = grid_api._state_with_metadata()

            # Balance comes from state wallet directly (bot writes it correctly).
            # DB stats are for display only — not added to balance.
            self.assertEqual(state['wallet']['balance'], 100.0)
            self.assertEqual(state['wallet']['unrealized_pnl'], -0.05)
            self.assertEqual(state['wallet']['equity'], 99.95)
            self.assertEqual(state['stats']['total_pnl'], 0.0)  # from state realized_pnl, not DB
            self.assertEqual(state['stats']['active_pnl'], -0.05)
        finally:
            grid_api._state = original_state

    def test_wallet_endpoint_returns_corrected_metadata_wallet(self):
        original_state = grid_api._state
        try:
            grid_api._state = grid_api._coerce_state({
                'wallet': {'initial_balance': 100.0, 'balance': 100.0},
                'slots': {'1': {'realized_pnl': 0.1, 'unrealized_pnl': 0.2}},
            })
            with patch.object(grid_api, '_load_state_file'), patch.object(
                grid_api,
                '_load_db_performance',
                return_value=({'total_pnl': 1.0}, []),
            ):
                wallet = asyncio.run(grid_api.get_wallet())

            # Balance from state wallet (100.0), not initial + DB + realized
            self.assertEqual(wallet['balance'], 100.0)
            self.assertEqual(wallet['unrealized_pnl'], 0.2)
            self.assertEqual(wallet['equity'], 100.2)
        finally:
            grid_api._state = original_state


if __name__ == '__main__':
    unittest.main()
