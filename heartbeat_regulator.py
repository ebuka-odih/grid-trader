"""Heartbeat regulator for the multi-grid trader.

The heartbeat is a deterministic supervisor loop that periodically verifies
core subsystems are fresh and coordinated. It does not make trading decisions;
it regulates runtime hygiene: wallet sync, risk checks, stale price detection,
and deployment pausing when market-data health is questionable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("heartbeat_regulator")


@dataclass
class HeartbeatSnapshot:
    timestamp: float
    active_grids: int
    price_bus: dict
    stale_symbols: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


class HeartbeatRegulator:
    def __init__(
        self,
        manager,
        interval_seconds: int = 15,
        max_tick_age_seconds: int = 300,
        pause_seconds: int = 10,
        close_stale_after_seconds: int = 600,  # only force-close after 10min stale
        min_stale_to_pause: int = 2,  # need multiple stale symbols to pause
        now_fn: Callable[[], float] | None = None,
    ):
        self.manager = manager
        self.interval_seconds = interval_seconds
        self.max_tick_age_seconds = max_tick_age_seconds
        self.pause_seconds = pause_seconds
        self.close_stale_after_seconds = close_stale_after_seconds
        self.min_stale_to_pause = min_stale_to_pause
        self._now_fn = now_fn or time.time
        self.last_snapshot: HeartbeatSnapshot | None = None
        self._stale_since: dict[str, float] = {}  # symbol -> first seen stale time

    async def run(self):
        logger.info(
            "💓 Heartbeat regulator started | interval=%ss | stale_tick=%ss",
            self.interval_seconds,
            self.max_tick_age_seconds,
        )
        while getattr(self.manager, "_running", False):
            try:
                await self.beat()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("💓 Heartbeat error: %s", e, exc_info=True)
            await asyncio.sleep(self.interval_seconds)

    async def beat(self) -> HeartbeatSnapshot:
        now = self._now_fn()
        actions: list[str] = []
        if getattr(self.manager, "_deployment_paused_until", 0) <= now:
            setattr(self.manager, "_pause_reason", None)

        # Keep accounting and risk state fresh even if no deployment cycle is running.
        update_wallet = getattr(self.manager, "_update_wallet_tracker", None)
        if update_wallet:
            update_wallet()

        run_risk = getattr(self.manager, "_run_emergency_checks", None)
        if run_risk:
            await run_risk()

        price_bus = getattr(self.manager, "price_bus", None)
        price_health = price_bus.health_status() if price_bus and hasattr(price_bus, "health_status") else {}
        active_symbols = sorted({getattr(slot, "symbol", None) for slot in getattr(self.manager, "slots", {}).values() if getattr(slot, "symbol", None)})

        if price_health and not price_health.get("running", True):
            self._pause_deployments(now, actions, "price_bus_down")

        stale_symbols: list[str] = []
        for symbol in active_symbols:
            age = None
            if hasattr(price_bus, "latest_price_age"):
                age = price_bus.latest_price_age(symbol)
            if age is None:
                slot_age = self._youngest_slot_age(symbol, now)
                if slot_age is not None and slot_age <= self.max_tick_age_seconds:
                    continue
                stale_symbols.append(symbol)
            elif age > self.max_tick_age_seconds:
                stale_symbols.append(symbol)

        # Track when each symbol first went stale
        for sym in stale_symbols:
            if sym not in self._stale_since:
                self._stale_since[sym] = now
        # Clear symbols that are no longer stale
        for sym in list(self._stale_since):
            if sym not in stale_symbols:
                del self._stale_since[sym]

        # Only pause if multiple symbols are stale (single stale = log + hold)
        if stale_symbols and len(stale_symbols) >= self.min_stale_to_pause:
            self._pause_deployments(now, actions, "stale_price_data")

        # Only force-close grids that have been stale for > close_stale_after_seconds
        long_stale = [sym for sym in stale_symbols if now - self._stale_since.get(sym, now) > self.close_stale_after_seconds]
        if long_stale:
            self._close_stale_slots(long_stale, actions)
        elif stale_symbols:
            # Just log, don't close
            actions.append(f"watching_stale:{','.join(stale_symbols)}")

        snapshot = HeartbeatSnapshot(
            timestamp=now,
            active_grids=len(getattr(self.manager, "slots", {})),
            price_bus=price_health,
            stale_symbols=stale_symbols,
            actions=actions,
        )
        self.last_snapshot = snapshot

        push_state = getattr(self.manager, "_push_api_state", None)
        if push_state:
            try:
                push_state()
            except Exception as e:
                logger.warning("💓 Heartbeat state push failed: %s", e)

        logger.info(
            "💓 heartbeat | active=%s | stale=%s | actions=%s | price_bus=%s",
            snapshot.active_grids,
            len(stale_symbols),
            actions or "none",
            {k: v for k, v in price_health.items() if k != "last_successful_recv"},
        )
        return snapshot

    def _youngest_slot_age(self, symbol: str, now: float) -> float | None:
        ages = []
        for slot in getattr(self.manager, "slots", {}).values():
            if getattr(slot, "symbol", None) == symbol and hasattr(slot, "started_at"):
                ages.append(max(0.0, now - float(slot.started_at)))
        return min(ages) if ages else None

    def _pause_deployments(self, now: float, actions: list[str], reason: str):
        paused_until = now + self.pause_seconds
        current = getattr(self.manager, "_deployment_paused_until", 0) or 0
        setattr(self.manager, "_deployment_paused_until", max(current, paused_until))
        setattr(self.manager, "_pause_reason", reason)
        action = f"pause_deployments:{reason}"
        if action not in actions:
            actions.append(action)

    def _close_stale_slots(self, stale_symbols: list[str], actions: list[str]):
        stale_set = set(stale_symbols)
        for slot_id, slot in list(getattr(self.manager, "slots", {}).items()):
            if getattr(slot, "symbol", None) not in stale_set:
                continue
            status = {}
            engine = getattr(slot, "engine", None)
            if engine is not None and hasattr(engine, "get_status"):
                try:
                    status = engine.get_status() or {}
                except Exception as e:
                    logger.warning("💓 Could not read stale slot status for %s: %s", getattr(slot, "symbol", None), e)
            fills = int(status.get("fills") or 0)
            total_pnl = float(status.get("total_pnl") or 0.0)
            if fills > 0 and total_pnl < 0:
                logger.warning(
                    "💓 Holding stale negative grid instead of closing | slot=%s | symbol=%s | fills=%s | pnl=$%.4f",
                    slot_id,
                    getattr(slot, "symbol", None),
                    fills,
                    total_pnl,
                )
                actions.append(f"hold_negative_stale_grid:{slot_id}:{slot.symbol}")
                continue
            state = getattr(slot, "state", None)
            if state is not None and getattr(state, "is_active", False):
                state.is_active = False
            slot.close_reason = "heartbeat_stale_price"

            # Record PnL to DB before force-removing
            try:
                realized = float(status.get("realized_pnl") or 0.0)
                unrealized = float(status.get("unrealized_pnl") or 0.0)
                duration = time.time() - getattr(slot, "started_at", time.time())
                journal = getattr(self.manager, "journal", None)
                grid_state = getattr(slot, "state", None)
                grid_obj = getattr(grid_state, "grid", None) if grid_state else None
                grid_id = getattr(grid_obj, "grid_id", None) or f"slot-{slot_id}"
                decision = getattr(slot, "decision", None)
                direction = getattr(decision, "direction", "neutral") if decision else "neutral"
                if journal and hasattr(journal, "record_cycle_close"):
                    wallet_tracker = getattr(self.manager, "wallet_tracker", None)
                    wallet_state = wallet_tracker.get_wallet_state() if wallet_tracker else {}
                    journal.record_cycle_close(
                        grid_id=grid_id,
                        total_pnl=total_pnl,
                        realized_pnl=realized,
                        unrealized_pnl=unrealized,
                        fills=fills,
                        duration=duration,
                        close_reason="heartbeat_stale_price",
                        wallet_balance=wallet_state.get("balance", 0.0),
                        wallet_exposure_pct=wallet_state.get("exposure_pct", 0.0),
                        direction=direction,
                        adjusted_leverage=getattr(slot, "adjusted_leverage", 0),
                        adjusted_order_size=getattr(slot, "adjusted_order_size", 0.0),
                    )
                    logger.info("📝 Stale slot PnL saved: %s | pnl=$%.4f | fills=%s", slot.symbol, total_pnl, fills)
            except Exception as e:
                logger.error("Failed to save stale slot PnL for %s: %s", getattr(slot, "symbol", None), e)

            task = getattr(slot, "task", None)
            if task is not None and hasattr(task, "done") and not task.done():
                task.cancel()
            # Force-remove the slot from the manager so it can't block new deployments.
            # If cleanup (_on_grid_closed) runs later, it will find the slot already gone
            # and skip its own pop.
            slots = getattr(self.manager, "slots", {})
            if slot_id in slots:
                slots.pop(slot_id, None)
                logger.info(f"💓 Force-removed stale slot #{slot_id} ({slot.symbol})")
            actions.append(f"close_stale_grid:{slot_id}:{slot.symbol}")
