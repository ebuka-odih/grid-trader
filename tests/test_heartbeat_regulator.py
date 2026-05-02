import asyncio
import time
import unittest
from types import SimpleNamespace

from heartbeat_regulator import HeartbeatRegulator


class DummyTask:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


class DummyPriceBus:
    def __init__(self, ages=None, running=True):
        self._ages = ages or {}
        self._running = running

    @property
    def active_symbols(self):
        return set(self._ages)

    def latest_price_age(self, symbol):
        return self._ages.get(symbol)

    def health_status(self):
        return {"running": self._running, "active_symbols": len(self._ages)}


class DummyManager:
    def __init__(self):
        self._running = True
        self._deployment_paused_until = 0
        self._pause_reason = None
        self.wallet_updates = 0
        self.risk_checks = 0
        self.state_pushes = 0
        self.price_bus = DummyPriceBus()
        self.slots = {}

    def _push_api_state(self):
        self.state_pushes += 1

    def _update_wallet_tracker(self):
        self.wallet_updates += 1

    async def _run_emergency_checks(self):
        self.risk_checks += 1


class HeartbeatRegulatorTests(unittest.TestCase):
    def test_heartbeat_syncs_wallet_and_runs_risk_checks(self):
        async def scenario():
            manager = DummyManager()
            hb = HeartbeatRegulator(manager, interval_seconds=1, now_fn=lambda: 1000.0)

            snapshot = await hb.beat()

            self.assertEqual(manager.wallet_updates, 1)
            self.assertEqual(manager.risk_checks, 1)
            self.assertEqual(manager.state_pushes, 1)
            self.assertEqual(snapshot.actions, [])
            self.assertEqual(snapshot.active_grids, 0)

        asyncio.run(scenario())

    def test_heartbeat_pauses_deployments_when_price_bus_is_not_running(self):
        async def scenario():
            now = 1000.0
            manager = DummyManager()
            manager.price_bus = DummyPriceBus(running=False)
            hb = HeartbeatRegulator(manager, pause_seconds=30, now_fn=lambda: now)

            snapshot = await hb.beat()

            self.assertGreaterEqual(manager._deployment_paused_until, now + 30)
            self.assertIn("pause_deployments:price_bus_down", snapshot.actions)

        asyncio.run(scenario())

    def test_heartbeat_does_not_close_new_grid_before_first_tick_grace(self):
        async def scenario():
            now = 1000.0
            manager = DummyManager()
            manager.price_bus = DummyPriceBus({"FAST/USDT:USDT": None})
            task = DummyTask()
            slot = SimpleNamespace(
                slot_id=1,
                symbol="FAST/USDT:USDT",
                task=task,
                close_reason="",
                started_at=now - 30,
                state=SimpleNamespace(is_active=True),
            )
            manager.slots = {1: slot}
            hb = HeartbeatRegulator(
                manager,
                max_tick_age_seconds=90,
                pause_seconds=45,
                now_fn=lambda: now,
            )

            snapshot = await hb.beat()

            self.assertTrue(slot.state.is_active)
            self.assertFalse(task.cancelled)
            self.assertEqual(snapshot.actions, [])

        asyncio.run(scenario())

    def test_heartbeat_closes_stale_grid_and_pauses_new_deployments(self):
        async def scenario():
            now = 1000.0
            manager = DummyManager()
            manager.price_bus = DummyPriceBus({"FAST/USDT:USDT": 91.0})
            task = DummyTask()
            slot = SimpleNamespace(
                slot_id=1,
                symbol="FAST/USDT:USDT",
                task=task,
                close_reason="",
                started_at=now - 30,
                state=SimpleNamespace(is_active=True),
                engine=SimpleNamespace(get_status=lambda: {"fills": 0, "total_pnl": 0.0}),
            )
            manager.slots = {1: slot}
            hb = HeartbeatRegulator(
                manager,
                max_tick_age_seconds=90,
                pause_seconds=45,
                close_stale_after_seconds=-1,
                min_stale_to_pause=1,
                now_fn=lambda: now,
            )

            snapshot = await hb.beat()

            self.assertFalse(slot.state.is_active)
            self.assertEqual(slot.close_reason, "heartbeat_stale_price")
            self.assertTrue(task.cancelled)
            self.assertGreaterEqual(manager._deployment_paused_until, now + 45)
            self.assertEqual(manager._pause_reason, "stale_price_data")
            self.assertIn("close_stale_grid:1:FAST/USDT:USDT", snapshot.actions)

        asyncio.run(scenario())

    def test_heartbeat_ignores_price_bus_symbols_without_active_slots(self):
        async def scenario():
            now = 1000.0
            manager = DummyManager()
            manager.price_bus = DummyPriceBus({"SCANONLY/USDT:USDT": None})
            hb = HeartbeatRegulator(
                manager,
                max_tick_age_seconds=90,
                pause_seconds=45,
                now_fn=lambda: now,
            )

            snapshot = await hb.beat()

            self.assertEqual(snapshot.actions, [])
            self.assertEqual(manager._deployment_paused_until, 0)
            self.assertIsNone(manager._pause_reason)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
