"""
LiveEngine v4 — Real Bybit order placement with smart close logic.

v4 Changes:
- Uses grid_core.py for shared logic (position tracking, fill processing, close conditions)
- Smart negative-close: time decay, momentum exit, grid imbalance, trailing stop
- Same interface as DryRunEngine so multi_grid_manager can swap seamlessly
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from grid_engine import GridEngine, GridState, GridLevel
from coin_scanner import CoinScore
from config import (
    DRY_RUN, BASE_ORDER_SIZE_USDT, TARGET_PNL_PCT_LOW, TARGET_PNL_PCT_HIGH,
    MAX_DRAWDOWN_PCT,
)
from adaptive_grid import AdaptiveGrid, AdaptiveConfig, AdaptiveResult, default_config
from grid_core import (
    GridPosition, FillEvent, PnLResult, CycleState, GridImbalance,
    SmartCloseEngine, SmartCloseConfig, CloseReason,
    process_fill, check_close_conditions, reset_position, reset_imbalance,
    perform_partial_close,
    allocated_margin_usdt,
)

logger = logging.getLogger("live_engine")


def _smart_close_config_from_env(**overrides) -> SmartCloseConfig:
    """Build a SmartCloseConfig from env vars, allowing kwarg overrides."""
    def _f(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, default))
        except (TypeError, ValueError):
            return default

    cfg = SmartCloseConfig(
        time_decay_enabled=str(os.getenv("SMART_CLOSE_TIME_DECAY_ENABLED", "false")).lower()
        in ("1", "true", "yes", "on"),
        time_decay_hours=_f("SMART_CLOSE_TIME_DECAY_HOURS", 4.0),
        time_decay_min_loss_pct=_f("SMART_CLOSE_TIME_DECAY_MIN_LOSS_PCT", 1.0),
        momentum_exit_enabled=str(os.getenv("SMART_CLOSE_MOMENTUM_EXIT_ENABLED", "false")).lower()
        in ("1", "true", "yes", "on"),
        momentum_threshold_pct=_f("SMART_CLOSE_MOMENTUM_THRESHOLD_PCT", 2.5),
        momentum_window_sec=_f("SMART_CLOSE_MOMENTUM_WINDOW_SEC", 60.0),
        imbalance_close_enabled=str(os.getenv("SMART_CLOSE_IMBALANCE_CLOSE_ENABLED", "false")).lower()
        in ("1", "true", "yes", "on"),
        imbalance_ratio_threshold=_f("SMART_CLOSE_IMBALANCE_RATIO", 3.5),
        imbalance_min_fills=int(_f("SMART_CLOSE_IMBALANCE_MIN_FILLS", 8)),
        trailing_stop_enabled=str(os.getenv("SMART_CLOSE_TRAILING_STOP_ENABLED", "false")).lower()
        in ("1", "true", "yes", "on"),
        trailing_stop_initial_pct=_f("SMART_CLOSE_TRAILING_INITIAL_PCT", 3.5),
        trailing_stop_tightened_pct=_f("SMART_CLOSE_TRAILING_TIGHTENED_PCT", 2.5),
        trailing_stop_tighten_hours=_f("SMART_CLOSE_TRAILING_TIGHTEN_HOURS", 4.0),
        recovery_check_enabled=str(os.getenv("SMART_CLOSE_RECOVERY_CHECK_ENABLED", "false")).lower()
        in ("1", "true", "yes", "on"),
        recovery_min_depth_pct=_f("SMART_CLOSE_RECOVERY_MIN_DEPTH_PCT", 1.5),
        recovery_max_hours=_f("SMART_CLOSE_RECOVERY_MAX_HOURS", 6.0),
        min_seconds_since_last_fill=_f("SMART_CLOSE_POST_FILL_COOLDOWN_SEC", 180.0),
        recovery_window_sec=_f("SMART_CLOSE_RECOVERY_WINDOW_SEC", 300.0),
        recovery_partial_pct=_f("SMART_CLOSE_RECOVERY_PARTIAL_PCT", 30.0),
        hard_loss_pct_floor=_f("HARD_FLOOR_BASE_PCT", 40.0),
        hard_loss_pct_floor_min=_f("HARD_FLOOR_MIN_PCT", 30.0),
        hard_loss_pct_floor_max=_f("HARD_FLOOR_MAX_PCT", 55.0),
        scale_out_fraction=_f("SCALE_OUT_FRACTION", 0.5),
        # Patch J: post-scale-out cooldown (env-tunable).
        post_scale_out_cooldown_sec=_f("POST_SCALE_OUT_COOLDOWN_SEC", 60.0),
        min_position_age_sec=_f("SMART_CLOSE_MIN_POS_AGE_SEC", 90.0),
        imbalance_emergency_ratio=_f("IMBALANCE_EMERGENCY_RATIO", 4.0),
        imbalance_emergency_min_fills=int(_f("IMBALANCE_EMERGENCY_MIN_FILLS", 4)),
        imbalance_emergency_min_loss_pct=_f("IMBALANCE_EMERGENCY_MIN_LOSS_PCT", 8.0),
        imbalance_emergency_min_age_sec=_f("IMBALANCE_EMERGENCY_MIN_AGE_SEC", 60.0),
        tp_floor_pct=_f("DYNAMIC_TP_FLOOR_PCT", 3.0),
        tp_min_age_full_target_min=_f("DYNAMIC_TP_FULL_TARGET_MIN", 10.0),
        tp_decay_step_pct=_f("DYNAMIC_TP_DECAY_STEP_PCT", 2.0),
        tp_decay_to_zero_at_min=_f("DYNAMIC_TP_DECAY_TO_ZERO_MIN", 30.0),
        tp_dust_floor_usdt=_f("DYNAMIC_TP_DUST_FLOOR_USDT", 0.05),
        tp_momentum_max_age_min=_f("DYNAMIC_TP_MOMENTUM_MAX_AGE_MIN", 30.0),
        tp_momentum_velocity_pct_per_min=_f("DYNAMIC_TP_MOMENTUM_VELOCITY_PCT_PER_MIN", 1.5),
        tp_momentum_extend_max_pct=_f("DYNAMIC_TP_MOMENTUM_EXTEND_MAX_PCT", 5.0),
        tp_momentum_trailing_giveback_pct=_f("DYNAMIC_TP_TRAILING_GIVEBACK_PCT", 0.5),
        tp_min_fills=int(_f("DYNAMIC_TP_MIN_FILLS", 1)),
        # Patch D: env hooks for piecewise TP step curve (3/2/1/0 default).
        tp_step_curve_enabled=str(os.getenv("DYNAMIC_TP_STEP_CURVE", "true")).lower()
        in ("1", "true", "yes", "on"),
        tp_tier_1_min=_f("DYNAMIC_TP_TIER_1_MIN", 10.0),
        tp_tier_2_min=_f("DYNAMIC_TP_TIER_2_MIN", 15.0),
        tp_tier_3_min=_f("DYNAMIC_TP_TIER_3_MIN", 30.0),
        tp_tier_2_pct=_f("DYNAMIC_TP_TIER_2_PCT", 2.0),
        tp_tier_3_pct=_f("DYNAMIC_TP_TIER_3_PCT", 1.0),
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@dataclass
class LiveState:
    """Tracks live grid trading state."""
    grid: GridState
    started_at: float = 0.0
    current_price: float = 0.0
    # Position — delegated to GridPosition
    position: GridPosition = field(default_factory=GridPosition)
    # Fill tracking
    fills: List[Dict] = field(default_factory=list)
    filled_levels: set = field(default_factory=set)
    # State
    is_active: bool = True
    # Grid imbalance tracker
    imbalance: GridImbalance = field(default_factory=GridImbalance)
    # Cycle tracking
    max_cycles: int = 1
    # ATR % at deploy (frozen — drives ATR-bucketed hard floor)
    atr_pct: float = 0.0


class LiveEngine:
    """
    Real Bybit grid trading engine v4.
    
    Uses grid_core for:
    - Position tracking (GridPosition)
    - Fill processing (process_fill)
    - Close conditions (check_close_conditions)
    - Smart negative close (SmartCloseEngine)
    """
    
    def __init__(self, max_cycles: int = 1, smart_close_config: SmartCloseConfig = None,
                 adaptive_config: AdaptiveConfig = None, alerter=None):
        self.state: Optional[LiveState] = None
        self._grid_engine: Optional[GridEngine] = None
        self._adaptive: Optional[AdaptiveGrid] = None
        self._adaptive_config = adaptive_config or default_config()
        self._cycle_state = CycleState(max_cycles=max_cycles)
        self._smart_close = SmartCloseEngine(
            smart_close_config or _smart_close_config_from_env()
        )
        # Optional Telegram alerter — manager passes its own. None = no-op.
        self._alerter = alerter
        
        # WebSocket fill queue
        self._fill_queue: asyncio.Queue = asyncio.Queue()
        
        # Position sync
        self._last_position_sync = 0.0
        self._position_sync_interval = 10.0
        # Patch C: two-cycle confirmation before treating exchange "flat"
        # as truth. Bybit's REST `fetch_positions` occasionally returns a
        # snapshot that lags the most-recent fill by a few seconds; a
        # single transient flat reading would otherwise stomp our local
        # position state, blow away `opened_at`, and reset the
        # smart-close recovery window. Require N consecutive flat reads
        # before we honour them.
        self._consecutive_flat_syncs = 0
        self._flat_confirm_threshold = 2
        
        logger.info("🔴 LiveEngine v4 initialized — REAL orders + smart close")
    
    async def deploy_grid(self, coin_score: CoinScore) -> LiveState:
        """Deploy a real grid on Bybit."""
        from config import BYBIT_API_KEY, BYBIT_API_SECRET, TRADING_MODE
        
        self._grid_engine = GridEngine()
        grid = await self._grid_engine.deploy_grid(coin_score)
        
        self.state = LiveState(
            grid=grid,
            started_at=time.time(),
            current_price=coin_score.price,
            max_cycles=self._cycle_state.max_cycles,
            atr_pct=getattr(coin_score, "atr_pct", 0.0),
        )
        
        self._adaptive = AdaptiveGrid(
            config=self._adaptive_config,
            upper=coin_score.suggested_upper,
            lower=coin_score.suggested_lower,
            num_grids=coin_score.suggested_grids,
            base_order_size=BASE_ORDER_SIZE_USDT,
            leverage=coin_score.suggested_leverage,
        )
        
        for level in grid.grid_levels:
            level.status = "placed"
        
        logger.info(
            f"🔴 LIVE Grid deployed (v4): {grid.symbol} | "
            f"{grid.lower_price:.4f}-{grid.upper_price:.4f} | "
            f"{len(grid.grid_levels)} levels | lev={grid.leverage}x"
        )
        
        return self.state
    
    async def on_price_update(self, price: float) -> Optional[str]:
        """
        Process price update with smart close logic.
        
        Order: fill queue → position sync → close checks → smart close
        """
        if not self.state or not self.state.is_active:
            return None
        
        self.state.current_price = price
        
        # Update smart close price history
        self._smart_close.update_price(price)
        
        # Process any pending fills from WebSocket
        fill_event = await self._process_fill_queue()
        if fill_event:
            return fill_event
        
        # Sync position from exchange periodically
        now = time.time()
        if now - self._last_position_sync > self._position_sync_interval:
            await self._sync_position_from_exchange()
            self._last_position_sync = now
        
        # Update unrealized PnL
        self.state.position.update_unrealized(price)
        
        # ── Standard close checks ──
        margin = allocated_margin_usdt(
            self.state.grid.order_size_usdt, self.state.grid.num_grids
        )
        
        # `check_close_conditions` is configured with HIGH for both target args
        # so `should_close` only fires as the upper-ceiling backstop. Dynamic
        # take-profit handles every close below that ceiling.
        pnl_result = check_close_conditions(
            position=self.state.position,
            current_price=price,
            allocated_margin=margin,
            target_pnl_pct_low=8.0,
            target_pnl_pct_high=8.0,
            max_drawdown_pct=MAX_DRAWDOWN_PCT,
            total_fills=len(self.state.fills),
        )

        # ── Dynamic take-profit ──
        if pnl_result.total_pnl > 0 and not self.state.position.is_flat:
            tp_reason = self._smart_close.evaluate_take_profit(
                position=self.state.position,
                current_price=price,
                allocated_margin=margin,
                total_pnl=pnl_result.total_pnl,
                total_fills=len(self.state.fills),
            )
            if tp_reason:
                return await self._handle_close(tp_reason.value, pnl_result.total_pnl)

        if pnl_result.should_close:
            return await self._handle_close(pnl_result.close_reason, pnl_result.total_pnl)

        # ── Smart close on losing positions (drawdown routed through too) ──
        if not self.state.position.is_flat and pnl_result.total_pnl < 0:
            smart_reason = self._smart_close.check_smart_close(
                position=self.state.position,
                current_price=price,
                allocated_margin=margin,
                imbalance=self.state.imbalance,
                total_fills=len(self.state.fills),
                drawdown_breached=pnl_result.drawdown_breached,
                atr_pct=self.state.atr_pct,
            )
            if smart_reason == CloseReason.PARTIAL_CLOSE:
                # Live scale-out: send a reduce-only market order for half the
                # position via grid_engine.close_position (which sets
                # reduceOnly=True). Mirror the in-memory state with
                # perform_partial_close so the recovery window can run on
                # the remainder. If the exchange-side scale-out fails, DO NOT
                # force a full close here — that would realize the whole loss
                # immediately and violate the recovery-first policy. Instead,
                # keep the grid alive, alert loudly, and let the hard-floor /
                # recovery path re-evaluate on the next tick.
                close_qty = (
                    self.state.position.qty
                    * self._smart_close.config.scale_out_fraction
                )
                pos_side = self.state.position.side
                try:
                    order = await self._grid_engine.close_position(
                        self.state.grid.symbol, pos_side, close_qty
                    )
                except Exception as e:
                    logger.error(
                        f"⚖️  LIVE SCALE-OUT exchange call raised: {e} — "
                        f"holding position for recovery instead of force-closing"
                    )
                    order = None
                if not order:
                    logger.error(
                        f"⚖️  LIVE SCALE-OUT failed (no order ack) — "
                        f"holding position for recovery instead of force-closing"
                    )
                    await self._alert_critical(
                        f"⚖️ <b>LIVE SCALE-OUT FAILED</b> {self.state.grid.symbol}\n"
                        f"Requested reduce-only close of {close_qty:.6f} {pos_side} at hard floor, "
                        f"but received no exchange acknowledgement.\n"
                        f"<b>Holding remainder for recovery; no forced full close was sent.</b>"
                    )
                    return None
                # Mirror in-memory state so recovery window + scaled_out flag
                # are correctly tracked for the remainder of the position.
                realised, qty = perform_partial_close(
                    self.state.position, self.state.imbalance, price,
                    fraction=self._smart_close.config.scale_out_fraction,
                )
                # Patch G: do NOT reset_recovery() here. The whole point of
                # the partial-close-then-recovery-window flow is that the
                # window must persist across the scale-out so the remaining
                # half gets a real chance to bounce. The original call here
                # was wiping the window 300ms before the next breach hit and
                # closed the remainder, turning every imbalance/floor event
                # into a guaranteed loss.
                logger.warning(
                    f"⚖️  LIVE SCALE-OUT: closed {qty:.6f} {pos_side} @ ${price:.4f} | "
                    f"realised=${realised:.4f} | remaining={self.state.position.qty:.6f} | "
                    f"order_id={order.get('id', '?') if isinstance(order, dict) else '?'}"
                )
                if self._alerter:
                    try:
                        await self._alerter.send(
                            f"⚖️ <b>LIVE SCALE-OUT</b>\n"
                            f"📊 {self.state.grid.symbol}\n"
                            f"💰 Realised: <code>${realised:.4f}</code>\n"
                            f"⏳ Remaining qty: {self.state.position.qty:.6f}"
                        )
                    except Exception:
                        pass
                return "partial_close"
            if smart_reason:
                logger.info(
                    f"🧠 LIVE SMART CLOSE: {smart_reason.value} | "
                    f"PnL=${pnl_result.total_pnl:.4f} | "
                    f"dd_breached={pnl_result.drawdown_breached}"
                )
                return await self._handle_close(smart_reason.value, pnl_result.total_pnl)
        
        return None
    
    async def _handle_close(self, reason: str, total_pnl: float) -> str:
        """
        Handle grid close: cancel any open orders, market-close the
        remaining position, then mark the engine inactive. This MUST run
        every code path that returns a final close reason — leaving open
        orders or an open position on the exchange is a critical bug.
        """
        if self._cycle_state.max_cycles > 1:
            cycle_done = self._cycle_state.complete_cycle(total_pnl)
            logger.info(
                f"🔴 LIVE CYCLE {self._cycle_state.cycles_completed}/{self._cycle_state.max_cycles} | "
                f"reason={reason} | PnL=${total_pnl:.4f} | "
                f"cumulative=${self._cycle_state.cumulative_pnl:.4f}"
            )
            if cycle_done:
                logger.info(f"🛑 MAX CYCLES REACHED — flattening + cancelling")
                await self._flatten_and_cancel(reason)
                return reason
            else:
                await self._reset_grid_for_next_cycle(self.state.current_price)
                return "cycle_complete"
        else:
            logger.info(f"🔴 LIVE CLOSE | reason={reason} | PnL=${total_pnl:.4f}")
            await self._flatten_and_cancel(reason)
            return reason

    async def _flatten_and_cancel(self, reason: str):
        """
        Cancel all live grid orders and market-close any remaining position.
        Idempotent — safe to call multiple times; failures are logged but
        don't raise (we still flip is_active=False so the manager can free
        the slot rather than leaving the engine in a half-open state).
        """
        if not self.state:
            return
        # 1) Cancel all open grid orders first so they can't fill while we
        #    are flattening.
        try:
            await self.cancel_grid()
        except Exception as e:
            logger.error(f"_flatten_and_cancel: cancel_grid failed: {e}")
        # 1.5) Re-sync position from exchange BEFORE deciding whether to
        # market-close. Without this, a grid that placed at-the-market
        # limit orders and got filled before the WS subscription was live
        # (or before the periodic 10s sync ran) shows local qty=0 even
        # though Bybit holds a real position — we'd skip the flatten and
        # leave an orphan. Sync also picks up any fills that landed in
        # the gap between cancel_grid and now.
        try:
            await self._sync_position_from_exchange()
        except Exception as e:
            logger.error(f"_flatten_and_cancel: pre-flatten sync failed: {e}")
        # 2) Close any remaining position via reduce-only market order.
        if not self.state.position.is_flat and self.state.position.qty > 0:
            symbol = self.state.grid.symbol
            side = self.state.position.side
            qty = self.state.position.qty
            try:
                order = await self._grid_engine.close_position(symbol, side, qty)
                if order:
                    logger.info(
                        f"🔴 LIVE FLATTEN: closed {side} {qty:.6f} @ market "
                        f"({reason}) order_id={order.get('id', '?') if isinstance(order, dict) else '?'}"
                    )
                else:
                    logger.error(
                        f"🚨 LIVE FLATTEN: close_position returned None for {symbol} "
                        f"{side} {qty} — position may still be open!"
                    )
                    await self._alert_critical(
                        f"🚨 <b>FLATTEN FAILED</b> {symbol}\n"
                        f"close_position returned None for {side} {qty:.6f}\n"
                        f"<b>Position may still be open on exchange.</b>"
                    )
            except Exception as e:
                logger.error(
                    f"🚨 LIVE FLATTEN: close_position raised for {symbol}: {e} — "
                    f"position may still be open!"
                )
                await self._alert_critical(
                    f"🚨 <b>FLATTEN ERROR</b> {symbol}\n"
                    f"{type(e).__name__}: {str(e)[:200]}\n"
                    f"<b>Position may still be open on exchange.</b>"
                )
        self.state.is_active = False

    async def _alert_critical(self, html: str):
        """Best-effort Telegram alert for critical events; never raises."""
        if not self._alerter:
            return
        try:
            await self._alerter.send(html)
        except Exception as e:
            logger.error(f"Telegram alert failed: {e}")
    
    async def _process_fill_queue(self) -> Optional[str]:
        """Process fill events from WebSocket using grid_core.process_fill."""
        try:
            while not self._fill_queue.empty():
                fill_data = self._fill_queue.get_nowait()
                
                order_id = fill_data.get("order_id", "")
                side = fill_data.get("side", "")
                qty = float(fill_data.get("qty", 0))
                price = float(fill_data.get("price", 0))
                
                level = self._find_level_by_order_id(order_id)
                if level:
                    self.state.filled_levels.add(level.index)
                    
                    # Use grid_core to process fill
                    fill_event = FillEvent(
                        level_index=level.index,
                        side=side,
                        price=price,
                        qty=qty,
                        timestamp=time.time(),
                        order_id=order_id,
                    )
                    pnl = process_fill(fill_event, self.state.position, self.state.imbalance)
                    
                    # Record fill for status
                    fill_record = {
                        "side": side,
                        "price": price,
                        "qty": qty,
                        "timestamp": time.time(),
                        "order_id": order_id,
                        "pnl": pnl,
                    }
                    self.state.fills.append(fill_record)
                    
                    logger.info(
                        f"🔴 LIVE FILL: {side} {qty} @ {price:.4f} | "
                        f"PnL=${pnl:.4f} | "
                        f"pos={self.state.position.side} {self.state.position.qty:.6f}"
                    )
                    
                    # Check exposure cap
                    if self._adaptive:
                        from adaptive_grid import FillEvent as AdaptiveFillEvent
                        af = AdaptiveFillEvent(
                            timestamp=time.time(), side=side, price=price,
                            qty=qty, level_index=level.index,
                        )
                        self._adaptive.exposure_cap.record_fill(af)
                        
                        if not self._adaptive.exposure_cap.fills_allowed():
                            logger.warning("🔴 EXPOSURE CAP BREACH — pausing fills")
                            return "exposure_breach"
        
        except asyncio.QueueEmpty:
            pass
        
        return None
    
    def _find_level_by_order_id(self, order_id: str) -> Optional[GridLevel]:
        if not self.state:
            return None
        for level in self.state.grid.grid_levels:
            if level.order_id == order_id:
                return level
        return None
    
    async def _sync_position_from_exchange(self):
        """Sync position from Bybit API.

        Bybit sometimes returns ``None`` for ``contracts`` / ``entryPrice``
        when the account is flat — naive ``float(None)`` would raise and
        abort the whole sync, leaving stale local state. We coerce ``None``
        to ``0.0`` instead.

        We also stamp ``opened_at`` / ``last_fill_at`` whenever we observe a
        live position with no local timestamp (e.g. sync raced ahead of the
        WebSocket fill or we restarted while a position was already open).
        Without this, ``position.age_seconds`` stays at 0 forever and the
        ``min_position_age_sec`` guard + emergency-imbalance bypass in
        ``grid_core.check_close_conditions`` permanently block the
        hard-loss floor from firing.
        """
        if not self._grid_engine or not self.state:
            return

        try:
            symbol = self.state.grid.symbol
            positions = await self._grid_engine.exchange.fetch_positions([symbol])

            for pos in positions:
                if pos.get("symbol") != symbol:
                    continue
                qty = abs(float(pos.get("contracts") or 0))
                side = pos.get("side") or ""
                entry = float(pos.get("entryPrice") or 0)

                if qty > 0 and entry > 0:
                    self.state.position.qty = qty
                    self.state.position.side = "Buy" if side == "long" else "Sell"
                    self.state.position.entry_price = entry
                    # Stamp open/last-fill timestamps if missing so the
                    # smart-close engine's age-based guards work.
                    now_ts = time.time()
                    if self.state.position.opened_at <= 0:
                        self.state.position.opened_at = now_ts
                    if self.state.position.last_fill_at <= 0:
                        self.state.position.last_fill_at = now_ts
                    # Real position observed → reset flat-confirmation
                    # counter so a future flat reading must be confirmed
                    # again from scratch.
                    self._consecutive_flat_syncs = 0
                else:
                    # Patch C: don't trust a single "flat" reading. Bybit's
                    # REST snapshot can lag a fresh fill by ~1-3s; a stomp
                    # here zeroes opened_at/recovery and lets losses run
                    # uncapped on the next cycle. Require N consecutive
                    # flat reads before we honour them. While unconfirmed,
                    # leave local state untouched so smart-close keeps
                    # working from WS-sourced data.
                    if not self.state.position.is_flat:
                        self._consecutive_flat_syncs += 1
                        if self._consecutive_flat_syncs >= self._flat_confirm_threshold:
                            logger.info(
                                f"Position sync: confirmed flat after "
                                f"{self._consecutive_flat_syncs} syncs — resetting local state"
                            )
                            reset_position(self.state.position)
                            self._consecutive_flat_syncs = 0
                        else:
                            logger.debug(
                                f"Position sync: exchange reports flat "
                                f"({self._consecutive_flat_syncs}/{self._flat_confirm_threshold}) — "
                                f"holding local state pending confirmation"
                            )
                    else:
                        # Both local and exchange are flat — fully consistent.
                        self._consecutive_flat_syncs = 0

                logger.debug(f"Position synced: {qty} {side} @ {entry}")
                break

        except Exception as e:
            logger.error(f"Position sync failed: {e}")
    
    async def _reset_grid_for_next_cycle(self, price: float):
        """Reset grid for next trading cycle."""
        if not self.state:
            return
        
        await self.cancel_grid()
        
        reset_position(self.state.position)
        reset_imbalance(self.state.imbalance)
        self._smart_close.reset_recovery()
        self._smart_close.reset_tp_peak()
        self.state.filled_levels.clear()

        if self._adaptive:
            self._adaptive.exposure_cap.exposure.buy_fills = 0
            self._adaptive.exposure_cap.exposure.sell_fills = 0
            self._adaptive.exposure_cap.exposure.consecutive_same_side = 0
        
        for level in self.state.grid.grid_levels:
            level.status = "placed"
            level.order_id = None
        
        if self._grid_engine:
            for level in self.state.grid.grid_levels:
                try:
                    order = await self._grid_engine.exchange.create_limit_order(
                        symbol=self.state.grid.symbol,
                        side=level.side.lower(),
                        amount=level.qty,
                        price=level.price,
                    )
                    level.order_id = order["id"]
                    level.status = "placed"
                except Exception as e:
                    logger.error(f"Failed to place order: {e}")
                    level.status = "failed"
        
        logger.info(
            f"🔄 LIVE GRID RESET for cycle {self._cycle_state.cycles_completed + 1}/{self._cycle_state.max_cycles} | "
            f"{len(self.state.grid.grid_levels)} levels | "
            f"cumulative=${self._cycle_state.cumulative_pnl:.4f}"
        )
    
    async def cancel_grid(self):
        if self._grid_engine and self.state:
            await self._grid_engine.cancel_grid(self.state.grid)
    
    def _allocated_margin_usdt(self) -> float:
        """Margin reserved by this grid (post-fills, otherwise allocation budget)."""
        if not self.state:
            return 0.0
        # Match dry_run_engine: order_size_usdt is notional per level; margin = notional/leverage.
        try:
            return allocated_margin_usdt(
                self.state.grid.order_size_usdt,
                self.state.grid.num_grids,
            )
        except Exception:
            level_count = len(self.state.grid.grid_levels) or self.state.grid.num_grids or 1
            lev = max(1, self.state.grid.leverage or 1)
            return float(self.state.grid.order_size_usdt or 0.0) * level_count / lev

    def _target_pnl_low_usdt(self) -> float:
        return self._allocated_margin_usdt() * TARGET_PNL_PCT_LOW / 100.0

    def _target_pnl_high_usdt(self) -> float:
        return self._allocated_margin_usdt() * TARGET_PNL_PCT_HIGH / 100.0

    def get_status(self) -> Dict[str, Any]:
        if not self.state:
            return {"active": False}

        s = self.state
        pos = s.position
        total = pos.realized_pnl + pos.unrealized_pnl
        duration = time.time() - (s.started_at or time.time())
        allocated = self._allocated_margin_usdt()

        status = {
            "active": s.is_active,
            "dry_run": False,
            "live": True,
            "grid_id": s.grid.grid_id,
            "symbol": s.grid.symbol,
            "current_price": s.current_price,
            "upper": s.grid.upper_price,
            "lower": s.grid.lower_price,
            "leverage": s.grid.leverage,
            "num_grids": s.grid.num_grids,
            "position_side": pos.side,
            "position_qty": round(pos.qty, 6),
            "entry_price": pos.entry_price,
            "realized_pnl": round(pos.realized_pnl, 4),
            "unrealized_pnl": round(pos.unrealized_pnl, 4),
            "total_pnl": round(total, 4),
            "fills": len(s.fills),
            "filled_levels": len(s.filled_levels),
            "cycles_completed": self._cycle_state.cycles_completed,
            "max_cycles": self._cycle_state.max_cycles,
            "position_age_hours": round(pos.age_hours, 1),
            "imbalance_ratio": round(s.imbalance.imbalance_ratio, 2),
            "fill_log": [
                {
                    "side": f.get("side", "unknown"),
                    "price": f.get("price", 0),
                    "qty": f.get("qty", 0),
                    "timestamp": f.get("timestamp", 0),
                    "pnl": f.get("pnl", 0),
                }
                for f in s.fills[-50:]
            ],
            "grid_levels": [
                {
                    "index": getattr(lv, "index", i),
                    "price": getattr(lv, "price", 0.0),
                    "side": getattr(lv, "side", "Buy"),
                    "status": getattr(lv, "status", "pending"),
                }
                for i, lv in enumerate(s.grid.grid_levels)
            ],
            "duration_sec": round(duration, 1),
            "allocated_margin_usdt": round(allocated, 4),
            "target_pnl_low": round(self._target_pnl_low_usdt(), 4),
            "target_pnl_high": round(self._target_pnl_high_usdt(), 4),
            "target_pnl_pct_low": TARGET_PNL_PCT_LOW,
            "target_pnl_pct_high": TARGET_PNL_PCT_HIGH,
            "max_drawdown_pct": MAX_DRAWDOWN_PCT,
        }

        if self._adaptive:
            adaptive_status = self._adaptive.status
            status["v3_adaptive"] = {
                "spike_active": adaptive_status["spike_active"],
                "spike_direction": adaptive_status["spike_state"]["direction"],
                "exposure_buy_fills": adaptive_status["exposure"]["buy_fills"],
                "exposure_sell_fills": adaptive_status["exposure"]["sell_fills"],
                "exposure_consecutive": adaptive_status["exposure"]["consecutive_same_side"],
                "exposure_breached": adaptive_status["exposure"]["breached"],
                "imbalance_ratio": adaptive_status["exposure"]["imbalance_ratio"],
                "recenters": adaptive_status["recenters"],
                "range_pct": adaptive_status["range_pct"],
                "trailing_enabled": adaptive_status["trailing_enabled"],
            }
            status["upper"] = adaptive_status["upper"]
            status["lower"] = adaptive_status["lower"]

        return status
    
    def notify_fill(self, order_id: str, side: str, qty: float, price: float):
        """Called by WebSocket handler when an order fills."""
        self._fill_queue.put_nowait({
            "order_id": order_id,
            "side": side,
            "qty": qty,
            "price": price,
        })
    
    async def close(self):
        """Clean shutdown."""
        if self.state and self.state.is_active:
            await self.cancel_grid()
            self.state.is_active = False
