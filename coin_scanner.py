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
    BYBIT_API_KEY,
    BYBIT_API_SECRET,
    TRADING_MODE,
    MIN_24H_VOLUME_USDT,
    SCAN_TOP_N_COINS,
    MAX_24H_RANGE_PCT,
    MIN_24H_RANGE_PCT,
    MAX_ATR_PCT,
    MIN_MEAN_REVERSION,
    COIN_BLACKLIST,
    MAX_SCANNER_LEVERAGE,
    SCANNER_SYMBOL_DELAY_MS,
    SCANNER_RATE_LIMIT_RETRIES,
    SCANNER_RATE_LIMIT_BACKOFF_SECONDS,
    clamp_leverage,
)
from entry_shape_planner import compute_entry_quality, plan_entry_shape
from scanner_learning import ScannerLearning

logger = logging.getLogger("coin_scanner")


@dataclass
class CoinScore:
    symbol: str
    price: float
    high_24h: float
    low_24h: float
    volume_24h_usdt: float
    atr_pct: float  # ATR as % of price
    range_pct: float  # 24h range as % of price
    mean_reversion_score: float  # 0-1, higher = more mean-reverting
    grid_score: float  # composite score
    suggested_upper: float
    suggested_lower: float
    suggested_grids: int
    suggested_leverage: int
    trend_direction: str = "neutral"  # dynamic from OHLCV slope
    market_regime: str = "ranging"
    entry_quality_score: float = 0.0
    range_position: float = 0.5
    vwap_distance_pct: float = 0.0
    pullback_depth_pct: float = 0.0
    slope_score: float = 0.0
    acceleration_score: float = 0.0
    htf_slope_score: float = 0.0
    ltf_slope_score: float = 0.0
    mtf_alignment_score: float = 0.0
    entry_shape_template: str = "atr_box"
    entry_shape_spacing: str = "balanced"
    entry_buy_density_bias: float = 0.5
    entry_sell_density_bias: float = 0.5
    entry_shape_notes: str = ""


class CoinScanner:
    """Scans Bybit linear perpetuals to find the best coins for grid trading."""

    def __init__(self, learning: ScannerLearning | None = None):
        exchange_opts = {
            "apiKey": BYBIT_API_KEY,
            "secret": BYBIT_API_SECRET,
            "enableRateLimit": True,
        }
        if TRADING_MODE == "testnet":
            self.exchange = ccxt.bybit(
                {
                    **exchange_opts,
                    "options": {"defaultType": "linear"},
                    "urls": {
                        "api": {
                            "public": "https://api-testnet.bybit.com",
                            "private": "https://api-testnet.bybit.com",
                        }
                    },
                }
            )
        else:
            self.exchange = ccxt.bybit({**exchange_opts, "options": {"defaultType": "linear"}})
        self.learning = learning or ScannerLearning()

    async def close(self):
        if getattr(self, "exchange", None) is not None:
            await self.exchange.close()

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "rate limit",
                "too many visits",
                "retcode: 10006",
                'retcode\":10006',
                "request timeout",
                "too many requests",
            )
        )

    async def fetch_tickers(self) -> list[dict]:
        """Get all linear perpetual tickers from Bybit."""
        logger.info("Fetching linear perpetual tickers...")
        tickers = await self.exchange.fetch_tickers()
        linear = [
            t
            for t in tickers.values()
            if t["symbol"].endswith("/USDT:USDT")
            and t.get("quoteVolume", 0) >= MIN_24H_VOLUME_USDT
        ]
        logger.info(f"Found {len(linear)} USDT linear perps with vol >= ${MIN_24H_VOLUME_USDT:,.0f}")
        linear.sort(key=lambda x: x.get("quoteVolume", 0), reverse=True)
        return linear[:SCAN_TOP_N_COINS]

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 96) -> pd.DataFrame:
        """Fetch OHLCV data. 96 x 15m = 24 hours."""
        attempts = max(1, SCANNER_RATE_LIMIT_RETRIES)
        for attempt in range(1, attempts + 1):
            try:
                ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                return df
            except Exception as exc:
                if not self._is_rate_limit_error(exc) or attempt >= attempts:
                    raise
                backoff = SCANNER_RATE_LIMIT_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Rate limit while fetching %s (attempt %s/%s). Backing off %.1fs",
                    symbol,
                    attempt,
                    attempts,
                    backoff,
                )
                await asyncio.sleep(backoff)

    async def fetch_symbol_frames(self, symbol: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Fetch short, medium, and higher timeframe candles for regime/entry scoring."""
        df_5m, df_15m, df_1h = await asyncio.gather(
            self.fetch_ohlcv(symbol, timeframe="5m", limit=72),
            self.fetch_ohlcv(symbol, timeframe="15m", limit=96),
            self.fetch_ohlcv(symbol, timeframe="1h", limit=72),
        )
        return df_5m, df_15m, df_1h

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
        return float(atr)

    def _mean_reversion_score(self, df: pd.DataFrame) -> float:
        """
        Score how mean-reverting the price is (0-1).
        Uses SMA crossing frequency and trend slope penalty.
        """
        close = df["close"]
        sma = close.rolling(20).mean()
        above = close > sma
        crosses = (above != above.shift(1)).sum()
        cross_score = min(crosses / 10.0, 1.0)
        returns = close.pct_change().dropna()
        if len(returns) > 10:
            slope = np.polyfit(range(len(returns)), returns.cumsum().values, 1)[0]
            trend_score = max(0, 1 - abs(slope) * 100)
        else:
            trend_score = 0.5
        return float(cross_score * 0.6 + trend_score * 0.4)

    def _range_position(self, current_price: float, high_lookback: float, low_lookback: float) -> float:
        span = max(high_lookback - low_lookback, 1e-9)
        position = (current_price - low_lookback) / span
        return float(min(1.0, max(0.0, position)))

    def _rolling_vwap(self, df: pd.DataFrame) -> float:
        volume = df["volume"].astype(float)
        close = df["close"].astype(float)
        total_volume = float(volume.sum())
        if total_volume <= 0:
            return float(close.iloc[-1])
        return float((close * volume).sum() / total_volume)

    def _vwap_distance_pct(self, current_price: float, vwap_price: float) -> float:
        base = max(abs(vwap_price), 1e-9)
        return float(((current_price - vwap_price) / base) * 100.0)

    def _pullback_depth_pct(self, current_price: float, swing_extreme: float) -> float:
        base = max(abs(swing_extreme), 1e-9)
        return float((abs(current_price - swing_extreme) / base) * 100.0)

    def _slope_score(self, df: pd.DataFrame) -> float:
        close = df["close"].astype(float)
        if len(close) < 8:
            return 0.0
        returns = close.pct_change().dropna()
        if len(returns) < 6:
            return 0.0
        return float(np.polyfit(range(len(returns)), returns.cumsum().values, 1)[0])

    @staticmethod
    def _alignment_score(*slopes: float) -> float:
        clean = [float(s) for s in slopes if abs(float(s)) > 1e-9]
        if not clean:
            return 0.0
        same_sign = all(s > 0 for s in clean) or all(s < 0 for s in clean)
        strength = min(sum(abs(s) for s in clean) / (0.0003 * len(clean)), 1.0)
        return float(strength if same_sign else -strength * 0.5)

    def _classify_market_regime(
        self,
        mr_score: float,
        slope: float,
        atr_pct: float,
        range_position: float,
        htf_slope: float | None = None,
        ltf_slope: float | None = None,
        m15_mr_score: float | None = None,
        alignment_score: float | None = None,
    ) -> str:
        htf = float(htf_slope if htf_slope is not None else slope)
        ltf = float(ltf_slope if ltf_slope is not None else slope)
        effective_mr = float(max(mr_score, m15_mr_score if m15_mr_score is not None else mr_score))
        align = float(alignment_score if alignment_score is not None else self._alignment_score(htf, slope, ltf))

        if atr_pct >= 3.25 and abs(align) >= 0.55:
            return "volatile"
        if effective_mr >= 0.68 and 0.15 <= range_position <= 0.85 and abs(htf) <= 0.00022 and abs(slope) <= 0.00025:
            return "ranging"
        if htf >= 0.00022 and (slope >= 0.00008 or align >= 0.35):
            return "trending_up"
        if htf <= -0.00022 and (slope <= -0.00008 or align >= 0.35):
            return "trending_down"
        if atr_pct >= 3.0:
            return "volatile"
        if effective_mr >= 0.62:
            return "ranging"
        if htf > 0 or (htf >= 0 and range_position <= 0.45 and ltf > -0.00015):
            return "trending_up"
        if htf < 0 or (htf <= 0 and range_position >= 0.55 and ltf < 0.00015):
            return "trending_down"
        return "ranging"

    def _score_coin(self, ticker: dict, df: pd.DataFrame, df_1h: pd.DataFrame | None = None, df_5m: pd.DataFrame | None = None) -> CoinScore:
        """Calculate composite grid score for a coin."""
        price = float(ticker["last"])
        high_24h = float(ticker["high"])
        low_24h = float(ticker["low"])
        volume = float(ticker.get("quoteVolume", 0))
        range_pct = (high_24h - low_24h) / max(price, 1e-9) * 100

        atr = self._calculate_atr(df)
        atr_pct = atr / max(price, 1e-9) * 100
        mr_score = self._mean_reversion_score(df)

        close = df["close"]
        returns = close.pct_change().dropna()
        slope = self._slope_score(df)
        htf_df = df_1h if df_1h is not None else df
        ltf_df = df_5m if df_5m is not None else df
        htf_slope = self._slope_score(htf_df)
        ltf_slope = self._slope_score(ltf_df)
        alignment_score = self._alignment_score(htf_slope, slope, ltf_slope)

        high_lookback = float(df["high"].max())
        low_lookback = float(df["low"].min())
        range_position = self._range_position(price, high_lookback, low_lookback)
        vwap_price = self._rolling_vwap(df)
        vwap_distance_pct = self._vwap_distance_pct(price, vwap_price)
        htf_high = float(htf_df["high"].max())
        htf_low = float(htf_df["low"].min())
        provisional_direction = "short" if htf_slope < -0.0002 else "long"
        swing_extreme = htf_high if provisional_direction != "short" else htf_low
        pullback_depth_pct = self._pullback_depth_pct(price, swing_extreme)
        acceleration_score = float(returns.diff().iloc[-1]) if len(returns) > 1 else 0.0
        market_regime = self._classify_market_regime(
            mr_score=mr_score,
            slope=slope,
            atr_pct=atr_pct,
            range_position=range_position,
            htf_slope=htf_slope,
            ltf_slope=ltf_slope,
            m15_mr_score=mr_score,
            alignment_score=alignment_score,
        )
        if market_regime == "trending_up":
            trend_direction = "long"
        elif market_regime == "trending_down":
            trend_direction = "short"
        else:
            trend_direction = "neutral"
        entry_quality_score = compute_entry_quality(
            market_regime=market_regime,
            range_position=range_position,
            vwap_distance_pct=vwap_distance_pct,
            pullback_depth_pct=pullback_depth_pct,
            atr_pct=atr_pct,
        )

        range_score = max(0, 1 - abs(range_pct - 4.0) / 4.0)
        atr_score = max(0, 1 - abs(atr_pct - 1.5) / 1.5)
        vol_score = min(1.0, np.log10(max(volume, 1)) / 9.0)
        grid_score = (
            range_score * 0.30
            + atr_score * 0.20
            + vol_score * 0.15
            + mr_score * 0.35
        )
        directional_bonus = 0.0
        if market_regime in {"trending_up", "trending_down"}:
            directional_bonus = max(0.0, alignment_score) * 0.08 + min(pullback_depth_pct / 3.0, 0.06)
        grid_score = grid_score * 0.62 + entry_quality_score * 0.30 + directional_bonus

        entry_plan = plan_entry_shape(
            current_price=price,
            market_regime=market_regime,
            atr=atr,
            swing_high=high_lookback,
            swing_low=low_lookback,
            range_position=range_position,
            vwap_price=vwap_price,
            pullback_depth_pct=pullback_depth_pct,
        )

        atr_based_leverage = int(90 / (atr_pct + 0.5))
        suggested_leverage = clamp_leverage(atr_based_leverage, maximum=MAX_SCANNER_LEVERAGE)

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
            suggested_upper=round(entry_plan.upper, 4),
            suggested_lower=round(entry_plan.lower, 4),
            suggested_grids=entry_plan.num_grids,
            suggested_leverage=suggested_leverage,
            trend_direction=trend_direction,
            market_regime=market_regime,
            entry_quality_score=round(entry_quality_score, 4),
            range_position=round(range_position, 4),
            vwap_distance_pct=round(vwap_distance_pct, 4),
            pullback_depth_pct=round(pullback_depth_pct, 4),
            slope_score=round(float(slope), 6),
            acceleration_score=round(float(acceleration_score), 6),
            htf_slope_score=round(float(htf_slope), 6),
            ltf_slope_score=round(float(ltf_slope), 6),
            mtf_alignment_score=round(float(alignment_score), 4),
            entry_shape_template=entry_plan.template_name,
            entry_shape_spacing=entry_plan.spacing_mode,
            entry_buy_density_bias=round(entry_plan.buy_density_bias, 4),
            entry_sell_density_bias=round(entry_plan.sell_density_bias, 4),
            entry_shape_notes=entry_plan.notes,
        )

    def _is_blacklisted(self, symbol: str) -> bool:
        """Check if a symbol contains any blacklisted token name."""
        big_cap_blacklist = set()
        blacklist = [b.strip().upper() for b in COIN_BLACKLIST.split(",") if b.strip()]
        blacklist = sorted(set(blacklist).union(big_cap_blacklist))
        base = symbol.replace("/USDT:USDT", "").replace("/USDT", "").upper()
        for token in blacklist:
            if token == base or base.startswith(token):
                return True
        return False

    def _passes_safety_filters(self, score: CoinScore) -> tuple[bool, str]:
        """Apply all safety filters. Returns (passes, reason)."""
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
                df_5m, df_15m, df_1h = await self.fetch_symbol_frames(symbol)
                score = self._score_coin(ticker, df_15m, df_1h=df_1h, df_5m=df_5m)

                passes, reason = self._passes_safety_filters(score)
                if passes:
                    raw_scores.append(score)
                    logger.info(
                        f"  ✅ {symbol}: score={score.grid_score:.3f} range={score.range_pct:.2f}% "
                        f"atr={score.atr_pct:.2f}% mr={score.mean_reversion_score:.2f} align={score.mtf_alignment_score:+.2f}"
                    )
                else:
                    filtered_out.append((symbol, reason))
                    logger.info(f"  🚫 {symbol}: FILTERED — {reason}")
            except Exception as e:
                logger.warning(f"  {symbol}: skipped ({e})")
                continue
            finally:
                if SCANNER_SYMBOL_DELAY_MS > 0:
                    await asyncio.sleep(SCANNER_SYMBOL_DELAY_MS / 1000.0)

        raw_scores = self.learning.rank_candidates(raw_scores)

        logger.info(f"\n📋 Scan summary: {len(raw_scores)} passed, {len(filtered_out)} filtered out")
        if filtered_out:
            logger.info("   Filtered:")
            for sym, reason in filtered_out[:10]:
                logger.info(f"     🚫 {sym}: {reason}")

        logger.info(f"\n🏆 Top {min(5, len(raw_scores))} coins:")
        for i, s in enumerate(raw_scores[:5]):
            logger.info(
                f"  {i+1}. {s.symbol} | score={s.grid_score:.3f} | learning={getattr(s, 'learning_score', 0):+.3f} "
                f"| range={s.range_pct:.2f}% | grids={s.suggested_grids} | lev={s.suggested_leverage}x"
            )

        return raw_scores

    async def get_best_coin(self) -> Optional[CoinScore]:
        """Scan and return the single best coin for grid trading."""
        scores = await self.scan()
        return scores[0] if scores else None
