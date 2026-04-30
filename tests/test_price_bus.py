import asyncio
import unittest

from price_bus import PriceBus, symbol_to_ws_symbol, ws_symbol_to_symbol


class PriceBusTests(unittest.TestCase):
    def test_symbol_conversion_round_trip(self):
        self.assertEqual(symbol_to_ws_symbol("AAVE/USDT:USDT"), "AAVEUSDT")
        self.assertEqual(ws_symbol_to_symbol("AAVEUSDT"), "AAVE/USDT:USDT")
        self.assertEqual(ws_symbol_to_symbol("1000PEPEUSDT"), "1000PEPE/USDT:USDT")

    def test_publish_tracks_latest_price_age_for_heartbeat(self):
        async def scenario():
            now = [100.0]
            bus = PriceBus(queue_size=1, now_fn=lambda: now[0])
            await bus.publish("AAVE/USDT:USDT", 95.5)
            now[0] = 112.5

            self.assertEqual(bus.latest_price("AAVE/USDT:USDT"), 95.5)
            self.assertEqual(bus.latest_price_age("AAVE/USDT:USDT"), 12.5)

        asyncio.run(scenario())

    def test_publish_fans_out_latest_price_to_all_subscribers(self):
        async def scenario():
            bus = PriceBus(queue_size=1)
            q1 = await bus.subscribe("AAVE/USDT:USDT")
            q2 = await bus.subscribe("AAVE/USDT:USDT")

            await bus.publish("AAVE/USDT:USDT", 94.25)

            self.assertEqual(await asyncio.wait_for(q1.get(), timeout=0.1), 94.25)
            self.assertEqual(await asyncio.wait_for(q2.get(), timeout=0.1), 94.25)
            self.assertEqual(bus.latest_price("AAVE/USDT:USDT"), 94.25)

        asyncio.run(scenario())

    def test_bybit_ticker_message_is_routed_to_project_symbol(self):
        async def scenario():
            bus = PriceBus(ws_url="wss://example.invalid")
            q = await bus.subscribe("ETH/USDT:USDT")
            await bus._handle_message('{"topic":"tickers.ETHUSDT","data":{"lastPrice":"2301.50"}}')

            self.assertEqual(await asyncio.wait_for(q.get(), timeout=0.1), 2301.50)
            self.assertEqual(bus.latest_price("ETH/USDT:USDT"), 2301.50)

        asyncio.run(scenario())

    def test_unsubscribe_removes_queue_and_symbol_when_last_subscriber_leaves(self):
        async def scenario():
            bus = PriceBus(ws_url="wss://example.invalid")
            q1 = await bus.subscribe("ETH/USDT:USDT")
            q2 = await bus.subscribe("ETH/USDT:USDT")

            await bus.unsubscribe("ETH/USDT:USDT", q1)
            self.assertIn("ETH/USDT:USDT", bus.active_symbols)

            await bus.unsubscribe("ETH/USDT:USDT", q2)
            self.assertNotIn("ETH/USDT:USDT", bus.active_symbols)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
