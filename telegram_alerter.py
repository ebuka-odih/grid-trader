"""
Telegram Alerter — sends trade notifications via Telegram Bot API.
Used for: trade opened, target hit, drawdown warning, periodic status.
"""

import asyncio
import logging
from typing import Optional

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("telegram_alerter")


class TelegramAlerter:
    """Sends formatted trade alerts to Telegram."""

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)
        if not self.enabled:
            logger.warning("Telegram alerts disabled — no bot token or chat ID configured")

    async def send(self, text: str, parse_mode: str = "HTML"):
        """Send a message to Telegram (async wrapper)."""
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: requests.post(url, json=payload, timeout=10)
            )
            if resp.status_code != 200:
                logger.warning(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")

    # ── Formatted Alerts ───────────────────────────────────────

    async def alert_grid_opened(self, symbol: str, upper: float, lower: float,
                                 grids: int, leverage: int, score: float):
        await self.send(
            f"🟢 <b>Grid Opened</b>\n"
            f"📊 {symbol}\n"
            f"📈 Range: ${lower:.4f} — ${upper:.4f}\n"
            f"🔲 Grids: {grids} | ⚡ Leverage: {leverage}x\n"
            f"⭐ Score: {score:.3f}"
        )

    async def alert_target_hit(self, symbol: str, total_pnl: float,
                                realized: float, fills: int):
        await self.send(
            f"🎯 <b>Target PnL Hit!</b>\n"
            f"📊 {symbol}\n"
            f"💰 Total PnL: <code>${total_pnl:.4f}</code>\n"
            f"✅ Realized: ${realized:.4f}\n"
            f"🔢 Fills: {fills}"
        )

    async def alert_drawdown(self, symbol: str, pnl: float, max_dd: float):
        await self.send(
            f"⚠️ <b>Drawdown Warning</b>\n"
            f"📊 {symbol}\n"
            f"📉 PnL: <code>${pnl:.4f}</code>\n"
            f"🛑 Max Drawdown: ${max_dd:.2f}"
        )

    async def alert_grid_closed(self, symbol: str, total_pnl: float, reason: str):
        emoji = "✅" if total_pnl > 0 else "❌"
        await self.send(
            f"{emoji} <b>Grid Closed</b>\n"
            f"📊 {symbol}\n"
            f"💰 Final PnL: <code>${total_pnl:.4f}</code>\n"
            f"📋 Reason: {reason}"
        )

    async def alert_status(self, status: dict):
        """Send periodic status update."""
        pnl = status.get("total_pnl", 0)
        emoji = "🟢" if pnl >= 0 else "🔴"
        await self.send(
            f"{emoji} <b>Status Update</b>\n"
            f"📊 {status.get('symbol', 'N/A')}\n"
            f"💲 Price: {status.get('current_price', 0):.4f}\n"
            f"💰 PnL: <code>${pnl:.4f}</code>\n"
            f"📦 Position: {status.get('position_qty', 0)}\n"
            f"🔢 Fills: {status.get('fills', 0)}\n"
            f"🎯 Target: ${status.get('target_pnl_low', 0)}-${status.get('target_pnl_high', 0)}"
        )

    async def alert_scan_results(self, top_coins: list):
        """Send top coin scan results."""
        lines = ["🔍 <b>Coin Scan Results</b>\n"]
        for i, coin in enumerate(top_coins[:5]):
            lines.append(
                f"{i+1}. {coin.symbol}\n"
                f"   Score: {coin.grid_score:.3f} | Range: {coin.range_pct:.2f}%\n"
                f"   Grids: {coin.suggested_grids} | Lev: {coin.suggested_leverage}x"
            )
        await self.send("\n".join(lines))
