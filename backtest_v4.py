"""
Backtest Engine v4 — Improved grid trading with:
1. ATR-based grid spacing (adapt to volatility)
2. Candle-based fill detection (OHLC high/low)
3. Multi-cycle grid (multiple buy/sell cycles before close)
4. Dynamic grid count (adjust levels based on volatility)
5. Better Sharpe ratio optimization
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backtest_v4")


# ── Data Fetcher ───────────────────────────────────────────────

def fetch_klines(symbol: str = "BTCUSDT", interval: str = "5", days: int = 7, category: str = "linear") -> list[dict]:
    """Fetch historical klines from Bybit v5 API."""
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
        
        last_ts = int(klines[-1][0])
        if last_ts <= current_start:
            break
        current_start = last_ts + 1
        
        time.sleep(0.1)
    
    all_klines.sort(key=lambda x: x["timestamp"])
    logger.info(f"✅ Fetched {len(all_klines)} candles")
    return all_klines


# ── ATR Calculator ─────────────────────────────────────────────

def calculate_atr(klines: list[dict], period: int = 14) -> float:
    """Calculate Average True Range over klines."""
    if len(klines) < period + 1:
        return 0.0
    
    true_ranges = []
    for i in range(1, len(klines)):
        high = klines[i]["high"]
        low = klines[i]["low"]
        prev_close = klines[i-1]["close"]
        
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    
    # Simple moving average of true ranges
    if len(true_ranges) < period:
        return sum(true_ranges) / len(true_ranges)
    
    return sum(true_ranges[-period:]) / period


def calculate_volatility(klines: list[dict], period: int = 14) -> float:
    """Calculate price volatility (standard deviation of returns)."""
    if len(klines) < period + 1:
        return 0.0
    
    returns = []
    for i in range(1, len(klines)):
        ret = (klines[i]["close"] - klines[i-1]["close"]) / klines[i-1]["close"]
        returns.append(ret)
    
    if len(returns) < period:
        returns = returns[-len(returns):]
    else:
        returns = returns[-period:]
    
    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance)


# ── Grid Configuration ─────────────────────────────────────────

@dataclass
class GridConfig:
    """Grid simulation configuration."""
    symbol: str = "BTCUSDT"
    upper: float = 0.0
    lower: float = 0.0
    num_grids: int = 10
    leverage: int = 50
    order_size_usdt: float = 10.0
    
    # v4: Multi-cycle settings
    max_cycles: int = 5           # Max buy/sell cycles before forced close
    cycle_target_pct: float = 1.0 # PnL target per cycle (1% of allocated)
    
    # v4: ATR-based spacing
    use_atr_spacing: bool = True
    atr_multiplier: float = 100.0  # Grid width = ATR * multiplier (e.g. BTC ATR=$41 * 100 = $4100)
    
    # v4: Dynamic grid count
    use_dynamic_grids: bool = True
    min_grids: int = 5
    max_grids: int = 20
    
    # Close targets
    max_drawdown_pct: float = 8.0
    timeout_hours: float = 48.0


# ── Simulation ─────────────────────────────────────────────────

@dataclass
class SimLevel:
    """A simulated grid level."""
    index: int
    price: float
    side: str
    qty: float
    status: str = "open"
    cycle: int = 0  # Which cycle this level belongs to


@dataclass
class SimFill:
    """A simulated fill."""
    timestamp: float
    level_index: int
    side: str
    price: float
    qty: float
    pnl: float = 0.0
    cycle: int = 0


@dataclass
class GridResult:
    """Result of a grid simulation."""
    strategy: str
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
    cycles_completed: int = 0
    
    # Risk metrics
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_trade_pnl: float = 0.0
    
    # Timing
    duration_hours: float = 0.0
    close_reason: str = "timeout"
    
    # History
    fills: list = field(default_factory=list)
    pnl_series: list = field(default_factory=list)


def calculate_sharpe(pnl_series: list[float], risk_free_rate: float = 0.0) -> float:
    """Calculate Sharpe ratio from PnL series."""
    if len(pnl_series) < 2:
        return 0.0
    
    returns = []
    for i in range(1, len(pnl_series)):
        if pnl_series[i-1] != 0:
            ret = (pnl_series[i] - pnl_series[i-1]) / abs(pnl_series[i-1])
            returns.append(ret)
    
    if not returns:
        return 0.0
    
    mean_ret = sum(returns) / len(returns)
    if len(returns) < 2:
        return 0.0
    
    variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
    std_ret = math.sqrt(variance) if variance > 0 else 1e-8
    
    return (mean_ret - risk_free_rate) / std_ret


def simulate_grid_candle(
    klines: list[dict],
    config: GridConfig,
    strategy: str = "v2",
) -> GridResult:
    """
    Simulate grid trading using candle-based fill detection.
    
    v2: Uniform spacing, single cycle
    v4: ATR spacing, multi-cycle, dynamic grids
    """
    is_v4 = strategy == "v4"
    
    # Calculate ATR and volatility
    atr = calculate_atr(klines, period=14)
    volatility = calculate_volatility(klines, period=14)
    current_price = klines[0]["close"]
    
    # v4: ATR-based grid spacing
    if is_v4 and config.use_atr_spacing and atr > 0:
        # ATR multiplier should be large enough to create meaningful grid width
        # For BTC with ATR=$41, multiplier=100 gives width=$4100 (~5% of price)
        grid_width = atr * config.atr_multiplier
        # Ensure minimum width (2% of price for funded challenge safety)
        min_width = current_price * 0.02
        grid_width = max(grid_width, min_width)
        upper = current_price + grid_width / 2
        lower = current_price - grid_width / 2
    else:
        upper = config.upper
        lower = config.lower
    
    # v4: Dynamic grid count based on volatility
    if is_v4 and config.use_dynamic_grids and volatility > 0:
        # More grids in low volatility, fewer in high volatility
        vol_pct = volatility * 100
        if vol_pct < 0.5:
            num_grids = config.max_grids
        elif vol_pct < 1.0:
            num_grids = int(config.max_grids * 0.7)
        elif vol_pct < 2.0:
            num_grids = int(config.max_grids * 0.5)
        else:
            num_grids = config.min_grids
        num_grids = max(config.min_grids, min(config.max_grids, num_grids))
    else:
        num_grids = config.num_grids
    
    # Calculate grid levels
    step = (upper - lower) / num_grids
    levels: list[SimLevel] = []
    
    for i in range(num_grids + 1):
        price_lvl = lower + step * i
        if abs(price_lvl - current_price) < step * 0.2:
            continue
        
        side = "Buy" if price_lvl < current_price else "Sell"
        qty = (config.order_size_usdt * config.leverage) / price_lvl
        
        levels.append(SimLevel(
            index=i,
            price=price_lvl,
            side=side,
            qty=qty,
            status="open",
            cycle=0,
        ))
    
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
    cycles_completed = 0
    current_cycle = 0
    cycle_pnl = 0.0
    
    allocated_margin = config.order_size_usdt * num_grids
    cycle_target = allocated_margin * config.cycle_target_pct / 100
    drawdown_limit = allocated_margin * config.max_drawdown_pct / 100
    
    start_time = klines[0]["timestamp"]
    close_reason = "timeout"
    pnl_series = [0.0]
    
    # Process each candle
    for candle in klines:
        ts = candle["timestamp"]
        high = candle["high"]
        low = candle["low"]
        close = candle["close"]
        
        # Check fills using candle high/low
        for level in levels:
            if level.index in filled_levels:
                continue
            
            filled = False
            fill_price = level.price
            
            # v4: Candle-based fill detection
            if level.side == "Buy" and low <= level.price:
                filled = True
                fill_price = level.price
            elif level.side == "Sell" and high >= level.price:
                filled = True
                fill_price = level.price
            
            if filled:
                filled_levels.add(level.index)
                level.status = "filled"
                
                # Calculate PnL
                fill_pnl = 0.0
                if position_qty > 0 and position_side != level.side:
                    if position_side == "Buy":
                        fill_pnl = (fill_price - entry_price) * min(level.qty, position_qty)
                    else:
                        fill_pnl = (entry_price - fill_price) * min(level.qty, position_qty)
                    realized_pnl += fill_pnl
                    cycle_pnl += fill_pnl
                    
                    position_qty -= level.qty
                    if position_qty <= 0:
                        position_qty = 0
                        entry_price = 0
                        position_side = ""
                else:
                    if position_qty > 0:
                        total_cost = entry_price * position_qty + fill_price * level.qty
                        position_qty += level.qty
                        entry_price = total_cost / position_qty
                    else:
                        position_qty = level.qty
                        entry_price = fill_price
                        position_side = level.side
                
                fills.append(SimFill(
                    timestamp=ts,
                    level_index=level.index,
                    side=level.side,
                    price=fill_price,
                    qty=level.qty,
                    pnl=fill_pnl,
                    cycle=current_cycle,
                ))
                
                # v4: Multi-cycle — reset levels after cycle completes
                if is_v4 and position_qty <= 0 and cycle_pnl > 0:
                    cycles_completed += 1
                    current_cycle += 1
                    cycle_pnl = 0.0
                    
                    # Reset all levels for next cycle
                    filled_levels.clear()
                    for lvl in levels:
                        lvl.status = "open"
                        lvl.cycle = current_cycle
                    
                    logger.info(f"  🔄 Cycle {cycles_completed} completed | "
                              f"realized=${realized_pnl:.2f}")
        
        # Update unrealized PnL
        if position_qty > 0 and entry_price > 0:
            if position_side == "Buy":
                unrealized_pnl = (close - entry_price) * position_qty
            else:
                unrealized_pnl = (entry_price - close) * position_qty
        else:
            unrealized_pnl = 0.0
        
        total_pnl = realized_pnl + unrealized_pnl
        pnl_series.append(total_pnl)
        
        # Track drawdown
        if total_pnl < 0:
            dd = abs(total_pnl)
            if dd > max_drawdown:
                max_drawdown = dd
                max_drawdown_pct = dd / max(allocated_margin, 1) * 100
        
        # Check close conditions
        elapsed_hours = (ts - start_time) / 3600
        
        # v4: Close after max cycles
        if is_v4 and cycles_completed >= config.max_cycles:
            close_reason = "max_cycles"
            break
        
        # v2: Close at target PnL
        if not is_v4 and total_pnl >= cycle_target and len(fills) >= 2:
            close_reason = "target_hit"
            break
        
        # Timeout
        if elapsed_hours >= config.timeout_hours:
            close_reason = "timeout"
            break
    
    # Final PnL
    final_price = klines[-1]["close"]
    if position_qty > 0 and entry_price > 0:
        if position_side == "Buy":
            unrealized_pnl = (final_price - entry_price) * position_qty
        else:
            unrealized_pnl = (entry_price - final_price) * position_qty
    
    total_pnl = realized_pnl + unrealized_pnl
    duration_hours = (klines[-1]["timestamp"] - start_time) / 3600
    
    # Calculate risk metrics
    sharpe = calculate_sharpe(pnl_series)
    
    winning_trades = [f for f in fills if f.pnl > 0]
    losing_trades = [f for f in fills if f.pnl < 0]
    win_rate = len(winning_trades) / max(len(fills), 1) * 100
    
    gross_profit = sum(f.pnl for f in winning_trades)
    gross_loss = abs(sum(f.pnl for f in losing_trades))
    profit_factor = gross_profit / max(gross_loss, 0.01)
    
    avg_trade_pnl = total_pnl / max(len(fills), 1)
    
    buy_fills = sum(1 for f in fills if f.side == "Buy")
    sell_fills = sum(1 for f in fills if f.side == "Sell")
    
    return GridResult(
        strategy=strategy,
        symbol=config.symbol,
        start_price=klines[0]["close"],
        end_price=final_price,
        upper=upper,
        lower=lower,
        num_grids=num_grids,
        leverage=config.leverage,
        realized_pnl=round(realized_pnl, 4),
        unrealized_pnl=round(unrealized_pnl, 4),
        total_pnl=round(total_pnl, 4),
        max_drawdown=round(max_drawdown, 4),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        total_fills=len(fills),
        buy_fills=buy_fills,
        sell_fills=sell_fills,
        cycles_completed=cycles_completed,
        sharpe_ratio=round(sharpe, 4),
        win_rate=round(win_rate, 2),
        profit_factor=round(profit_factor, 4),
        avg_trade_pnl=round(avg_trade_pnl, 4),
        duration_hours=round(duration_hours, 2),
        close_reason=close_reason,
        fills=fills,
        pnl_series=pnl_series,
    )


# ── Report Generator ───────────────────────────────────────────

def generate_report(v2: GridResult, v4: GridResult, config: GridConfig) -> str:
    """Generate comparison report."""
    
    def fmt_pnl(val):
        sign = "+" if val >= 0 else ""
        return f"{sign}${val:.2f}"
    
    def fmt_pct(val):
        return f"{val:.1f}%"
    
    report = []
    report.append("=" * 65)
    report.append(f"  BACKTEST v4: {config.symbol}")
    report.append(f"  v2: uniform spacing, single cycle")
    report.append(f"  v4: ATR spacing, multi-cycle, dynamic grids")
    report.append("=" * 65)
    report.append("")
    
    price_change = (v4.end_price - v4.start_price) / v4.start_price * 100
    report.append(f"  📈 Price: ${v4.start_price:.2f} → ${v4.end_price:.2f} ({price_change:+.2f}%)")
    report.append(f"  ⏱️  Duration: {v4.duration_hours:.1f} hours")
    report.append("")
    
    report.append("  ┌─────────────────────┬──────────────┬──────────────┬──────────┐")
    report.append("  │ Metric              │ v2 (uniform) │ v4 (improved)│ Δ Delta  │")
    report.append("  ├─────────────────────┼──────────────┼──────────────┼──────────┤")
    
    rows = [
        ("Total PnL", fmt_pnl(v2.total_pnl), fmt_pnl(v4.total_pnl), fmt_pnl(v4.total_pnl - v2.total_pnl)),
        ("Realized PnL", fmt_pnl(v2.realized_pnl), fmt_pnl(v4.realized_pnl), ""),
        ("Unrealized PnL", fmt_pnl(v2.unrealized_pnl), fmt_pnl(v4.unrealized_pnl), ""),
        ("Max Drawdown", fmt_pct(v2.max_drawdown_pct), fmt_pct(v4.max_drawdown_pct), f"{v4.max_drawdown_pct - v2.max_drawdown_pct:+.1f}%"),
        ("Total Fills", str(v2.total_fills), str(v4.total_fills), f"+{v4.total_fills - v2.total_fills}"),
        ("Cycles", str(v2.cycles_completed), str(v4.cycles_completed), ""),
        ("Buy/Sell", f"{v2.buy_fills}/{v2.sell_fills}", f"{v4.buy_fills}/{v4.sell_fills}", ""),
        ("Sharpe Ratio", f"{v2.sharpe_ratio:.2f}", f"{v4.sharpe_ratio:.2f}", f"{v4.sharpe_ratio - v2.sharpe_ratio:+.2f}"),
        ("Win Rate", fmt_pct(v2.win_rate), fmt_pct(v4.win_rate), ""),
        ("Profit Factor", f"{v2.profit_factor:.2f}", f"{v4.profit_factor:.2f}", ""),
        ("Avg Trade PnL", fmt_pnl(v2.avg_trade_pnl), fmt_pnl(v4.avg_trade_pnl), ""),
        ("Grid Levels", str(v2.num_grids), str(v4.num_grids), ""),
        ("Grid Width", f"${v2.upper - v2.lower:.2f}", f"${v4.upper - v4.lower:.2f}", ""),
        ("Close Reason", v2.close_reason, v4.close_reason, ""),
    ]
    
    for label, v2_val, v4_val, delta in rows:
        report.append(f"  │ {label:<19} │ {v2_val:>12} │ {v4_val:>12} │ {delta:>8} │")
    
    report.append("  └─────────────────────┴──────────────┴──────────────┴──────────┘")
    report.append("")
    
    # Verdict
    report.append("  🏆 VERDICT:")
    if v4.total_pnl > v2.total_pnl:
        report.append(f"     v4 WINS on PnL: {fmt_pnl(v4.total_pnl - v2.total_pnl)} better")
    else:
        report.append(f"     v2 WINS on PnL: {fmt_pnl(v2.total_pnl - v4.total_pnl)} better")
    
    if v4.sharpe_ratio > v2.sharpe_ratio:
        report.append(f"     v4 WINS on Sharpe: {v4.sharpe_ratio - v2.sharpe_ratio:+.2f} better risk-adjusted")
    
    if v4.max_drawdown_pct < v2.max_drawdown_pct:
        report.append(f"     v4 WINS on drawdown: {v2.max_drawdown_pct - v4.max_drawdown_pct:.1f}% less risk")
    
    if v4.cycles_completed > 0:
        report.append(f"     v4 completed {v4.cycles_completed} cycles (more trades = more opportunities)")
    
    report.append("")
    report.append("=" * 65)
    
    return "\n".join(report)


# ── Main ───────────────────────────────────────────────────────

def run_backtest(
    symbol: str = "BTCUSDT",
    days: int = 7,
    leverage: int = 50,
    order_size: float = 10.0,
    grid_range_pct: float = 3.0,
    max_cycles: int = 5,
) -> tuple[GridResult, GridResult]:
    """Run v2 vs v4 backtest."""
    
    klines = fetch_klines(symbol=symbol, interval="5", days=days)
    if len(klines) < 100:
        logger.error(f"❌ Not enough data: {len(klines)} candles")
        return None, None
    
    current_price = klines[0]["close"]
    upper = current_price * (1 + grid_range_pct / 100)
    lower = current_price * (1 - grid_range_pct / 100)
    
    # ATR for v4
    atr = calculate_atr(klines, period=14)
    volatility = calculate_volatility(klines, period=14)
    
    logger.info(f"\n{'='*50}")
    logger.info(f"  Symbol: {symbol} | Price: ${current_price:.2f}")
    logger.info(f"  ATR(14): ${atr:.2f} | Volatility: {volatility*100:.2f}%")
    logger.info(f"  v2 Range: ${lower:.2f}-${upper:.2f}")
    logger.info(f"  v4 Range: ATR-based (auto)")
    logger.info(f"{'='*50}\n")
    
    config = GridConfig(
        symbol=symbol,
        upper=upper,
        lower=lower,
        num_grids=10,
        leverage=leverage,
        order_size_usdt=order_size,
        max_cycles=max_cycles,
        use_atr_spacing=True,
        use_dynamic_grids=True,
    )
    
    # Run v2
    logger.info("🔵 Running v2 (uniform, single cycle)...")
    v2_result = simulate_grid_candle(klines, config, strategy="v2")
    
    # Run v4
    logger.info("🟢 Running v4 (ATR spacing, multi-cycle)...")
    v4_result = simulate_grid_candle(klines, config, strategy="v4")
    
    report = generate_report(v2_result, v4_result, config)
    print(report)
    
    return v2_result, v4_result


def run_multi_pair():
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
        logger.info(f"  MULTI-PAIR: {symbol}")
        logger.info(f"{'='*60}")
        
        v2, v4 = run_backtest(
            symbol=symbol,
            days=7,
            leverage=50,
            order_size=10.0,
            grid_range_pct=range_pct,
            max_cycles=5,
        )
        
        if v2 and v4:
            all_results.append((symbol, v2, v4))
    
    if all_results:
        print("\n" + "=" * 80)
        print("  MULTI-PAIR SUMMARY")
        print("=" * 80)
        print(f"  {'Symbol':<10} {'v2 PnL':>10} {'v4 PnL':>10} {'Delta':>10} {'v2 Sharpe':>10} {'v4 Sharpe':>10} {'Winner':>8}")
        print("  " + "-" * 75)
        
        v4_wins = 0
        for symbol, v2, v4 in all_results:
            delta = v4.total_pnl - v2.total_pnl
            winner = "v4" if v4.total_pnl > v2.total_pnl else "v2"
            if winner == "v4":
                v4_wins += 1
            print(f"  {symbol:<10} ${v2.total_pnl:>8.2f} ${v4.total_pnl:>8.2f} ${delta:>8.2f} "
                  f"{v2.sharpe_ratio:>8.2f} {v4.sharpe_ratio:>8.2f} {winner:>8}")
        
        print(f"\n  v4 won {v4_wins}/{len(all_results)} pairs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Grid Trading Backtest v4")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--leverage", type=int, default=50)
    parser.add_argument("--order-size", type=float, default=10.0)
    parser.add_argument("--range-pct", type=float, default=3.0)
    parser.add_argument("--max-cycles", type=int, default=5)
    parser.add_argument("--run-all", action="store_true")
    
    args = parser.parse_args()
    
    if args.run_all:
        run_multi_pair()
    else:
        run_backtest(
            symbol=args.symbol,
            days=args.days,
            leverage=args.leverage,
            order_size=args.order_size,
            grid_range_pct=args.range_pct,
            max_cycles=args.max_cycles,
        )
