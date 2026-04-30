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
        max_tick_age_seconds: int = 90,
        pause_seconds: int = 30,
        now_fn: Callable[[], float] | None = None,
    ):
        self.manager = manager
        self.interval_seconds = interval_seconds
        self.max_tick_age_seconds = max_tick_age_seconds
        self.pause_seconds = pause_seconds
        self._now_fn = now_fn or time.time
        self.last_snapshot: HeartbeatSnapshot | None = None

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

        if stale_symbols:
            self._pause_deployments(now, actions, "stale_price_data")
            self._close_stale_slots(stale_symbols, actions)

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
            price_health,
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
            task = getattr(slot, "task", None)
            if task is not None and hasattr(task, "done") and not task.done():
                task.cancel()
            actions.append(f"close_stale_grid:{slot_id}:{slot.symbol}")
