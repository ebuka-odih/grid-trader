"""
Coin Scanner Agent — scores and ranks coins for grid trading.
Uses Bybit REST API (via ccxt) to fetch market data, then:
  1. Filters by 24h volume
  2. Calculates volatility (ATR-based range ratio)
  3. Scores on: range suitability + volume + mean-reversion tendency
  4. Returns top N coins with suggested grid params
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd

from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, TRADING_MODE,
    MIN_24H_VOLUME_USDT, SCAN_TOP_N_COINS,
    MAX_24H_RANGE_PCT, MIN_24H_RANGE_PCT,
    MAX_ATR_PCT, MIN_MEAN_REVERSION, COIN_BLACKLIST,
    MAX_SCANNER_LEVERAGE, MIN_SAFE_LEVERAGE,
)

from scanner_learning import ScannerLearning

logger = logging.getLogger("coin_scanner")


@dataclass
class CoinScore:
    symbol: str
    price: float
    high_24h: float
    low_24h: float
    volume_24h_usdt: float
    atr_pct: float           # ATR as % of price
    range_pct: float          # 24h range as % of price
    mean_reversion_score: float  # 0-1, higher = more mean-reverting
    grid_score: float         # composite score
    suggested_upper: float
    suggested_lower: float
    suggested_grids: int
    suggested_leverage: int
    trend_direction: str = "neutral"  # "long", "short", or "neutral" — dynamic from OHLCV slope


class CoinScanner:
    """Scans Bybit linear perpetuals to find the best coins for grid trading."""

    def __init__(self, learning: ScannerLearning | None = None):
        exchange_opts = {
            "apiKey": BYBIT_API_KEY,
            "secret": BYBIT_API_SECRET,
            "enableRateLimit": True,
        }
        if TRADING_MODE == "testnet":
            self.exchange = ccxt.bybit({
                **exchange_opts,
                "options": {"defaultType": "linear"},
                "urls": {"api": {"public": "https://api-testnet.bybit.com", "private": "https://api-testnet.bybit.com"}},
            })
        else:
            self.exchange = ccxt.bybit({**exchange_opts, "options": {"defaultType": "linear"}})
        self.learning = learning or ScannerLearning()

    async def close(self):
        await self.exchange.close()

    # ── Step 1: Fetch all linear perpetual tickers ─────────────

    async def fetch_tickers(self) -> list[dict]:
        """Get all linear perpetual tickers from Bybit."""
        logger.info("Fetching linear perpetual tickers...")
        tickers = await self.exchange.fetch_tickers()
        # Filter to USDT linear perpetuals only
        linear = [
            t for t in tickers.values()
            if t["symbol"].endswith("/USDT:USDT")
            and t.get("quoteVolume", 0) >= MIN_24H_VOLUME_USDT
        ]
        logger.info(f"Found {len(linear)} USDT linear perps with vol >= ${MIN_24H_VOLUME_USDT:,.0f}")
        # Sort by volume descending
        linear.sort(key=lambda x: x.get("quoteVolume", 0), reverse=True)
        return linear[:SCAN_TOP_N_COINS]

    # ── Step 2: Fetch OHLCV for ATR + mean-reversion calc ─────

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 96) -> pd.DataFrame:
        """Fetch OHLCV data. 96 x 15m = 24 hours."""
        ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    # ── Step 3: Score a single coin ────────────────────────────

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate Average True Range."""
        high = df["high"]
        low = df["low"]
        close = df["close"]
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        return atr

    def _mean_reversion_score(self, df: pd.DataFrame) -> float:
        """
        Score how mean-reverting the price is (0-1).
        Uses Hurst exponent approximation: lower = more mean-reverting.
        Also checks how often price crosses its SMA (more crosses = better for grid).
        """
        close = df["close"]
        sma = close.rolling(20).mean()
        # Count SMA crosses in last 96 candles
        above = close > sma
        crosses = (above != above.shift(1)).sum()
        # Normalize: 10+ crosses in 96 candles = good mean reversion
        cross_score = min(crosses / 10.0, 1.0)
        # Also penalize if trend is too strong (linear regression slope)
        returns = close.pct_change().dropna()
        if len(returns) > 10:
            slope = np.polyfit(range(len(returns)), returns.cumsum().values, 1)[0]
            trend_score = max(0, 1 - abs(slope) * 100)  # lower slope = better
        else:
            trend_score = 0.5
        return cross_score * 0.6 + trend_score * 0.4

    def _score_coin(self, ticker: dict, df: pd.DataFrame) -> CoinScore:
        """Calculate composite grid score for a coin."""
        price = ticker["last"]
        high_24h = ticker["high"]
        low_24h = ticker["low"]
        volume = ticker.get("quoteVolume", 0)
        range_pct = (high_24h - low_24h) / price * 100

        atr = self._calculate_atr(df)
        atr_pct = atr / price * 100

        mr_score = self._mean_reversion_score(df)

        # Determine trend direction from OHLCV slope
        # Grid trading profits from oscillation (neutral), but when a clear trend
        # exists, bias grid levels to accumulate in the trend direction.
        close = df["close"]
        returns = close.pct_change().dropna()
        if len(returns) > 10:
            slope = np.polyfit(range(len(returns)), returns.cumsum().values, 1)[0]
        else:
            slope = 0.0
        # Strong trend threshold — only bias if slope is significant AND
        # mean reversion is weak (coin is trending, not ranging)
        STRONG_TREND_SLOPE = 0.0003
        if mr_score < 0.6 and slope > STRONG_TREND_SLOPE:
            trend_direction = "long"
        elif mr_score < 0.6 and slope < -STRONG_TREND_SLOPE:
            trend_direction = "short"
        else:
            trend_direction = "neutral"

        # Composite score:
        # - Range 1-8% is ideal for grid (too narrow = no profit, too wide = risky)
        range_score = max(0, 1 - abs(range_pct - 4.0) / 4.0)
        # - ATR 0.3-3% is ideal
        atr_score = max(0, 1 - abs(atr_pct - 1.5) / 1.5)
        # - Volume: more is better (log scale)
        vol_score = min(1.0, np.log10(max(volume, 1)) / 9.0)  # 1B = 1.0
        # - Mean reversion: higher is better
        # - Grid score = weighted composite
        grid_score = (
            range_score * 0.30
            + atr_score * 0.20
            + vol_score * 0.15
            + mr_score * 0.35
        )

        # Suggest grid parameters based on volatility
        # Upper/lower: 2x ATR from current price
        suggested_upper = price + atr * 2
        suggested_lower = price - atr * 2
        # Grids: exchange-style scalping uses one dense grid per coin.
        # Clamp to 10–20 internal levels so we do not deploy low-density 5–8 level grids.
        suggested_grids = max(10, min(20, int(range_pct / 0.5)))
        # Leverage: high-frequency cross-margin policy.
        # Margin stays capped separately at 2%; leverage controls movement/quantity.
        atr_based_leverage = int(90 / (atr_pct + 0.5))
        suggested_leverage = max(MIN_SAFE_LEVERAGE, min(MAX_SCANNER_LEVERAGE, atr_based_leverage))

        return CoinScore(
            symbol=ticker["symbol"],
            price=price,
            high_24h=high_24h,
            low_24h=low_24h,
            volume_24h_usdt=volume,
            atr_pct=round(atr_pct, 4),
            range_pct=round(range_pct, 4),
            mean_reversion_score=round(mr_score, 4),
            grid_score=round(grid_score, 4),
            suggested_upper=round(suggested_upper, 4),
            suggested_lower=round(suggested_lower, 4),
            suggested_grids=suggested_grids,
            suggested_leverage=suggested_leverage,
            trend_direction=trend_direction,
        )

    # ── Step 4: Safety Filters ─────────────────────────────────

    def _is_blacklisted(self, symbol: str) -> bool:
        """Check if a symbol contains any blacklisted token name."""
        big_cap_blacklist = set()  # No hardcoded blacklist — all coins tradeable
        blacklist = [b.strip().upper() for b in COIN_BLACKLIST.split(",") if b.strip()]
        blacklist = sorted(set(blacklist).union(big_cap_blacklist))
        base = symbol.replace("/USDT:USDT", "").replace("/USDT", "").upper()
        for token in blacklist:
            if token == base or base.startswith(token):
                return True
        return False

    def _passes_safety_filters(self, score: CoinScore) -> tuple[bool, str]:
        """
        Apply all safety filters. Returns (passes, reason).
        """
        if self._is_blacklisted(score.symbol):
            return False, f"blacklisted ({score.symbol})"
        if score.range_pct > MAX_24H_RANGE_PCT:
            return False, f"range too wide ({score.range_pct:.1f}% > {MAX_24H_RANGE_PCT}%)"
        if score.range_pct < MIN_24H_RANGE_PCT:
            return False, f"range too narrow ({score.range_pct:.1f}% < {MIN_24H_RANGE_PCT}%)"
        if score.atr_pct > MAX_ATR_PCT:
            return False, f"ATR too high ({score.atr_pct:.1f}% > {MAX_ATR_PCT}%)"
        if score.mean_reversion_score < MIN_MEAN_REVERSION:
            return False, f"not mean-reverting enough (mr={score.mean_reversion_score:.2f} < {MIN_MEAN_REVERSION})"
        return True, "passed"

    # ── Step 5: Full scan ──────────────────────────────────────

    async def scan(self) -> list[CoinScore]:
        """
        Run a full scan: fetch tickers, score each coin, apply safety filters, return ranked results.
        """
        tickers = await self.fetch_tickers()
        raw_scores: list[CoinScore] = []
        filtered_out: list[tuple[str, str]] = []

        for ticker in tickers:
            symbol = ticker["symbol"]
            try:
                df = await self.fetch_ohlcv(symbol)
                score = self._score_coin(ticker, df)

                # Apply safety filters
                passes, reason = self._passes_safety_filters(score)
                if passes:
                    raw_scores.append(score)
                    logger.info(f"  ✅ {symbol}: score={score.grid_score:.3f} range={score.range_pct:.2f}% atr={score.atr_pct:.2f}% mr={score.mean_reversion_score:.2f}")
                else:
                    filtered_out.append((symbol, reason))
                    logger.info(f"  🚫 {symbol}: FILTERED — {reason}")
            except Exception as e:
                logger.warning(f"  {symbol}: skipped ({e})")
                continue

        # Apply adaptive learning penalties/bonuses, then sort by final score.
        raw_scores = self.learning.rank_candidates(raw_scores)

        logger.info(f"\n📋 Scan summary: {len(raw_scores)} passed, {len(filtered_out)} filtered out")
        if filtered_out:
            logger.info("   Filtered:")
            for sym, reason in filtered_out[:10]:
                logger.info(f"     🚫 {sym}: {reason}")

        logger.info(f"\n🏆 Top {min(5, len(raw_scores))} coins:")
        for i, s in enumerate(raw_scores[:5]):
            logger.info(f"  {i+1}. {s.symbol} | score={s.grid_score:.3f} | learning={getattr(s, 'learning_score', 0):+.3f} | range={s.range_pct:.2f}% | grids={s.suggested_grids} | lev={s.suggested_leverage}x")

        return raw_scores

    async def get_best_coin(self) -> Optional[CoinScore]:
        """Scan and return the single best coin for grid trading."""
        scores = await self.scan()
        return scores[0] if scores else None
