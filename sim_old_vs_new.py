"""
Live-data simulation: OLD vs NEW smart-close config.

Replays recent 5-minute Bybit candles through the production close pipeline
(check_close_conditions + SmartCloseEngine.check_smart_close) using the same
two configs the live bot has run with:

  OLD: cooldown=0s, recovery=0s, hard_floor=∞, MAX_DD=3%, momentum=1%,
       trailing=2%, time_decay=2h.  (force-close on drawdown breach)
  NEW: cooldown=180s, recovery=300s/30%, hard_floor=5%, MAX_DD=6%,
       momentum=2.5%, trailing=3.5%, time_decay=4h.
       Drawdown breach routed through cooldown + recovery.

Reports per-symbol and aggregate: close-reason mix, win rate, avg win/loss,
total PnL.
"""
from __future__ import annotations
import json
import sys
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from urllib.request import urlopen, Request

sys.path.insert(0, ".")
from grid_core import (
    GridPosition, FillEvent, GridImbalance,
    SmartCloseEngine, SmartCloseConfig, CloseReason,
    process_fill, check_close_conditions, reset_position, reset_imbalance,
)

SYMBOLS = ["PEPEUSDT", "WIFUSDT", "WLDUSDT", "GENIUSUSDT", "BIOUSDT",
           "ZEREBROUSDT", "FARTCOINUSDT", "PENGUUSDT"]
DAYS = 7
INTERVAL = "5"      # minutes
ORDER_SIZE = 1.0    # USDT margin per grid level
NUM_GRIDS = 10
LEVERAGE = 25
TARGET_LOW_PCT = 5.0
TARGET_HIGH_PCT = 8.0


def fetch_candles(symbol: str, interval: str = INTERVAL, days: int = DAYS):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    rows: List[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (f"https://api.bybit.com/v5/market/kline?category=linear"
               f"&symbol={symbol}&interval={interval}&start={cursor}&end={end_ms}&limit=1000")
        try:
            req = Request(url, headers={"User-Agent": "sim/1"})
            with urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"  fetch err {symbol}: {e}", file=sys.stderr)
            break
        ks = data.get("result", {}).get("list", [])
        if not ks:
            break
        for k in ks:
            rows.append({"ts": int(k[0]) / 1000,
                         "high": float(k[2]), "low": float(k[3]), "close": float(k[4])})
        last = int(ks[0][0])  # bybit returns desc; first item is most recent in batch
        if last <= cursor:
            break
        cursor = last + 1
        time.sleep(0.05)
    rows.sort(key=lambda r: r["ts"])
    # de-dup
    seen = set(); out = []
    for r in rows:
        if r["ts"] in seen: continue
        seen.add(r["ts"]); out.append(r)
    return out


def make_old_config() -> SmartCloseConfig:
    # Legacy aggressive defaults (pre-fix), with deferral disabled.
    return SmartCloseConfig(
        time_decay_hours=2.0, time_decay_min_loss_pct=1.0,
        momentum_threshold_pct=1.0, momentum_window_sec=60.0,
        imbalance_ratio_threshold=3.0, imbalance_min_fills=6,
        trailing_stop_initial_pct=2.0, trailing_stop_tightened_pct=2.0,
        trailing_stop_tighten_hours=4.0,
        recovery_min_depth_pct=1.5, recovery_max_hours=6.0,
        min_seconds_since_last_fill=0.0,
        recovery_window_sec=0.0, recovery_partial_pct=0.0,
        hard_loss_pct_floor=999.0,
    )


def make_new_config() -> SmartCloseConfig:
    return SmartCloseConfig(
        time_decay_hours=4.0, time_decay_min_loss_pct=1.0,
        momentum_threshold_pct=2.5, momentum_window_sec=60.0,
        imbalance_ratio_threshold=3.5, imbalance_min_fills=8,
        trailing_stop_initial_pct=3.5, trailing_stop_tightened_pct=2.5,
        trailing_stop_tighten_hours=4.0,
        recovery_min_depth_pct=1.5, recovery_max_hours=6.0,
        min_seconds_since_last_fill=180.0,
        recovery_window_sec=300.0, recovery_partial_pct=30.0,
        hard_loss_pct_floor=5.0,
    )


@dataclass
class TradeOutcome:
    symbol: str
    pnl: float
    reason: str
    fills: int
    duration_sec: float


def build_grid(mid: float, n: int):
    # +/- 2% range, n+1 levels.
    upper = mid * 1.02
    lower = mid * 0.98
    step = (upper - lower) / n
    levels = []
    for i in range(n + 1):
        p = lower + step * i
        side = "Buy" if p < mid else "Sell"
        if abs(p - mid) < step * 0.3:
            continue
        levels.append({"index": i, "price": p, "side": side, "filled": False})
    return levels, upper, lower


def simulate_symbol(candles: List[dict], cfg: SmartCloseConfig,
                    label: str, symbol: str, max_dd_pct: float) -> List[TradeOutcome]:
    """Replay one symbol's candles end-to-end. New grid is rebuilt each cycle."""
    if len(candles) < 50:
        return []

    qty = (ORDER_SIZE * LEVERAGE) / candles[0]["close"]
    allocated_margin = ORDER_SIZE * NUM_GRIDS
    outcomes: List[TradeOutcome] = []

    pos = GridPosition()
    imb = GridImbalance()
    smart = SmartCloseEngine(cfg)

    mid = candles[0]["close"]
    levels, upper, lower = build_grid(mid, NUM_GRIDS)
    grid_started = candles[0]["ts"]
    fills_count = 0
    prev_close = mid
    fired = False

    # Drive the full price path; reopen a fresh grid after each close.
    for c in candles:
        ts = c["ts"]; high = c["high"]; low = c["low"]; close = c["close"]
        # Use close as the SmartClose price tape (1 tick per candle).
        smart.update_price(close, ts)

        # Fill detection per level: did this candle range touch the level?
        for lv in levels:
            if lv["filled"]:
                continue
            touched = (lv["side"] == "Buy" and low <= lv["price"] <= prev_close) or \
                      (lv["side"] == "Sell" and prev_close <= lv["price"] <= high)
            if touched:
                lv["filled"] = True
                # Use level price for fill, qty from constant.
                f = FillEvent(level_index=lv["index"], side=lv["side"],
                              price=lv["price"], qty=qty, timestamp=ts)
                process_fill(f, pos, imb)
                fills_count += 1

        # Update unrealized + standard close conditions.
        pnl_result = check_close_conditions(
            position=pos, current_price=close, allocated_margin=allocated_margin,
            target_pnl_pct_low=TARGET_LOW_PCT, target_pnl_pct_high=TARGET_HIGH_PCT,
            max_drawdown_pct=max_dd_pct, total_fills=fills_count,
        )

        close_reason: Optional[str] = None
        if pnl_result.should_close:
            close_reason = pnl_result.close_reason  # target_hit
        elif pnl_result.total_pnl < 0 and not pos.is_flat:
            sm = smart.check_smart_close(
                position=pos, current_price=close, allocated_margin=allocated_margin,
                imbalance=imb, total_fills=fills_count,
                drawdown_breached=pnl_result.drawdown_breached,
            )
            if sm:
                close_reason = sm.value
            elif label == "OLD" and pnl_result.drawdown_breached:
                # OLD code: force-close on drawdown breach when smart says hold.
                close_reason = "drawdown"

        if close_reason:
            outcomes.append(TradeOutcome(
                symbol=symbol, pnl=pnl_result.total_pnl, reason=close_reason,
                fills=fills_count, duration_sec=ts - grid_started,
            ))
            # Reset for next cycle.
            reset_position(pos); reset_imbalance(imb)
            smart.reset_recovery()
            mid = close
            levels, upper, lower = build_grid(mid, NUM_GRIDS)
            grid_started = ts
            fills_count = 0

        prev_close = close

    return outcomes


def summarize(label: str, outcomes: List[TradeOutcome]):
    if not outcomes:
        print(f"\n[{label}] no trades closed.")
        return
    wins = [o for o in outcomes if o.pnl > 0]
    losses = [o for o in outcomes if o.pnl < 0]
    total_pnl = sum(o.pnl for o in outcomes)
    wr = len(wins) / len(outcomes) * 100
    avg_w = sum(o.pnl for o in wins) / len(wins) if wins else 0.0
    avg_l = sum(o.pnl for o in losses) / len(losses) if losses else 0.0
    profit_factor = (sum(o.pnl for o in wins) / abs(sum(o.pnl for o in losses))) if losses and sum(o.pnl for o in losses) < 0 else float('inf')

    print(f"\n=== {label} ===")
    print(f"trades={len(outcomes)}  wins={len(wins)}  losses={len(losses)}  "
          f"win_rate={wr:.1f}%")
    print(f"total_pnl=${total_pnl:+.4f}  avg_win=${avg_w:+.4f}  avg_loss=${avg_l:+.4f}  "
          f"PF={profit_factor:.2f}")

    by_reason: Dict[str, List[TradeOutcome]] = {}
    for o in outcomes:
        by_reason.setdefault(o.reason, []).append(o)
    print(f"  {'reason':<22} {'n':>4} {'sum_pnl':>10} {'avg_pnl':>10} {'avg_min':>9}")
    for reason, lst in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        s = sum(o.pnl for o in lst); a = s / len(lst)
        m = sum(o.duration_sec for o in lst) / len(lst) / 60
        print(f"  {reason:<22} {len(lst):>4} {s:>+10.4f} {a:>+10.4f} {m:>9.1f}")


def main():
    print(f"Fetching {DAYS}d of {INTERVAL}m candles for {len(SYMBOLS)} symbols…")
    data: Dict[str, List[dict]] = {}
    for s in SYMBOLS:
        data[s] = fetch_candles(s)
        print(f"  {s}: {len(data[s])} candles")

    old_all: List[TradeOutcome] = []
    new_all: List[TradeOutcome] = []
    for s, cs in data.items():
        if len(cs) < 50:
            continue
        old_all += simulate_symbol(cs, make_old_config(), "OLD", s, max_dd_pct=3.0)
        new_all += simulate_symbol(cs, make_new_config(), "NEW", s, max_dd_pct=6.0)

    summarize("OLD config (3% DD, no cooldown/recovery)", old_all)
    summarize("NEW config (6% DD, cooldown+recovery, 5% hard floor)", new_all)


if __name__ == "__main__":
    main()
