"""
Smart Close Backtest — Compare "hold until positive" vs smart close logic.

Runs both strategies on the same historical data to measure improvement.
Tests: win rate, average loss, max drawdown, total return.

Usage:
    python smart_close_backtest.py              # Default: BTC+ETH, 30 days
    python smart_close_backtest.py --days 60    # 60 days
    python smart_close_backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT XRPUSDT
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import List, Dict
from urllib.request import urlopen, Request
from urllib.error import URLError

import pandas as pd

sys.path.insert(0, ".")
from grid_core import (
    GridPosition, FillEvent, GridImbalance, SmartCloseEngine, SmartCloseConfig,
    CloseReason, process_fill, check_close_conditions, reset_position, reset_imbalance,
)


# ── Data Fetcher ───────────────────────────────────────────────

def fetch_candles(symbol: str, interval: str = "60", days: int = 30) -> pd.DataFrame:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    rows = []
    current = start_ms
    while current < end_ms:
        url = (f"https://api.bybit.com/v5/market/kline"
               f"?category=linear&symbol={symbol}"
               f"&interval={interval}&start={current}&end={end_ms}&limit=1000")
        try:
            req = Request(url, headers={"User-Agent": "SmartCloseBacktest/1.0"})
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception:
            break
        klines = data.get("result", {}).get("list", [])
        if not klines:
            break
        for k in klines:
            rows.append({
                "Open": float(k[1]), "High": float(k[2]),
                "Low": float(k[3]), "Close": float(k[4]),
                "Volume": float(k[5]), "_ts": int(k[0]) / 1000,
            })
        last_ts = int(klines[-1][0])
        if last_ts <= current:
            break
        current = last_ts + 1
        time.sleep(0.05)
    rows.sort(key=lambda r: r["_ts"])
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["_ts"], unit="s", utc=True)
    df.drop(columns=["_ts"], inplace=True)
    return df[~df.index.duplicated(keep='last')]


# ── Grid Simulation ────────────────────────────────────────────

@dataclass
class GridSimConfig:
    grid_spacing: float = 500
    tp_spacing: float = 1000
    sl_spacing: float = 500
    order_size: float = 10.0
    leverage: float = 10.0
    max_cycles: int = 3


@dataclass
class SimResult:
    strategy: str
    symbol: str
    total_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    max_drawdown: float = 0.0
    total_fills: int = 0
    tp_fills: int = 0
    sl_fills: int = 0
    smart_close_fills: int = 0
    cycles: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    trades: List[Dict] = field(default_factory=list)


def simulate_grid(
    candles: pd.DataFrame,
    config: GridSimConfig,
    strategy: str = "hold",
    symbol: str = "BTCUSDT",
) -> SimResult:
    """
    Simulate grid trading on historical candles.
    
    strategy="hold": Original "hold until positive" approach
    strategy="smart": Smart close logic (time decay, momentum, imbalance)
    """
    close_series = candles["Close"].astype(float).values
    high_series = candles["High"].astype(float).values
    low_series = candles["Low"].astype(float).values
    timestamps = candles.index
    
    mid_price = close_series[0]
    grid_upper = mid_price * (1 + config.grid_spacing * 20 / mid_price)
    grid_lower = mid_price * (1 - config.grid_spacing * 20 / mid_price)
    
    # Generate grid levels
    levels = []
    step = config.grid_spacing
    n_levels = int((grid_upper - grid_lower) / step) + 1
    for i in range(n_levels):
        price = grid_lower + step * i
        side = "Buy" if price < mid_price else "Sell"
        levels.append({"price": price, "side": side, "index": i, "filled": False})
    
    # State
    position = GridPosition()
    imbalance = GridImbalance()
    fills = []
    pnl_history = []
    max_dd = 0.0
    
    # Smart close engine
    if strategy == "smart":
        smart_config = SmartCloseConfig(
            time_decay_enabled=True,
            time_decay_hours=8.0,
            time_decay_min_loss_pct=0.5,
            momentum_exit_enabled=True,
            momentum_window_sec=3600,  # 1 hour for hourly candles
            momentum_threshold_pct=1.5,
            imbalance_close_enabled=True,
            imbalance_ratio_threshold=3.0,
            imbalance_min_fills=6,
            trailing_stop_enabled=True,
            trailing_stop_initial_pct=3.0,
            trailing_stop_tighten_hours=4.0,
            trailing_stop_tightened_pct=1.5,
            recovery_check_enabled=True,
            recovery_min_depth_pct=1.0,
            recovery_max_hours=12.0,
        )
        smart_close = SmartCloseEngine(smart_config)
    else:
        smart_close = None
    
    allocated = config.order_size * len(levels)
    tp_pct = 2.0  # Target 2% of allocated
    dd_pct = 8.0  # Max 8% drawdown
    
    cycles = 0
    smart_closes = 0
    prev_close = close_series[0]
    
    for i in range(len(close_series)):
        close = close_series[i]
        high = high_series[i]
        low = low_series[i]
        ts = timestamps[i]
        ts_float = ts.timestamp() if hasattr(ts, 'timestamp') else i * 3600
        
        if smart_close:
            smart_close.update_price(close, ts_float)
        
        # Fill detection
        for level in levels:
            if level["filled"]:
                continue
            
            filled = False
            if level["side"] == "Buy" and prev_close > level["price"] >= low:
                filled = True
            elif level["side"] == "Sell" and prev_close < level["price"] <= high:
                filled = True
            
            if filled:
                level["filled"] = True
                fill = FillEvent(
                    level_index=level["index"],
                    side=level["side"],
                    price=level["price"],
                    qty=config.order_size * config.leverage / level["price"],
                    timestamp=ts_float,
                )
                pnl = process_fill(fill, position, imbalance)
                fills.append(fill)
        
        # Update unrealized
        position.update_unrealized(close)
        total = position.realized_pnl + position.unrealized_pnl
        max_dd = min(max_dd, total)
        
        # Check close conditions
        pnl_result = check_close_conditions(
            position=position,
            current_price=close,
            allocated_margin=allocated,
            target_pnl_pct_low=tp_pct,
            target_pnl_pct_high=4.0,
            max_drawdown_pct=dd_pct,
            total_fills=len(fills),
        )
        
        should_close = False
        close_reason = ""
        
        if pnl_result.should_close:
            should_close = True
            close_reason = pnl_result.close_reason
        
        # Smart close check
        if not should_close and strategy == "smart" and not position.is_flat and total < 0:
            smart_reason = smart_close.check_smart_close(
                position=position,
                current_price=close,
                allocated_margin=allocated,
                imbalance=imbalance,
                total_fills=len(fills),
            )
            if smart_reason:
                should_close = True
                close_reason = smart_reason.value
                smart_closes += 1
        
        if should_close:
            cycles += 1
            pnl_history.append({
                "cycle": cycles,
                "pnl": total,
                "reason": close_reason,
                "fills": len(fills),
            })
            reset_position(position)
            reset_imbalance(imbalance)
            for level in levels:
                level["filled"] = False
        
        prev_close = close
    
    # Calculate stats
    total_pnl = position.realized_pnl + position.unrealized_pnl
    wins = [t for t in pnl_history if t["pnl"] > 0]
    losses = [t for t in pnl_history if t["pnl"] <= 0]
    
    win_rate = len(wins) / max(len(pnl_history), 1) * 100
    avg_win = sum(t["pnl"] for t in wins) / max(len(wins), 1)
    avg_loss = sum(t["pnl"] for t in losses) / max(len(losses), 1)
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / max(gross_loss, 0.01)
    
    return SimResult(
        strategy=strategy,
        symbol=symbol,
        total_pnl=round(total_pnl + sum(t["pnl"] for t in pnl_history), 2),
        realized_pnl=round(position.realized_pnl, 2),
        unrealized_pnl=round(position.unrealized_pnl, 2),
        max_drawdown=round(abs(max_dd), 2),
        total_fills=len(fills),
        tp_fills=len(wins),
        sl_fills=len(losses),
        smart_close_fills=smart_closes,
        cycles=cycles,
        win_rate=round(win_rate, 1),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        profit_factor=round(profit_factor, 2),
        trades=pnl_history,
    )


# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Smart Close Backtest")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    args = parser.parse_args()
    
    print("=" * 70)
    print("  SMART CLOSE BACKTEST")
    print("  Comparing 'hold until positive' vs 'smart close' strategies")
    print("=" * 70)
    
    all_results = []
    
    for symbol in args.symbols:
        print(f"\n📡 Fetching {symbol} ({args.days}d)...")
        candles = fetch_candles(symbol, days=args.days)
        print(f"   ✅ {len(candles)} candles")
        
        config = GridSimConfig()
        
        # Run both strategies
        hold_result = simulate_grid(candles, config, strategy="hold", symbol=symbol)
        smart_result = simulate_grid(candles, config, strategy="smart", symbol=symbol)
        
        all_results.append((hold_result, smart_result))
        
        print(f"\n  {symbol} Results:")
        print(f"  {'Metric':<25} {'Hold':>12} {'Smart':>12} {'Delta':>12}")
        print(f"  {'-'*60}")
        print(f"  {'Total PnL':<25} ${hold_result.total_pnl:>11,.2f} ${smart_result.total_pnl:>11,.2f} ${smart_result.total_pnl - hold_result.total_pnl:>11,.2f}")
        print(f"  {'Cycles Completed':<25} {hold_result.cycles:>12} {smart_result.cycles:>12} {smart_result.cycles - hold_result.cycles:>12}")
        print(f"  {'Win Rate':<25} {hold_result.win_rate:>11.1f}% {smart_result.win_rate:>11.1f}% {smart_result.win_rate - hold_result.win_rate:>11.1f}%")
        print(f"  {'Avg Win':<25} ${hold_result.avg_win:>11,.2f} ${smart_result.avg_win:>11,.2f}")
        print(f"  {'Avg Loss':<25} ${hold_result.avg_loss:>11,.2f} ${smart_result.avg_loss:>11,.2f}")
        print(f"  {'Profit Factor':<25} {hold_result.profit_factor:>12.2f} {smart_result.profit_factor:>12.2f}")
        print(f"  {'Max Drawdown':<25} ${hold_result.max_drawdown:>11,.2f} ${smart_result.max_drawdown:>11,.2f}")
        print(f"  {'Smart Closes':<25} {'N/A':>12} {smart_result.smart_close_fills:>12}")
    
    # Summary
    print(f"\n{'='*70}")
    print(f"  AGGREGATE RESULTS")
    print(f"{'='*70}")
    
    hold_total = sum(r.total_pnl for r, _ in all_results)
    smart_total = sum(r.total_pnl for _, r in all_results)
    hold_cycles = sum(r.cycles for r, _ in all_results)
    smart_cycles = sum(r.cycles for _, r in all_results)
    hold_wr = sum(r.win_rate for r, _ in all_results) / len(all_results)
    smart_wr = sum(r.win_rate for _, r in all_results) / len(all_results)
    hold_pf = sum(r.profit_factor for r, _ in all_results) / len(all_results)
    smart_pf = sum(r.profit_factor for _, r in all_results) / len(all_results)
    smart_closes = sum(r.smart_close_fills for _, r in all_results)
    
    print(f"  {'Metric':<25} {'Hold':>12} {'Smart':>12} {'Improvement':>12}")
    print(f"  {'-'*60}")
    print(f"  {'Total PnL':<25} ${hold_total:>11,.2f} ${smart_total:>11,.2f} ${smart_total - hold_total:>11,.2f}")
    print(f"  {'Total Cycles':<25} {hold_cycles:>12} {smart_cycles:>12} {smart_cycles - hold_cycles:>12}")
    print(f"  {'Avg Win Rate':<25} {hold_wr:>11.1f}% {smart_wr:>11.1f}% {smart_wr - hold_wr:>11.1f}%")
    print(f"  {'Avg Profit Factor':<25} {hold_pf:>12.2f} {smart_pf:>12.2f} {smart_pf - hold_pf:>12.2f}")
    print(f"  {'Smart Closes':<25} {'N/A':>12} {smart_closes:>12}")
    
    pnl_improvement = smart_total - hold_total
    wr_improvement = smart_wr - hold_wr
    
    print(f"\n  {'VERDICT':}")
    if pnl_improvement > 0:
        print(f"  ✅ Smart close IMPROVED PnL by ${pnl_improvement:,.2f}")
    else:
        print(f"  ❌ Smart close REDUCED PnL by ${abs(pnl_improvement):,.2f}")
    
    if wr_improvement > 0:
        print(f"  ✅ Win rate IMPROVED by {wr_improvement:.1f}pp")
    else:
        print(f"  ❌ Win rate REDUCED by {abs(wr_improvement):.1f}pp")
    
    if smart_pf > hold_pf:
        print(f"  ✅ Profit factor IMPROVED: {hold_pf:.2f} → {smart_pf:.2f}")


if __name__ == "__main__":
    main()
