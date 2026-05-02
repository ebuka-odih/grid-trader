"""Paper-first Hummingbot execution adapter skeleton.

This adapter is deliberately conservative: it verifies Hummingbot paper mode
before writing deployment artifacts, stores generated configs/signals, and
returns normalized `GridExecutionState` objects. It does not place live orders.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .base import GridDeployRequest, GridExecutionState
from .hummingbot_config import HummingbotConfigGenerator


class HummingbotSafetyError(RuntimeError):
    """Raised when adapter safety gates block deployment."""


class HummingbotExecutionAdapter:
    """Hummingbot paper-mode adapter for grid-trader deployment requests."""

    def __init__(
        self,
        hummingbot_home: str | Path = "~/.hummingbot",
        allow_live: bool = False,
        backend_name: str = "hummingbot_paper",
    ):
        self.hummingbot_home = Path(hummingbot_home).expanduser()
        self.allow_live = allow_live
        self.backend_name = backend_name if allow_live else "hummingbot_paper"
        self.config_generator = HummingbotConfigGenerator(self.hummingbot_home)
        self.signals_path = self.hummingbot_home / "data" / "grid_trader_hummingbot_signals.json"
        self._states: dict[str, GridExecutionState] = {}

    def is_paper_mode_enabled(self) -> bool:
        """Return true when Hummingbot client config explicitly enables paper mode."""
        client_config = self.hummingbot_home / "conf" / "conf_client.yml"
        if not client_config.exists():
            return False
        text = client_config.read_text().lower()
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("paper_trade_enabled:"):
                value = line.split(":", 1)[1].strip().strip('"\'')
                return value in {"true", "yes", "1", "on"}
        return False

    def _assert_safe_to_deploy(self) -> None:
        if self.allow_live:
            return
        if not self.is_paper_mode_enabled():
            raise HummingbotSafetyError(
                "Hummingbot paper mode is not enabled; refusing deployment without allow_live=True"
            )

    async def deploy_grid(self, request: GridDeployRequest) -> GridExecutionState:
        """Write isolated config/signal and return an active normalized state."""
        self._assert_safe_to_deploy()
        generated = self.config_generator.write_strategy_config(request)
        grid_id = self._grid_id(request.symbol, generated.path)
        now = time.time()

        state = GridExecutionState(
            grid_id=grid_id,
            symbol=request.symbol,
            active=True,
            leverage=generated.leverage,
            grid_levels=self._build_grid_levels(request),
            metadata={
                "backend": self.backend_name,
                "exchange": request.exchange,
                "market": generated.market,
                "config_path": str(generated.path),
                "signal_path": str(self.signals_path),
                "cross_margin": request.cross_margin,
                "deployed_at": now,
            },
        )
        self._states[grid_id] = state
        self._write_signal(request, state)
        return state

    async def get_status(self, grid_id: str) -> GridExecutionState:
        try:
            return self._states[grid_id]
        except KeyError as exc:
            raise KeyError(f"unknown Hummingbot grid_id: {grid_id}") from exc

    async def stop_grid(self, grid_id: str, reason: str = "manual") -> GridExecutionState:
        state = await self.get_status(grid_id)
        if state.fills > 0 and state.total_pnl < 0 and reason != "emergency":
            state.close_reason = "hold_negative_pnl"
            state.metadata["last_stop_request"] = reason
            return state

        state.active = False
        state.close_reason = reason
        state.metadata["stopped_at"] = time.time()
        self._write_all_signals()
        return state

    async def close(self) -> None:
        self._write_all_signals()

    def _grid_id(self, symbol: str, config_path: Path) -> str:
        return f"hb:{symbol}:{config_path.stem.rsplit('_', 1)[-1]}"

    def _build_grid_levels(self, request: GridDeployRequest) -> list[dict]:
        levels = max(1, int(request.num_grids))
        step = (float(request.upper) - float(request.lower)) / levels
        grid_levels: list[dict] = []
        for index in range(levels + 1):
            price = float(request.lower) + (step * index)
            side = "buy" if index < levels / 2 else "sell"
            grid_levels.append(
                {
                    "level_id": index + 1,
                    "price": round(price, 10),
                    "side": side,
                    "order_size": float(request.margin_per_level_usdt),
                    "status": "open",
                    "order_id": None,
                    "fill_price": None,
                    "realized_pnl": 0.0,
                }
            )
        return grid_levels

    def _write_signal(self, request: GridDeployRequest, state: GridExecutionState) -> None:
        self.signals_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._read_signals_payload()
        payload["updated_at"] = time.time()
        payload.setdefault("grids", {})[state.grid_id] = {
            "grid_id": state.grid_id,
            "symbol": request.symbol,
            "exchange": request.exchange,
            "backend": self.backend_name,
            "active": state.active,
            "market": state.metadata.get("market"),
            "config_path": state.metadata.get("config_path"),
            "lower": request.lower,
            "upper": request.upper,
            "num_grids": request.num_grids,
            "leverage": state.leverage,
            "margin_per_level_usdt": request.margin_per_level_usdt,
            "direction": request.direction,
            "cross_margin": request.cross_margin,
            "close_reason": state.close_reason,
        }
        self.signals_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def _write_all_signals(self) -> None:
        self.signals_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._read_signals_payload()
        payload["updated_at"] = time.time()
        grids = payload.setdefault("grids", {})
        for grid_id, state in self._states.items():
            entry = grids.setdefault(grid_id, {"grid_id": grid_id, "symbol": state.symbol})
            entry.update(
                {
                    "active": state.active,
                    "leverage": state.leverage,
                    "close_reason": state.close_reason,
                    "total_pnl": state.total_pnl,
                    "fills": state.fills,
                }
            )
        self.signals_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def _read_signals_payload(self) -> dict:
        if not self.signals_path.exists():
            return {"source": "grid-trader", "grids": {}}
        try:
            return json.loads(self.signals_path.read_text())
        except json.JSONDecodeError:
            return {"source": "grid-trader", "grids": {}}
