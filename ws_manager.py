"""
Bybit WebSocket Manager — handles public (ticker) and private (order/position) streams.
Uses pybit's HTTP for auth, raw websockets for real-time data.
"""

import asyncio
import json
import hmac
import hashlib
import time
import logging
from typing import Callable, Optional

import websockets

from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET,
    BYBIT_WS_PUBLIC, BYBIT_WS_PRIVATE,
    TRADING_MODE
)

logger = logging.getLogger("ws_manager")


class BybitWSManager:
    """Manages persistent WebSocket connections to Bybit V5 API."""

    def __init__(self):
        self._public_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._private_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._public_callbacks: dict[str, Callable] = {}  # topic -> callback
        self._private_callbacks: dict[str, Callable] = {}
        self._running = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 30

    # ── Public WebSocket (tickers, klines, orderbook) ──────────

    async def connect_public(self):
        """Connect to the public WebSocket and subscribe to topics."""
        logger.info(f"Connecting to public WS: {BYBIT_WS_PUBLIC}")
        self._public_ws = await websockets.connect(
            BYBIT_WS_PUBLIC,
            ping_interval=20,
            ping_timeout=10,
        )
        self._running = True
        logger.info("✅ Public WS connected")

    async def subscribe_ticker(self, symbol: str, callback: Callable):
        """Subscribe to real-time ticker for a linear perpetual symbol."""
        topic = f"tickers.{symbol}"
        self._public_callbacks[topic] = callback
        msg = {"op": "subscribe", "args": [topic]}
        if self._public_ws:
            await self._public_ws.send(json.dumps(msg))
            logger.info(f"📡 Subscribed to {topic}")

    async def subscribe_kline(self, symbol: str, interval: str, callback: Callable):
        """Subscribe to kline/candlestick data. interval: 1,3,5,15,30,60,120,240,360,720,D,W,M"""
        topic = f"kline.{interval}.{symbol}"
        self._public_callbacks[topic] = callback
        msg = {"op": "subscribe", "args": [topic]}
        if self._public_ws:
            await self._public_ws.send(json.dumps(msg))
            logger.info(f"📡 Subscribed to {topic}")

    # ── Private WebSocket (orders, positions, executions) ──────

    async def connect_private(self):
        """Connect to private WebSocket with auth."""
        logger.info(f"Connecting to private WS: {BYBIT_WS_PRIVATE}")
        self._private_ws = await websockets.connect(
            BYBIT_WS_PRIVATE,
            ping_interval=20,
            ping_timeout=10,
        )
        # Authenticate
        expires = int((time.time() + 10) * 1000)
        signature = hmac.new(
            BYBIT_API_SECRET.encode(),
            f"GET/realtime{expires}".encode(),
            hashlib.sha256,
        ).hexdigest()
        auth_msg = {
            "op": "auth",
            "args": [BYBIT_API_KEY, expires, signature],
        }
        await self._private_ws.send(json.dumps(auth_msg))
        resp = await asyncio.wait_for(self._private_ws.recv(), timeout=5)
        resp_data = json.loads(resp)
        if resp_data.get("op") == "auth" and resp_data.get("success"):
            logger.info("✅ Private WS authenticated")
        else:
            logger.error(f"❌ Private WS auth failed: {resp_data}")
            raise ConnectionError("Private WS auth failed")
        self._running = True

    async def subscribe_position(self, callback: Callable):
        """Subscribe to position updates."""
        topic = "position"
        self._private_callbacks[topic] = callback
        msg = {"op": "subscribe", "args": ["position"]}
        if self._private_ws:
            await self._private_ws.send(json.dumps(msg))
            logger.info("📡 Subscribed to position updates")

    async def subscribe_execution(self, callback: Callable):
        """Subscribe to execution (fill) updates."""
        topic = "execution"
        self._private_callbacks[topic] = callback
        msg = {"op": "subscribe", "args": ["execution"]}
        if self._private_ws:
            await self._private_ws.send(json.dumps(msg))
            logger.info("📡 Subscribed to execution updates")

    async def subscribe_order(self, callback: Callable):
        """Subscribe to order updates."""
        topic = "order"
        self._private_callbacks[topic] = callback
        msg = {"op": "subscribe", "args": ["order"]}
        if self._private_ws:
            await self._private_ws.send(json.dumps(msg))
            logger.info("📡 Subscribed to order updates")

    # ── Listen loops ───────────────────────────────────────────

    async def listen_public(self):
        """Continuously listen for public WS messages."""
        while self._running:
            try:
                msg = await self._public_ws.recv()
                data = json.loads(msg)
                # Handle subscription confirmations
                if data.get("op") == "subscribe":
                    continue
                # Route to callback by topic
                if "topic" in data:
                    topic = data["topic"]
                    for key, cb in self._public_callbacks.items():
                        if topic.startswith(key.split(".")[0]):
                            await cb(data)
                            break
            except websockets.ConnectionClosed:
                logger.warning("⚠️ Public WS closed, reconnecting...")
                await self._reconnect_public()
            except Exception as e:
                logger.error(f"Public WS error: {e}")
                await asyncio.sleep(1)

    async def listen_private(self):
        """Continuously listen for private WS messages."""
        while self._running:
            try:
                msg = await self._private_ws.recv()
                data = json.loads(msg)
                # Route to callback
                if "topic" in data:
                    topic = data["topic"]
                    for key, cb in self._private_callbacks.items():
                        if key in topic:
                            await cb(data)
                            break
            except websockets.ConnectionClosed:
                logger.warning("⚠️ Private WS closed, reconnecting...")
                await self._reconnect_private()
            except Exception as e:
                logger.error(f"Private WS error: {e}")
                await asyncio.sleep(1)

    # ── Reconnect logic ────────────────────────────────────────

    async def _reconnect_public(self):
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
        await asyncio.sleep(self._reconnect_delay)
        await self.connect_public()
        # Re-subscribe all topics
        for topic in self._public_callbacks:
            msg = {"op": "subscribe", "args": [topic]}
            await self._public_ws.send(json.dumps(msg))
        self._reconnect_delay = 1

    async def _reconnect_private(self):
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
        await asyncio.sleep(self._reconnect_delay)
        await self.connect_private()
        for topic in self._private_callbacks:
            msg = {"op": "subscribe", "args": [topic]}
            await self._private_ws.send(json.dumps(msg))
        self._reconnect_delay = 1

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self, need_private: bool = False):
        """Start all connections and listen loops."""
        await self.connect_public()
        tasks = [asyncio.create_task(self.listen_public())]
        if need_private:
            await self.connect_private()
            tasks.append(asyncio.create_task(self.listen_private()))
        await asyncio.gather(*tasks)

    async def stop(self):
        """Gracefully shutdown."""
        self._running = False
        if self._public_ws:
            await self._public_ws.close()
        if self._private_ws:
            await self._private_ws.close()
        logger.info("WebSocket manager stopped")
