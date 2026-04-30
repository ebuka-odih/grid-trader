"""
Shared WebSocket Price Bus for high-speed multi-grid dry-run execution.

Instead of opening one Bybit public WebSocket per grid, the manager owns one
PriceBus. Grids subscribe to symbol queues and receive fan-out price ticks from
a single public connection. This is the base layer for many concurrent bots:
fast deterministic tick handling, lower socket overhead, and one reconnect loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Callable, Optional

import websockets

from config import BYBIT_WS_PUBLIC

logger = logging.getLogger("price_bus")


def symbol_to_ws_symbol(symbol: str) -> str:
    """Convert ccxt/bybit unified symbol to Bybit v5 topic symbol."""
    return symbol.replace("/", "").replace(":USDT", "")


def ws_symbol_to_symbol(ws_symbol: str) -> str:
    """Convert Bybit v5 linear USDT topic symbol to project symbol form."""
    if ws_symbol.endswith("USDT"):
        return f"{ws_symbol[:-4]}/USDT:USDT"
    return ws_symbol


class PriceBus:
    """
    One public WebSocket connection faning price ticks to many grid monitors.

    Tests can call publish() directly without opening a network socket. Runtime
    uses start()/stop() with subscribe()/unsubscribe().
    """

    def __init__(
        self, 
        ws_url: str = BYBIT_WS_PUBLIC, 
        queue_size: int = 1, 
        now_fn: Callable[[], float] | None = None,
        stale_price_threshold: float = 5.0  # seconds
    ):
        self.ws_url = ws_url
        self.queue_size = queue_size
        self._now_fn = now_fn or time.time
        self._stale_price_threshold = stale_price_threshold
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._latest_prices: dict[str, float] = {}
        self._latest_update_ts: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._subscribed_ws_symbols: set[str] = set()
        self._health_status: dict = {
            "last_error": None,
            "reconnect_count": 0,
            "last_successful_recv": None,
            "ws_connected": False,
        }

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def active_symbols(self) -> set[str]:
        return {symbol for symbol, queues in self._subscribers.items() if queues}

    def latest_price(self, symbol: str) -> Optional[float]:
        return self._latest_prices.get(symbol)

    def latest_price_age(self, symbol: str) -> Optional[float]:
        ts = self._latest_update_ts.get(symbol)
        if ts is None:
            return None
        return max(0.0, self._now_fn() - ts)

    def is_price_fresh(self, symbol: str) -> bool:
        """Check if price for symbol is fresh (not stale)."""
        age = self.latest_price_age(symbol)
        return age is not None and age <= self._stale_price_threshold

    def health_status(self) -> dict:
        """Get detailed health status of the price bus."""
        base_status = {
            "running": self._running,
            "connected": self._ws is not None,
            "active_symbols": len(self.active_symbols),
            "subscribed_ws_symbols": len(self._subscribed_ws_symbols),
        }
        base_status.update(self._health_status)
        return base_status

    async def start(self):
        """Start the background WebSocket reader if it is not already running."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="shared_price_bus")
        logger.info("📡 Shared price bus started")

    async def stop(self):
        """Stop the background reader and close the socket."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._task = None
        logger.info("📡 Shared price bus stopped")

    async def subscribe(self, symbol: str) -> asyncio.Queue:
        """Subscribe to a project symbol and return a queue of price floats."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        async with self._lock:
            was_new = symbol not in self._subscribers or not self._subscribers[symbol]
            self._subscribers[symbol].add(queue)
            latest = self._latest_prices.get(symbol)
        if latest is not None:
            await self._offer(queue, latest)
        if was_new:
            await self._send_subscribe(symbol)
        return queue

    async def unsubscribe(self, symbol: str, queue: asyncio.Queue):
        async with self._lock:
            queues = self._subscribers.get(symbol)
            if not queues:
                return
            queues.discard(queue)
            if not queues:
                self._subscribers.pop(symbol, None)
                self._latest_prices.pop(symbol, None)
                self._latest_update_ts.pop(symbol, None)
                await self._send_unsubscribe(symbol)

    async def publish(self, symbol: str, price: float):
        """Publish one tick to all current subscribers. Used by runtime and tests."""
        async with self._lock:
            self._latest_prices[symbol] = float(price)
            self._latest_update_ts[symbol] = self._now_fn()
            queues = list(self._subscribers.get(symbol, set()))
        for queue in queues:
            await self._offer(queue, float(price))

    async def _offer(self, queue: asyncio.Queue, price: float):
        """Keep only the latest tick so slow consumers do not build lag."""
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await queue.put(price)

    async def _send_subscribe(self, symbol: str):
        ws_symbol = symbol_to_ws_symbol(symbol)
        if not self._ws or ws_symbol in self._subscribed_ws_symbols:
            return
        await self._ws.send(json.dumps({"op": "subscribe", "args": [f"tickers.{ws_symbol}"]}))
        self._subscribed_ws_symbols.add(ws_symbol)
        logger.info(f"📡 Price bus subscribed: {symbol}")

    async def _send_unsubscribe(self, symbol: str):
        ws_symbol = symbol_to_ws_symbol(symbol)
        if not self._ws or ws_symbol not in self._subscribed_ws_symbols:
            return
        await self._ws.send(json.dumps({"op": "unsubscribe", "args": [f"tickers.{ws_symbol}"]}))
        self._subscribed_ws_symbols.discard(ws_symbol)
        logger.info(f"📡 Price bus unsubscribed: {symbol}")

    async def _run(self):
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._health_status["ws_connected"] = True
                    self._subscribed_ws_symbols.clear()
                    for symbol in sorted(self.active_symbols):
                        await self._send_subscribe(symbol)

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                            self._health_status["last_successful_recv"] = self._now_fn()
                            await self._handle_message(msg)
                        except asyncio.TimeoutError:
                            # No message received in 30 seconds, check connection
                            logger.warning("📡 Price bus websocket timeout, checking connection...")
                            try:
                                pong_waiter = await ws.ping()
                                await asyncio.wait_for(pong_waiter, timeout=10.0)
                            except Exception:
                                logger.error("📡 Price bus websocket ping failed, reconnecting")
                                break
                        except Exception as e:
                            logger.error(f"📡 Price bus websocket recv error: {e}")
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._health_status["last_error"] = str(e)
                self._health_status["reconnect_count"] += 1
                self._health_status["ws_connected"] = False
                if self._running:
                    logger.error(f"📡 Price bus websocket error: {e}; reconnecting in 3s")
                    await asyncio.sleep(3)
            finally:
                self._ws = None
                self._health_status["ws_connected"] = False
                self._subscribed_ws_symbols.clear()

    async def _handle_message(self, msg: str):
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return

        topic = data.get("topic", "")
        payload = data.get("data", {})
        if not topic.startswith("tickers.") or "lastPrice" not in payload:
            return

        ws_symbol = topic.split(".", 1)[1]
        symbol = ws_symbol_to_symbol(ws_symbol)
        try:
            price = float(payload["lastPrice"])
        except (TypeError, ValueError):
            return
        await self.publish(symbol, price)