"""
Backtest Engine v3 — Compare v2 (uniform) vs v3 (adaptive) grid strategies.

Fetches historical kline data from Bybit and simulates grid trading
with both approaches on the same price series.

Usage:
    python backtest_engine.py                    # Default: BTCUSDT 4h, last 7 days
    python backtest_engine.py --symbol ETHUSDT   # Specific symbol
    python backtest_engine.py --days 30          # Last 30 days
    python backtest_engine.py --run-all          # Run all configured pairs
"""

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adaptive_grid import AdaptiveGrid, AdaptiveConfig, default_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backtest")


# ── Historical Data Fetcher ────────────────────────────────────

def fetch_klines(
    symbol: str = "BTCUSDT",
    interval: str = "5",       # 5-minute candles
    days: int = 7,
    category: str = "linear",
) -> list[dict]:
    """
    Fetch historical klines from Bybit v5 API.
    Returns list of {timestamp, open, high, low, close, volume}.
    """
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    
    all_klines = []
    current_start = start_ms
    
    logger.info(f"📊 Fetching {symbol} {interval}m klines for {days} days...")
    
    while current_start < end_ms:
        url = (
            f"https://api.bybit.com/v5/market/kline"
            f"?category={category}&symbol={symbol}"
            f"&interval={interval}&start={current_start}&end={end_ms}&limit=1000"
        )
        
        try:
            req = Request(url, headers={"User-Agent": "GridBacktest/1.0"})
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except (URLError, json.JSONDecodeError) as e:
            logger.error(f"❌ API error: {e}")
            break
        
        if data.get("retCode") != 0:
            logger.error(f"❌ Bybit error: {data.get('retMsg')}")
            break
        
        klines = data.get("result", {}).get("list", [])
        if not klines:
            break
        
        for k in klines:
            all_klines.append({
                "timestamp": int(k[0]) / 1000,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        
        # Move start forward
        last_ts = int(klines[-1][0])
        if last_ts <= current_start:
            break
        current_start = last_ts + 1
        
        time.sleep(0.1)  # Rate limit
    
    # Sort by timestamp
    all_klines.sort(key=lambda x: x["timestamp"])
    
    logger.info(f"✅ Fetched {len(all_klines)} candles "
                f"({all_klines[0]['timestamp']:.0f} → {all_klines[-1]['timestamp']:.0f})")
    
    return all_klines


def klines_to_ticks(klines: list[dict], ticks_per_candle: int = 5) -> list[tuple[float, float]]:
    """
    Convert OHLC candles to synthetic tick data.
    Each candle generates `ticks_per_candle` price points.
    Returns [(timestamp, price), ...]
    """
    ticks = []
    for k in klines:
        ts = k["timestamp"]
        o, h, l, c = k["open"], k["high"], k["low"], k["close"]
        
        # Generate synthetic path: open → high/low → close
        if ticks_per_candle >= 4:
            # Full path: open → (high or low first) → other extreme → close
            if c > o:  # Bullish candle
                path = [o, l, h, c]
            else:  # Bearish candle
                path = [o, h, l, c]
            
            # Pad to ticks_per_candle
            while len(path) < ticks_per_candle:
                # Insert midpoints
                idx = len(path) // 2
                mid = (path[idx] + path[min(idx + 1, len(path) - 1)]) / 2
                path.insert(idx + 1, mid)
        else:
            path = [o, h, l, c][:ticks_per_candle]
        
        tick_interval = 300 / max(len(path), 1)  # 5min candle / N ticks
        for i, price in enumerate(path):
            ticks.append((ts + i * tick_interval, price))
    
    return ticks


# ── Grid Simulation ────────────────────────────────────────────

@dataclass
class GridConfig:
    """Grid simulation configuration."""
    symbol: str = "BTCUSDT"
    upper: float = 0.0
    lower: float = 0.0
    num_grids: int = 10
    leverage: int = 50
    order_size_usdt: float = 10.0
    
    # Close targets
    target_pnl_pct: float = 2.0       # Close at +2% of allocated margin
    max_drawdown_pct: float = 8.0     # Hold through 8% drawdown
    timeout_hours: float = 48.0       # Max grid lifetime


@dataclass
class SimLevel:
    """A simulated grid level."""
    index: int
    price: float
    side: str  # "Buy" or "Sell"
    qty: float
    status: str = "open"  # open, filled, rebalanced


@dataclass
class SimFill:
    """A simulated fill."""
    timestamp: float
    level_index: int
    side: str
    price: float
    qty: float
    pnl: float = 0.0


@dataclass
class GridResult:
    """Result of a single grid simulation."""
    strategy: str  # "v2" or "v3"
    symbol: str
    start_price: float
    end_price: float
    upper: float
    lower: float
    num_grids: int
    leverage: int
    
    # PnL
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    
    # Activity
    total_fills: int = 0
    buy_fills: int = 0
    sell_fills: int = 0
    recenters: int = 0
    spikes_detected: int = 0
    exposure_breaches: int = 0
    
    # Timing
    duration_hours: float = 0.0
    close_reason: str = "timeout"
    close_timestamp: float = 0.0
    
    # Fill history for analysis
    fills: list = field(default_factory=list)
    
    # Drawdown series for charting
    drawdown_series: list = field(default_factory=list)


def simulate_grid(
    ticks: list[tuple[float, float]],
    config: GridConfig,
    strategy: str = "v2",
    adaptive_config: AdaptiveConfig = None,
) -> GridResult:
    """
    Simulate grid trading on historical tick data.
    
    strategy="v2": Uniform sizing, no adaptive features
    strategy="v3": Exponential sizing, fast spike detection, recentering, trailing
    """
    is_v3 = strategy == "v3"
    
    # Calculate initial grid levels
    step = (config.upper - config.lower) / config.num_grids
    levels: list[SimLevel] = []
    
    for i in range(config.num_grids + 1):
        price_lvl = config.lower + step * i
        side = "Buy" if price_lvl < ticks[0][1] else "Sell"
        
        # v3: Exponential sizing
        if is_v3 and adaptive_config and adaptive_config.exp_sizing_enabled:
            dist = abs(price_lvl - ticks[0][1]) / max(config.upper - config.lower, 1e-8)
            dist = min(dist, 1.0)
            gamma = adaptive_config.exp_sizing_gamma
            factor = math.exp(-gamma * dist * 3)
            factor = max(adaptive_config.exp_sizing_min_factor, min(1.0, factor))
            order_size = config.order_size_usdt * factor
        else:
            order_size = config.order_size_usdt
        
        qty = (order_size * config.leverage) / price_lvl
        
        levels.append(SimLevel(
            index=i,
            price=price_lvl,
            side=side,
            qty=qty,
            status="open",
        ))
    
    # Initialize adaptive grid for v3
    adaptive = None
    if is_v3:
        cfg = adaptive_config or default_config()
        adaptive = AdaptiveGrid(
            config=cfg,
            upper=config.upper,
            lower=config.lower,
            num_grids=config.num_grids,
            base_order_size=config.order_size_usdt,
            leverage=config.leverage,
        )
    
    # Simulation state
    position_qty = 0.0
    position_side = ""
    entry_price = 0.0
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    fills: list[SimFill] = []
    filled_levels: set = set()
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    spikes_detected = 0
    exposure_breaches = 0
    recenters = 0
    
    start_time = ticks[0][0]
    close_reason = "timeout"
    closed_early = False
    close_price = ticks[-1][1]
    close_timestamp = ticks[-1][0]
    
    # Allocated margin for PnL targets
    allocated_margin = config.order_size_usdt * config.num_grids
    target_pnl = allocated_margin * config.target_pnl_pct / 100
    drawdown_limit = allocated_margin * config.max_drawdown_pct / 100
    
    # Process ticks
    old_price = ticks[0][1]
    
    for ts, price in ticks:
        pending_recenter = False
        pending_trail_shift = None

        # ── v3: Adaptive Processing ──────────────────────
        if adaptive:
            result = adaptive.on_price(price)
            
            if result.action == "pause":
                spikes_detected += 1
                old_price = price
                continue
            
            if result.action == "close_excess":
                if position_qty > 0 and entry_price > 0:
                    if position_side == "Buy":
                        unrealized_pnl = (price - entry_price) * position_qty
                    else:
                        unrealized_pnl = (entry_price - price) * position_qty
                else:
                    unrealized_pnl = 0.0
                exposure_breaches += 1
                total_pnl = realized_pnl + unrealized_pnl
                close_reason = "exposure_breach"
                close_price = price
                close_timestamp = ts
                closed_early = True
                break
            
            if result.action == "freeze":
                old_price = price
                continue
            
            pending_recenter = result.recentered
            if pending_recenter:
                recenters += 1
            
            pending_trail_shift = result.trail_shift
        
        # ── Fill Detection ───────────────────────────────
        for level in levels:
            if level.index in filled_levels:
                continue
            
            # Check exposure cap
            if adaptive and not adaptive.exposure_cap.fills_allowed():
                continue
            
            filled = False
            if level.side == "Buy" and old_price > level.price >= price:
                filled = True
            elif level.side == "Sell" and old_price < level.price <= price:
                filled = True
            
            if filled:
                filled_levels.add(level.index)
                level.status = "filled"
                
                # Record in adaptive
                if adaptive:
                    adaptive.record_fill(level.side, level.qty, level.index)
                
                # Calculate PnL
                fill_pnl = 0.0
                if position_qty > 0 and position_side != level.side:
                    if position_side == "Buy":
                        fill_pnl = (price - entry_price) * min(level.qty, position_qty)
                    else:
                        fill_pnl = (entry_price - price) * min(level.qty, position_qty)
                    realized_pnl += fill_pnl
                    position_qty -= level.qty
                    if position_qty <= 0:
                        position_qty = 0
                        entry_price = 0
                        position_side = ""
                else:
                    if position_qty > 0:
                        total_cost = entry_price * position_qty + price * level.qty
                        position_qty += level.qty
                        entry_price = total_cost / position_qty
                    else:
                        position_qty = level.qty
                        entry_price = price
                        position_side = level.side
                
                fills.append(SimFill(
                    timestamp=ts,
                    level_index=level.index,
                    side=level.side,
                    price=price,
                    qty=level.qty,
                    pnl=fill_pnl,
                ))
                
                # Rebalance level on opposite side
                level.status = "rebalanced"

        # ── Recenter/trail AFTER fills ───────────────────
        if adaptive:
            if pending_recenter:
                _handle_backtest_recenter(levels, filled_levels, adaptive, price, config)
            if pending_trail_shift is not None:
                _handle_backtest_trail(levels, filled_levels, adaptive, pending_trail_shift)

        # ── Update Unrealized PnL ────────────────────────
        if position_qty > 0 and entry_price > 0:
            if position_side == "Buy":
                unrealized_pnl = (price - entry_price) * position_qty
            else:
                unrealized_pnl = (entry_price - price) * position_qty
        else:
            unrealized_pnl = 0.0
        
        total_pnl = realized_pnl + unrealized_pnl
        
        # Track drawdown
        if total_pnl < 0:
            dd = abs(total_pnl)
            if dd > max_drawdown:
                max_drawdown = dd
                max_drawdown_pct = dd / max(allocated_margin, 1) * 100
        
        # Record drawdown series (every 100 ticks)
        if len(fills) > 0 and len(ticks) > 0:
            tick_idx = ticks.index((ts, price)) if (ts, price) in ticks else -1
            if tick_idx >= 0 and tick_idx % 100 == 0:
                elapsed_hours = (ts - start_time) / 3600
                # Store (elapsed_hours, total_pnl, drawdown_pct)
                pass  # We'll track this differently
        
        # ── Check Close Conditions ───────────────────────
        if total_pnl >= target_pnl and len(fills) >= 2:
            close_reason = "target_hit"
            close_price = price
            close_timestamp = ts
            closed_early = True
            break
        
        # Timeout check
        elapsed_hours = (ts - start_time) / 3600
        if elapsed_hours >= config.timeout_hours:
            close_reason = "timeout"
            close_price = price
            close_timestamp = ts
            closed_early = True
            break
        
        old_price = price
    
    # Final PnL uses the actual close tick when the grid exited early.
    final_price = close_price if closed_early else ticks[-1][1]
    final_timestamp = close_timestamp if closed_early else ticks[-1][0]
    if position_qty > 0 and entry_price > 0:
        if position_side == "Buy":
            unrealized_pnl = (final_price - entry_price) * position_qty
        else:
            unrealized_pnl = (entry_price - final_price) * position_qty
    else:
        unrealized_pnl = 0.0

    total_pnl = realized_pnl + unrealized_pnl
    duration_hours = (final_timestamp - start_time) / 3600
    
    # Count buy/sell fills
    buy_fills = sum(1 for f in fills if f.side == "Buy")
    sell_fills = sum(1 for f in fills if f.side == "Sell")
    
    return GridResult(
        strategy=strategy,
        symbol=config.symbol,
        start_price=ticks[0][1],
        end_price=final_price,
        upper=config.upper,
        lower=config.lower,
        num_grids=config.num_grids,
        leverage=config.leverage,
        realized_pnl=round(realized_pnl, 4),
        unrealized_pnl=round(unrealized_pnl, 4),
        total_pnl=round(total_pnl, 4),
        max_drawdown=round(max_drawdown, 4),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        total_fills=len(fills),
        buy_fills=buy_fills,
        sell_fills=sell_fills,
        recenters=recenters,
        spikes_detected=spikes_detected,
        exposure_breaches=exposure_breaches,
        duration_hours=round(duration_hours, 2),
        close_reason=close_reason,
        close_timestamp=final_timestamp,
        fills=fills,
    )


def _handle_backtest_recenter(
    levels: list[SimLevel],
    filled_levels: set,
    adaptive: AdaptiveGrid,
    price: float,
    config: GridConfig,
):
    """Handle grid recentering in backtest."""
    new_upper = adaptive.bounds.upper
    new_lower = adaptive.bounds.lower
    num_grids = adaptive.bounds.num_grids
    
    step = (new_upper - new_lower) / num_grids
    new_levels = []
    
    for i in range(num_grids + 1):
        price_lvl = new_lower + step * i
        if abs(price_lvl - price) < step * 0.3:
            continue
        
        side = "Buy" if price_lvl < price else "Sell"
        
        # Exponential sizing
        if adaptive.config.exp_sizing_enabled:
            dist = abs(price_lvl - price) / max(new_upper - new_lower, 1e-8)
            dist = min(dist, 1.0)
            factor = math.exp(-adaptive.config.exp_sizing_gamma * dist * 3)
            factor = max(adaptive.config.exp_sizing_min_factor, min(1.0, factor))
            order_size = config.order_size_usdt * factor
        else:
            order_size = config.order_size_usdt
        
        qty = (order_size * config.leverage) / price_lvl
        
        new_levels.append(SimLevel(
            index=i,
            price=price_lvl,
            side=side,
            qty=qty,
            status="open",
        ))
    
    # Replace levels
    levels.clear()
    levels.extend(new_levels)
    filled_levels.clear()
    adaptive.exposure_cap.reset()


def _handle_backtest_trail(
    levels: list[SimLevel],
    filled_levels: set,
    adaptive: AdaptiveGrid,
    shift: float,
):
    """Handle grid trailing in backtest."""
    for level in levels:
        level.price += shift
        level.status = "open"
    filled_levels.clear()
    adaptive.exposure_cap.reset()


# ── Report Generator ───────────────────────────────────────────

def generate_report(
    v2_result: GridResult,
    v3_result: GridResult,
    config: GridConfig,
) -> str:
    """Generate a comparison report."""
    
    def fmt_pnl(val):
        sign = "+" if val >= 0 else ""
        return f"{sign}${val:.2f}"
    
    def fmt_pct(val):
        return f"{val:.1f}%"
    
    # Determine winner
    v3_wins_pnl = v3_result.total_pnl > v2_result.total_pnl
    v3_wins_dd = v3_result.max_drawdown_pct < v2_result.max_drawdown_pct
    v3_wins_fills = v3_result.total_fills > v2_result.total_fills
    
    pnl_diff = v3_result.total_pnl - v2_result.total_pnl
    dd_diff = v2_result.max_drawdown_pct - v3_result.max_drawdown_pct
    
    report = []
    report.append("=" * 60)
    report.append(f"  BACKTEST REPORT: {config.symbol}")
    report.append(f"  Grid: {config.lower:.2f} - {config.upper:.2f} | "
                  f"{config.num_grids} levels | {config.leverage}x leverage")
    report.append(f"  Order size: ${config.order_size_usdt}/level | "
                  f"Allocated: ${config.order_size_usdt * config.num_grids:.0f}")
    report.append("=" * 60)
    report.append("")
    
    # Price info
    price_change = (v2_result.end_price - v2_result.start_price) / v2_result.start_price * 100
    report.append(f"  📈 Price: ${v2_result.start_price:.2f} → ${v2_result.end_price:.2f} ({price_change:+.2f}%)")
    report.append(f"  ⏱️  Duration: {v2_result.duration_hours:.1f} hours")
    report.append("")
    
    # Comparison table
    report.append("  ┌─────────────────────┬──────────────┬──────────────┬──────────┐")
    report.append("  │ Metric              │ v2 (uniform) │ v3 (adaptive)│ Δ Delta  │")
    report.append("  ├─────────────────────┼──────────────┼──────────────┼──────────┤")
    
    rows = [
        ("Total PnL", fmt_pnl(v2_result.total_pnl), fmt_pnl(v3_result.total_pnl), fmt_pnl(pnl_diff)),
        ("Realized PnL", fmt_pnl(v2_result.realized_pnl), fmt_pnl(v3_result.realized_pnl), ""),
        ("Unrealized PnL", fmt_pnl(v2_result.unrealized_pnl), fmt_pnl(v3_result.unrealized_pnl), ""),
        ("Max Drawdown", fmt_pct(v2_result.max_drawdown_pct), fmt_pct(v3_result.max_drawdown_pct), f"-{dd_diff:.1f}%"),
        ("Total Fills", str(v2_result.total_fills), str(v3_result.total_fills), f"+{v3_result.total_fills - v2_result.total_fills}"),
        ("Buy/Sell Ratio", f"{v2_result.buy_fills}/{v2_result.sell_fills}", f"{v3_result.buy_fills}/{v3_result.sell_fills}", ""),
        ("Recenters", "0", str(v3_result.recenters), ""),
        ("Spikes Detected", "0", str(v3_result.spikes_detected), ""),
        ("Exposure Breaches", "0", str(v3_result.exposure_breaches), ""),
        ("Close Reason", v2_result.close_reason, v3_result.close_reason, ""),
    ]
    
    for label, v2_val, v3_val, delta in rows:
        report.append(f"  │ {label:<19} │ {v2_val:>12} │ {v3_val:>12} │ {delta:>8} │")
    
    report.append("  └─────────────────────┴──────────────┴──────────────┴──────────┘")
    report.append("")
    
    # Winner
    report.append("  🏆 VERDICT:")
    if v3_wins_pnl:
        report.append(f"     v3 WINS on PnL: {fmt_pnl(pnl_diff)} better")
    else:
        report.append(f"     v2 WINS on PnL: {fmt_pnl(-pnl_diff)} better")
    
    if v3_wins_dd:
        report.append(f"     v3 WINS on drawdown: {dd_diff:.1f}% less risk")
    
    if v3_result.spikes_detected > 0:
        report.append(f"     v3 caught {v3_result.spikes_detected} spikes (v2 would have filled through them)")
    
    if v3_result.recenters > 0:
        report.append(f"     v3 recentered {v3_result.recenters} times (kept grid relevant)")
    
    report.append("")
    report.append("=" * 60)
    
    return "\n".join(report)


# ── Main ───────────────────────────────────────────────────────

def run_backtest(
    symbol: str = "BTCUSDT",
    days: int = 7,
    num_grids: int = 10,
    leverage: int = 50,
    order_size: float = 10.0,
    grid_range_pct: float = 3.0,
) -> tuple[GridResult, GridResult]:
    """Run a full backtest comparing v2 vs v3."""
    
    # Fetch historical data
    klines = fetch_klines(symbol=symbol, interval="5", days=days)
    if len(klines) < 100:
        logger.error(f"❌ Not enough data: {len(klines)} candles")
        return None, None
    
    # Convert to ticks
    ticks = klines_to_ticks(klines, ticks_per_candle=4)
    
    # Calculate grid bounds from recent price
    prices = [k["close"] for k in klines[-100:]]  # Last 100 candles
    current_price = prices[-1]
    avg_price = sum(prices) / len(prices)
    
    # Dynamic grid bounds
    upper = current_price * (1 + grid_range_pct / 100)
    lower = current_price * (1 - grid_range_pct / 100)
    
    config = GridConfig(
        symbol=symbol,
        upper=upper,
        lower=lower,
        num_grids=num_grids,
        leverage=leverage,
        order_size_usdt=order_size,
    )
    
    logger.info(f"\n{'='*50}")
    logger.info(f"  Running backtest: {symbol}")
    logger.info(f"  Price: ${current_price:.2f} | Range: ${lower:.2f}-${upper:.2f}")
    logger.info(f"  Grid: {num_grids} levels | {leverage}x leverage | ${order_size}/level")
    logger.info(f"  Ticks: {len(ticks)} | Duration: {days} days")
    logger.info(f"{'='*50}\n")
    
    # Run v2 (uniform sizing, no adaptive)
    logger.info("🔵 Running v2 (uniform)...")
    v2_result = simulate_grid(ticks, config, strategy="v2")
    
    # Run v3 (adaptive)
    logger.info("🟢 Running v3 (adaptive)...")
    v3_config = default_config()
    v3_result = simulate_grid(ticks, config, strategy="v3", adaptive_config=v3_config)
    
    # Generate report
    report = generate_report(v2_result, v3_result, config)
    print(report)
    
    return v2_result, v3_result


def run_multi_pair_backtest():
    """Run backtests across multiple pairs."""
    pairs = [
        ("BTCUSDT", 3.0),
        ("ETHUSDT", 4.0),
        ("SOLUSDT", 5.0),
        ("XRPUSDT", 5.0),
    ]
    
    all_results = []
    
    for symbol, range_pct in pairs:
        logger.info(f"\n{'='*60}")
        logger.info(f"  MULTI-PAIR BACKTEST: {symbol}")
        logger.info(f"{'='*60}")
        
        v2, v3 = run_backtest(
            symbol=symbol,
            days=7,
            num_grids=10,
            leverage=50,
            order_size=10.0,
            grid_range_pct=range_pct,
        )
        
        if v2 and v3:
            all_results.append((symbol, v2, v3))
    
    # Summary table
    if all_results:
        print("\n" + "=" * 80)
        print("  MULTI-PAIR SUMMARY")
        print("=" * 80)
        print(f"  {'Symbol':<10} {'v2 PnL':>10} {'v3 PnL':>10} {'Delta':>10} {'v2 DD%':>8} {'v3 DD%':>8} {'Winner':>8}")
        print("  " + "-" * 70)
        
        v3_wins = 0
        for symbol, v2, v3 in all_results:
            delta = v3.total_pnl - v2.total_pnl
            winner = "v3" if v3.total_pnl > v2.total_pnl else "v2"
            if winner == "v3":
                v3_wins += 1
            print(f"  {symbol:<10} ${v2.total_pnl:>8.2f} ${v3.total_pnl:>8.2f} ${delta:>8.2f} "
                  f"{v2.max_drawdown_pct:>6.1f}% {v3.max_drawdown_pct:>6.1f}% {winner:>8}")
        
        print(f"\n  v3 won {v3_wins}/{len(all_results)} pairs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Grid Trading Backtest v3")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair")
    parser.add_argument("--days", type=int, default=7, help="Days of history")
    parser.add_argument("--grids", type=int, default=10, help="Number of grid levels")
    parser.add_argument("--leverage", type=int, default=50, help="Leverage")
    parser.add_argument("--order-size", type=float, default=10.0, help="Order size USDT")
    parser.add_argument("--range-pct", type=float, default=3.0, help="Grid range %")
    parser.add_argument("--run-all", action="store_true", help="Run all pairs")
    
    args = parser.parse_args()
    
    if args.run_all:
        run_multi_pair_backtest()
    else:
        run_backtest(
            symbol=args.symbol,
            days=args.days,
            num_grids=args.grids,
            leverage=args.leverage,
            order_size=args.order_size,
            grid_range_pct=args.range_pct,
        )
