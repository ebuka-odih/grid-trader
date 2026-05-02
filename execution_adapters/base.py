"""Common execution adapter contract for grid deployments.

The manager/scanner/risk layer should depend on these types, not directly on a
specific exchange engine. That lets the existing dry-run/custom Bybit path stay
available while a Hummingbot-backed executor is introduced safely in paper mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class GridDeployRequest:
    """Request to deploy one dense grid for one symbol."""

    symbol: str
    lower: float
    upper: float
    num_grids: int
    leverage: int
    margin_per_level_usdt: float
    direction: str = "neutral"
    exchange: str = "hyperliquid_perpetual"
    cross_margin: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class GridExecutionState:
    """Normalized state returned by any grid execution backend."""

    grid_id: str
    symbol: str
    active: bool
    current_price: float = 0.0
    total_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fills: int = 0
    position_qty: float = 0.0
    avg_entry_price: float = 0.0
    leverage: int = 1
    grid_levels: list[dict] = field(default_factory=list)
    close_reason: str = ""
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class GridExecutionAdapter(Protocol):
    """Runtime-checkable async adapter interface for grid execution."""

    async def deploy_grid(self, request: GridDeployRequest) -> GridExecutionState:
        """Deploy one grid and return normalized state."""

    async def get_status(self, grid_id: str) -> GridExecutionState:
        """Return latest normalized state for a deployed grid."""

    async def stop_grid(self, grid_id: str, reason: str = "manual") -> GridExecutionState:
        """Stop or request stop for a deployed grid."""

    async def close(self) -> None:
        """Release adapter resources."""
