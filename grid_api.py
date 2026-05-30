"""
Grid Trader API — FastAPI + WebSocket server.
Serves live bot state to the grid_terminal UI.
Runs alongside the bot, not embedded in it.

Usage:
    # Start bot + API together:
    ./run.sh multi --api

    # Or API-only (for read-only monitoring):
    python3 grid_api.py
"""

import asyncio
import logging
import time
import json
import sqlite3
import os
import subprocess
from pathlib import Path
from typing import Optional

# Overlay runtime_config + decrypted secrets onto os.environ BEFORE any
# downstream module reads env. Keep this above the FastAPI import too —
# admin.py imports runtime_config; doing it here makes the order obvious.
import runtime_config  # noqa: F401  side-effect: applies overlay on import
from config import (
    DEFAULT_LEVERAGE,
    EMERGENCY_LIQUIDATION_BUFFER_PCT,
    MAX_SINGLE_DIRECTION_EXPOSURE_PCT,
    MAX_TOTAL_WALLET_EXPOSURE_PCT,
    MAX_TRADE_WALLET_EXPOSURE_PCT,
    PORTFOLIO_RESERVE_PCT,
    clamp_leverage,
)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ── Paths ────────────────────────────────────────────────
GRID_TRADER_ROOT = Path(__file__).parent.resolve()
UI_DIR = Path(os.getenv("GRID_TRADER_UI_DIR", str(GRID_TRADER_ROOT / "frontend_dist"))).expanduser()
UI_DIST_DIR = Path(os.getenv("GRID_TRADER_UI_DIST_DIR", str(UI_DIR if UI_DIR.name == "frontend_dist" else UI_DIR / "dist"))).expanduser()

# ── Dynamic State File Selection ─────────────────────────────
# Always use the most recently modified state file for fresh data
from pathlib import Path as _Path

def _get_state_file() -> _Path:
    """Return the most recently modified state file."""
    main_file = _Path(os.getenv("GRID_TRADER_STATE_FILE", "/tmp/grid_trader_state.json"))
    test_file = _Path(os.getenv("GRID_TRADER_TEST_STATE_FILE", "/tmp/grid_trader_test_state.json"))
    
    candidates = []
    if main_file.exists():
        candidates.append((main_file, main_file.stat().st_mtime))
    if test_file.exists():
        candidates.append((test_file, test_file.stat().st_mtime))
    
    if not candidates:
        return main_file  # Default fallback
    
    # Sort by mtime descending and return newest
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]

BOT_STATE_FILE = _get_state_file()
MANUAL_CLOSE_REQUESTS_FILE = runtime_config.DATA_DIR / "manual_close_requests.jsonl"

# ── Logging ──────────────────────────────────────────────
logger = logging.getLogger("grid_api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)

logger.info(f"Using state file: {BOT_STATE_FILE}")


def _runtime_portfolio_defaults() -> dict:
    """Return live portfolio caps from runtime/env with config fallbacks."""

    def _env_float(key: str, default: float) -> float:
        try:
            return float(os.getenv(key, default))
        except (TypeError, ValueError):
            return float(default)

    max_total = _env_float("MAX_TOTAL_WALLET_EXPOSURE_PCT", MAX_TOTAL_WALLET_EXPOSURE_PCT)
    reserve_pct = _env_float("PORTFOLIO_RESERVE_PCT", PORTFOLIO_RESERVE_PCT)
    max_single_direction = _env_float(
        "MAX_SINGLE_DIRECTION_EXPOSURE_PCT",
        MAX_SINGLE_DIRECTION_EXPOSURE_PCT,
    )
    max_trade = _env_float("MAX_TRADE_WALLET_EXPOSURE_PCT", MAX_TRADE_WALLET_EXPOSURE_PCT)
    emergency_buffer = _env_float(
        "EMERGENCY_LIQUIDATION_BUFFER_PCT",
        EMERGENCY_LIQUIDATION_BUFFER_PCT,
    )
    return {
        "max_exposure_pct": round(max_total, 4),
        "reserved_pct": round(reserve_pct, 4),
        "max_total_wallet_exposure_pct": round(max_total, 4),
        "max_single_direction_exposure_pct": round(max_single_direction, 4),
        "max_trade_wallet_exposure_pct": round(max_trade, 4),
        "reserve_pct": round(reserve_pct, 4),
        "emergency_liquidation_buffer_pct": round(emergency_buffer, 4),
    }


def _append_manual_close_request(slot_id: int, slot: dict, reason: str = "manual_close") -> dict:
    runtime_config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    request = {
        "slot_id": int(slot_id),
        "trade_id": slot.get("trade_id") or slot.get("grid_id") or f"slot_{slot_id}",
        "symbol": slot.get("symbol"),
        "reason": reason,
        "requested_at": time.time(),
    }
    with MANUAL_CLOSE_REQUESTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(request) + "\n")
    return request

# ── In-memory state cache
# Written by the bot's main thread/task, read by API handlers.
# This is a simple in-process dict — no external DB needed for live state.
DEFAULT_STATE: dict = {
    "mode": "stopped",        # stopped | running | paused
    "started_at": None,
    "wallet": {
        "balance": 100.0,
        "exposure_pct": 0.0,
        "total_exposure_usdt": 0.0,
        "position_count": 0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
    },
    "portfolio": _runtime_portfolio_defaults(),
    "slots": {},              # slot_id -> slot data
    "completed_trades": [],   # last 50 closed trades
    "scanner_candidates": [], # top 10 scanner picks
    "deploy_rejections": [],  # active/recent deploy rejection reasons + cooldowns
    "deploy_diagnostics": {
        "free_slots": 0,
        "raw_candidates": 0,
        "post_capacity_candidates": 0,
        "post_prefilter_candidates": 0,
        "picked_candidates": 0,
        "active_rejections": 0,
        "recent_rejections": [],
        "rejection_cooldown_seconds": 0,
        "min_rejection_cooldown_seconds": 0,
    },
    "events": [],             # recent backend events for the dashboard log panel
    "heartbeat": {
        "active": 0,
        "stale": 0,
        "last_run": None,
        "actions": [],
        "price_bus_up": True,
        "paused": False,
        "pause_reason": None,
    },
    "stats": {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
    },
    "current_prices": {},     # symbol -> price
    "last_update": None,
}
_state: dict = json.loads(json.dumps(DEFAULT_STATE))
_last_loaded_state_mtime = 0.0
DB_PATH = Path(os.getenv("GRID_TRADER_DB_FILE", str(GRID_TRADER_ROOT / "multi_grid_trades.db"))).expanduser()


def _parse_simple_hummingbot_client_config(path: Path) -> dict:
    """Parse the small subset of conf_client.yml needed by the dashboard."""
    result = {
        "exists": path.exists(),
        "paper_trade_enabled": False,
        "instance_id": None,
        "heartbeat_enabled": False,
        "heartbeat_interval_min": None,
        "paper_trade_account_balance": {},
    }
    if not path.exists():
        return result

    in_balance = False
    for raw in path.read_text(errors="ignore").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("paper_trade_account_balance:"):
            in_balance = True
            continue
        if raw and not raw.startswith(" "):
            in_balance = False
        if in_balance and ":" in raw:
            key, value = raw.strip().split(":", 1)
            try:
                result["paper_trade_account_balance"][key] = float(value.strip().strip('"\''))
            except ValueError:
                pass
            continue
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"\'')
        if key == "instance_id":
            result["instance_id"] = value
        elif key == "paper_trade_enabled":
            result["paper_trade_enabled"] = value.lower() in {"true", "1", "yes", "on"}
        elif key == "heartbeat_enabled":
            result["heartbeat_enabled"] = value.lower() in {"true", "1", "yes", "on"}
        elif key == "heartbeat_interval_min":
            try:
                result["heartbeat_interval_min"] = float(value)
            except ValueError:
                pass
    return result


def _hummingbot_docker_status() -> dict:
    """Best-effort local Docker status without failing API if Docker is unavailable."""
    base = {"container_name": "hummingbot-paper", "container_running": False, "command": None}
    try:
        proc = subprocess.run(
            ["docker", "inspect", "hummingbot-paper", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return base
        info = json.loads(proc.stdout)
        state = info.get("State", {})
        config = info.get("Config", {})
        base["container_running"] = bool(state.get("Running"))
        base["command"] = " ".join(config.get("Cmd") or [])
    except Exception:
        return base
    return base


def _read_dotenv_value(key: str, default: str | None = None) -> str | None:
    env_path = GRID_TRADER_ROOT / ".env"
    if not env_path.exists():
        return default
    for raw in env_path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"\'')
    return default


def _load_hummingbot_status() -> dict:
    """Load Hummingbot adapter/config/signal status for the custom dashboard."""
    backend = os.getenv("EXECUTION_BACKEND") or _read_dotenv_value("EXECUTION_BACKEND", "dry_run")
    exchange = os.getenv("HUMMINGBOT_EXCHANGE") or _read_dotenv_value("HUMMINGBOT_EXCHANGE", "hyperliquid_perpetual")
    allow_live_raw = os.getenv("HUMMINGBOT_ALLOW_LIVE") or _read_dotenv_value("HUMMINGBOT_ALLOW_LIVE", "false") or "false"
    home = Path(os.getenv("HUMMINGBOT_HOME") or _read_dotenv_value("HUMMINGBOT_HOME", "/home/forge1/.hummingbot")).expanduser()
    signals_path = home / "data" / "grid_trader_hummingbot_signals.json"
    generated_dir = home / "conf" / "generated" / "grid_trader"
    now = time.time()

    payload = {}
    grids = {}
    if signals_path.exists():
        try:
            payload = json.loads(signals_path.read_text())
            grids = payload.get("grids", {}) if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            grids = {}

    active_grids = [g for g in grids.values() if isinstance(g, dict) and g.get("active")]
    active_grids.sort(key=lambda g: str(g.get("grid_id", "")), reverse=True)
    config_files = []
    if generated_dir.exists():
        config_files = sorted(generated_dir.glob("*.yml"), key=lambda p: p.stat().st_mtime, reverse=True)

    return {
        "backend": backend,
        "exchange": exchange,
        "allow_live": allow_live_raw.strip().lower() in {"1", "true", "yes", "on"},
        "hummingbot_home": str(home),
        "client_config": _parse_simple_hummingbot_client_config(home / "conf" / "conf_client.yml"),
        "signals": {
            "exists": signals_path.exists(),
            "path": str(signals_path),
            "age_seconds": round(now - signals_path.stat().st_mtime, 2) if signals_path.exists() else None,
            "total_grids": len(grids),
            "active_grids": len(active_grids),
            "source": payload.get("source") if isinstance(payload, dict) else None,
            "updated_at": payload.get("updated_at") if isinstance(payload, dict) else None,
        },
        "generated_configs": {
            "count": len(config_files),
            "dir": str(generated_dir),
            "newest": [
                {"name": p.name, "path": str(p), "age_seconds": round(now - p.stat().st_mtime, 2)}
                for p in config_files[:10]
            ],
        },
        "docker": _hummingbot_docker_status(),
        "active_samples": active_grids[:25],
    }


def _load_db_performance(limit: int = 50) -> tuple[dict, list[dict]]:
    """Load closed-trade performance from SQLite for dashboard source-of-truth stats."""
    if not DB_PATH.exists():
        return {}, []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        existing_cols = {r[1] for r in cursor.execute("PRAGMA table_info(grid_cycles)").fetchall()}
        order_size_expr = (
            "adjusted_order_size" if "adjusted_order_size" in existing_cols
            else "0.0 AS adjusted_order_size"
        )
        # Only count trades with actual fills (exclude no_fills_timeout, price_bus_error, etc.)
        cursor.execute("SELECT COUNT(*) FROM grid_cycles WHERE closed_at IS NOT NULL AND COALESCE(fills_count, 0) > 0")
        total_trades = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM grid_cycles WHERE closed_at IS NOT NULL AND COALESCE(fills_count, 0) > 0 AND COALESCE(total_pnl, 0) > 0")
        wins = int(cursor.fetchone()[0] or 0)
        losses = max(0, total_trades - wins)
        cursor.execute("SELECT COALESCE(SUM(total_pnl), 0) FROM grid_cycles WHERE closed_at IS NOT NULL AND COALESCE(fills_count, 0) > 0")
        total_pnl = float(cursor.fetchone()[0] or 0.0)
        win_rate = (wins / total_trades * 100) if total_trades else 0.0
        # History list shows ALL closed cycles — including 0-fill grids that were
        # deployed and recycled without trading (close_reason=no_fills_timeout).
        # Stats above stay filtered to fills>0 so empties don't dilute win-rate/PnL,
        # but the list must show them so the active-count dropping (e.g. 13->7) is
        # explained instead of looking like trades vanished silently.
        cursor.execute(f"""
            SELECT grid_id, symbol, started_at, closed_at, close_reason,
                   total_pnl, realized_pnl, fills_count, duration_seconds,
                   upper_price, lower_price, num_grids, leverage, was_profitable,
                   {order_size_expr}
            FROM grid_cycles
            WHERE closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT ?
        """, (limit,))
        trades = []
        for row in cursor.fetchall():
            order_size = float(row["adjusted_order_size"] or 0.0)
            num_grids = int(row["num_grids"] or 0)
            allocated_margin = order_size * num_grids
            total_pnl = float(row["total_pnl"] or 0.0)
            profit_pct = (total_pnl / allocated_margin * 100) if allocated_margin > 0 else 0.0
            trades.append({
                "slot_id": row["grid_id"],
                "symbol": row["symbol"],
                "started_at": row["started_at"],
                "closed_at": row["closed_at"],
                "close_reason": row["close_reason"],
                "total_pnl": total_pnl,
                "realized_pnl": row["realized_pnl"] or 0.0,
                "fills_count": row["fills_count"] or 0,
                "duration_seconds": row["duration_seconds"] or 0,
                "upper_price": row["upper_price"] or 0.0,
                "lower_price": row["lower_price"] or 0.0,
                "num_grids": num_grids,
                "leverage": row["leverage"] or 0,
                "was_profitable": bool(row["was_profitable"]),
                "order_size": order_size,
                "allocated_margin": allocated_margin,
                "profit_percentage": round(profit_pct, 2),
            })
        conn.close()
        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 4),
        }, trades
    except Exception as exc:
        logger.warning(f"Could not load DB performance from {DB_PATH}: {exc}")
        return {}, []


def _coerce_state(raw: dict | None) -> dict:
    """Normalize a raw bot snapshot into the API's stable response shape."""
    if not isinstance(raw, dict):
        return json.loads(json.dumps(DEFAULT_STATE))

    normalized = json.loads(json.dumps(DEFAULT_STATE))

    for key in ("mode", "started_at", "last_update"):
        if key in raw:
            normalized[key] = raw[key]

    for key in ("wallet", "portfolio", "heartbeat", "stats"):
        value = raw.get(key)
        if isinstance(value, dict):
            normalized[key].update(value)

    slots = raw.get("slots")
    normalized["slots"] = slots if isinstance(slots, dict) else {}

    completed_trades = raw.get("completed_trades")
    normalized["completed_trades"] = completed_trades if isinstance(completed_trades, list) else []

    scanner_candidates = raw.get("scanner_candidates")
    normalized["scanner_candidates"] = scanner_candidates if isinstance(scanner_candidates, list) else []

    deploy_rejections = raw.get("deploy_rejections")
    normalized["deploy_rejections"] = deploy_rejections if isinstance(deploy_rejections, list) else []

    deploy_diagnostics = raw.get("deploy_diagnostics")
    if isinstance(deploy_diagnostics, dict):
        normalized["deploy_diagnostics"].update(deploy_diagnostics)

    events = raw.get("events")
    normalized["events"] = events if isinstance(events, list) else []

    current_prices = raw.get("current_prices")
    normalized["current_prices"] = current_prices if isinstance(current_prices, dict) else {}

    portfolio_exposure = raw.get("portfolio_exposure")
    if isinstance(portfolio_exposure, dict):
        normalized["portfolio_exposure"] = portfolio_exposure

    cluster_gate = raw.get("cluster_gate")
    if isinstance(cluster_gate, dict):
        normalized["cluster_gate"] = cluster_gate

    return normalized


# ── Bot state update API (called by the bot) ──────────────
def _load_state_file(force: bool = False) -> None:
    """Load latest bot state snapshot written by multi_grid_manager.py.
    
    Reloads automatically if the file has been modified since last load.
    """
    global _state, _last_loaded_state_mtime
    if not BOT_STATE_FILE.exists():
        return
    try:
        mtime = BOT_STATE_FILE.stat().st_mtime
        # Skip ONLY if file hasn't changed (unless forced)
        if not force and mtime <= _last_loaded_state_mtime:
            return
        raw = json.loads(BOT_STATE_FILE.read_text())
        if isinstance(raw, dict):
            slots = raw.get("slots", {})
            _state = _coerce_state(raw)
            _last_loaded_state_mtime = mtime
            logger.info(f"State loaded: {len(slots)} slots, mtime={mtime}")
    except Exception as exc:
        logger.warning(f"Could not read bot state file {BOT_STATE_FILE}: {exc}")


def _safe_float(value, default: float = 0.0) -> float:
    """Safely convert a value to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _state_with_metadata(access_session: Optional[dict] = None) -> dict:
    """Return the current state plus freshness metadata for clients.
    
    Also recalculates wallet balance from realized PnL to ensure it reflects trading results.
    """
    last_update = _state.get("last_update")
    try:
        state_age_seconds = round(time.time() - float(last_update), 2) if last_update else None
    except (TypeError, ValueError):
        state_age_seconds = None
    
    # Recalculate wallet balance from durable closed PnL plus active slot PnL.
    # Closed cycles are already realized cash for the dry-run wallet; active
    # slot realized PnL represents completed pairs inside still-open grids.

    slots = _state.get("slots", {})
    active_realized = sum(_safe_float(s.get("realized_pnl", 0)) for s in slots.values())
    active_unrealized = sum(_safe_float(s.get("unrealized_pnl", 0)) for s in slots.values())

    # Update stats from active slots and DB-backed closed trade history.
    # DB stats are for display only — balance comes from the state file directly.
    db_stats, db_trades = _load_db_performance(limit=50)

    # Use state file wallet as source of truth for balance (the bot writes it correctly).
    # DB total_pnl is cumulative across ALL runs and would inflate the balance.
    state_wallet = _state.get("wallet", {})
    current_balance = _safe_float(state_wallet.get("balance"), 100.0)
    current_equity = current_balance + active_unrealized

    # Update wallet in returned state (don't mutate _state directly for cache stability)
    mutable_state = dict(_state)
    mutable_state["wallet"] = dict(state_wallet)
    mutable_state["wallet"]["balance"] = round(current_balance, 4)
    mutable_state["wallet"]["equity"] = round(current_equity, 4)
    mutable_state["wallet"]["realized_pnl"] = round(_safe_float(state_wallet.get("realized_pnl")), 4)
    mutable_state["wallet"]["unrealized_pnl"] = round(active_unrealized, 4)

    mutable_state["portfolio"] = dict(_state.get("portfolio", {}))
    mutable_state["portfolio"].update(_runtime_portfolio_defaults())

    mutable_state["stats"] = dict(_state.get("stats", {}))
    mutable_state["stats"].update(db_stats)
    # Override PnL with current run values (not cumulative DB which spans multiple runs)
    mutable_state["stats"]["total_pnl"] = round(_safe_float(state_wallet.get("realized_pnl"), 0.0), 4)
    mutable_state["stats"]["active_pnl"] = round(active_unrealized, 4)
    if db_trades:
        mutable_state["completed_trades"] = db_trades
    
    return {
        **mutable_state,
        "connected_clients": len(manager.active_connections),
        "state_age_seconds": state_age_seconds,
        "dashboard_access": build_access_context(access_session),
    }


def update_bot_state(
    mode: str = None,
    wallet: dict = None,
    slots: dict = None,
    completed_trades: list = None,
    scanner_candidates: list = None,
    heartbeat: dict = None,
    stats: dict = None,
    current_prices: dict = None,
):
    """Called by the running bot to push state snapshots."""
    global _state
    _state = _coerce_state(_state)
    if mode is not None:
        _state["mode"] = mode
    if wallet is not None:
        _state["wallet"].update(wallet)
    if slots is not None:
        _state["slots"] = slots
    if completed_trades is not None:
        _state["completed_trades"] = completed_trades[-50:]
    if scanner_candidates is not None:
        _state["scanner_candidates"] = scanner_candidates[:10]
    if heartbeat is not None:
        _state["heartbeat"].update(heartbeat)
    if stats is not None:
        _state["stats"].update(stats)
    if current_prices is not None:
        _state["current_prices"].update(current_prices)
    _state["last_update"] = time.time()


# ── FastAPI app ───────────────────────────────────────────
app = FastAPI(title="Grid Trader API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Admin / access routers ─────────────────────────────────
# Authenticated config + secrets management plus viewer/admin dashboard sessions.
from admin import (
    router as admin_router,
    access_router,
    auth_required as access_auth_required,
    dashboard_tenant as access_dashboard_tenant,
    build_access_context,
    require_admin_access,
    require_viewer_access,
)
app.include_router(admin_router)
app.include_router(access_router)

# ── WebSocket clients ─────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WS client connected | total={len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"WS client disconnected | total={len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Broadcast state to all connected UI clients."""
        if not self.active_connections:
            return
        data = json.dumps(message, default=str)
        dead = []
        for ws in self.active_connections:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


def _viewer_session(authorization: Optional[str], tenant: Optional[str]) -> dict:
    return require_viewer_access(authorization, tenant=tenant)


def _admin_session(authorization: Optional[str], tenant: Optional[str]) -> dict:
    return require_admin_access(authorization, tenant=tenant)


def _ws_authorization(websocket: WebSocket) -> Optional[str]:
    auth_header = websocket.headers.get("authorization")
    if auth_header:
        return auth_header
    token = websocket.query_params.get("token")
    if token:
        return f"Bearer {token}"
    return None


# ── REST Endpoints ────────────────────────────────────────
@app.get("/api/state")
async def get_state(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    """Full bot state snapshot."""
    session = _viewer_session(authorization, x_tenant_id)
    _load_state_file()
    return _state_with_metadata(session)


@app.get("/api/slots")
async def get_slots(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    """Active grid slots."""
    _viewer_session(authorization, x_tenant_id)
    return list(_state["slots"].values())


@app.get("/api/slots/{slot_id}")
async def get_slot(slot_id: int, authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _viewer_session(authorization, x_tenant_id)
    slot = _state["slots"].get(str(slot_id)) or _state["slots"].get(slot_id)
    if slot is None:
        raise HTTPException(404, f"Slot {slot_id} not found")
    return slot


@app.post("/api/slots/{slot_id}/close")
async def close_slot(slot_id: int, payload: Optional[dict] = None, authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _admin_session(authorization, x_tenant_id)
    slot = _state["slots"].get(str(slot_id)) or _state["slots"].get(slot_id)
    if slot is None:
        raise HTTPException(404, f"Slot {slot_id} not found")
    if slot.get("status") != "active":
        raise HTTPException(409, f"Slot {slot_id} is not active")

    reason = str((payload or {}).get("reason") or "manual_close")
    request = _append_manual_close_request(slot_id, slot, reason=reason)
    slot["pending_close"] = True
    slot["pending_close_reason"] = reason
    slot["pending_close_requested_at"] = request["requested_at"]
    _state["last_update"] = time.time()
    await manager.broadcast({
        "type": "slot_close_requested",
        "data": {"slot_id": slot_id, "symbol": slot.get("symbol"), "reason": reason},
        "ts": request["requested_at"],
    })
    return {"ok": True, "status": "requested", **request}


@app.get("/api/wallet")
async def get_wallet(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    session = _viewer_session(authorization, x_tenant_id)
    _load_state_file()
    return _state_with_metadata(session)["wallet"]


@app.get("/api/stats")
async def get_stats(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _viewer_session(authorization, x_tenant_id)
    """Get trade stats. PnL comes from the state file (current run only), not cumulative DB."""
    try:
        _load_state_file()
        db_stats, _ = _load_db_performance(limit=0)
        stats = dict(db_stats) if db_stats else dict(_state.get("stats", {}))
        # Override total_pnl with current run's realized PnL (not cumulative DB)
        state_wallet = _state.get("wallet", {})
        stats["total_pnl"] = _safe_float(state_wallet.get("realized_pnl"), 0.0)
        stats["active_pnl"] = _safe_float(state_wallet.get("unrealized_pnl"), 0.0)
        return stats
    except Exception as e:
        logger.error(f"Failed to load stats from DB: {e}")
        return _state.get("stats", {})


@app.get("/api/scanner")
async def get_scanner(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _viewer_session(authorization, x_tenant_id)
    return _state["scanner_candidates"]


@app.get("/api/deploy/rejections")
async def get_deploy_rejections(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _viewer_session(authorization, x_tenant_id)
    _load_state_file()
    return {
        "deploy_rejections": _state.get("deploy_rejections", []),
        "deploy_diagnostics": _state.get("deploy_diagnostics", {}),
    }


@app.get("/api/heartbeat")
async def get_heartbeat(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _viewer_session(authorization, x_tenant_id)
    return _state["heartbeat"]


@app.get("/api/trades")
async def get_trades(limit: int = 50, authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _viewer_session(authorization, x_tenant_id)
    """Get completed trades from database (source of truth)."""
    try:
        _, trades = _load_db_performance(limit=limit)
        return trades or _state["completed_trades"][-limit:]
    except Exception as e:
        logger.error(f"Failed to load trades from DB: {e}")
        # Fallback to in-memory state
        return _state["completed_trades"][-limit:]


@app.get("/api/prices")
async def get_prices(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _viewer_session(authorization, x_tenant_id)
    return _state["current_prices"]


@app.get("/api/hummingbot")
async def get_hummingbot_status(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    """Hummingbot paper adapter/config/signal status for dashboard display."""
    _viewer_session(authorization, x_tenant_id)
    return _load_hummingbot_status()


@app.post("/api/pause")
async def pause_bot(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _admin_session(authorization, x_tenant_id)
    _state["mode"] = "paused"
    await manager.broadcast({"type": "bot_paused"})
    return {"status": "paused"}


@app.post("/api/resume")
async def resume_bot(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _admin_session(authorization, x_tenant_id)
    _state["mode"] = "running"
    await manager.broadcast({"type": "bot_resumed"})
    return {"status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "clients": len(manager.active_connections)}


# ── Portfolio Deployment Endpoints ───────────────────────
@app.get("/api/portfolio/status")
async def get_portfolio_status(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _viewer_session(authorization, x_tenant_id)
    """Get portfolio-level exposure and status."""
    wallet = _state.get("wallet", {})
    slots = _state.get("slots", {})
    active_count = len([s for s in slots.values() if s.get("status") == "running"])
    total_margin = sum(
        s.get("margin", 0) for s in slots.values()
    )
    total_pnl = sum(
        (s.get("realized_pnl", 0) + s.get("unrealized_pnl", 0))
        for s in slots.values()
    )
    total_round_trips = sum(
        s.get("total_round_trips", 0) for s in slots.values()
    )

    return {
        "wallet_balance": wallet.get("initial_balance", 100.0),
        "total_margin_used": total_margin,
        "wallet_exposure_pct": round(total_margin / max(wallet.get("initial_balance", 100.0), 1) * 100, 2),
        "max_tokens": 30,
        "active_grids": active_count,
        "available_slots": 30 - active_count,
        "total_pnl": round(total_pnl, 4),
        "total_round_trips": total_round_trips,
    }


@app.post("/api/portfolio/deploy")
async def deploy_portfolio(config: dict, authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _admin_session(authorization, x_tenant_id)
    """Deploy multiple grids across top scanner candidates.
    
    Args:
        config: {
            "max_tokens": int = 10,  # How many tokens to deploy
            "exposure_pct": float = 2.0,  # % of wallet per token
            "use_cross_margin": bool = True,
            "max_leverage": int = 10,
        }
    """
    import asyncio
    from pathlib import Path
    import sys

    # Import grid-trader modules
    sys.path.insert(0, str(Path(__file__).parent))
    
    max_tokens = config.get("max_tokens", 10)
    exposure_pct = config.get("exposure_pct", 2.0)
    use_cross_margin = config.get("use_cross_margin", True)
    max_leverage = clamp_leverage(config.get("max_leverage", DEFAULT_LEVERAGE))

    wallet_balance = _state.get("wallet", {}).get("initial_balance", 100.0)
    capital_per_token = wallet_balance * (exposure_pct / 100)

    # Get candidates from scanner
    candidates = _state.get("scanner_candidates", [])
    if not candidates:
        raise HTTPException(503, "Scanner not ready or no candidates available")

    # Deploy top N candidates
    deployed = []
    for i, candidate in enumerate(candidates[:max_tokens]):
        symbol = candidate.get("symbol")
        if not symbol:
            continue
        
        # Skip if already active
        if any(s.get("symbol") == symbol for s in _state["slots"].values()):
            continue

        # Prepare deployment config
        deploy_config = {
            "symbol": symbol,
            "lower_price": candidate.get("grid_range_low"),
            "upper_price": candidate.get("grid_range_high"),
            "num_levels": candidate.get("suggested_levels", 15),
            "capital_usdt": capital_per_token,
            "leverage": clamp_leverage(candidate.get("suggested_leverage", candidate.get("max_leverage", max_leverage)), maximum=max_leverage),
            "max_leverage": max_leverage,
            "is_cross_margin": use_cross_margin,
        }
        deployed.append(deploy_config)

    return {
        "deployed": len(deployed),
        "capital_per_token": capital_per_token,
        "use_cross_margin": use_cross_margin,
        "max_leverage": max_leverage,
        "tokens": [d["symbol"] for d in deployed],
    }


@app.post("/api/scanner/deploy-best")
async def deploy_best_token(authorization: Optional[str] = Header(None), x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id")):
    _admin_session(authorization, x_tenant_id)
    """Quick deploy on the #1 scanner candidate."""
    candidates = _state.get("scanner_candidates", [])
    if not candidates:
        raise HTTPException(503, "No scanner candidates available")
    
    best = candidates[0]
    wallet = _state.get("wallet", {})
    capital_per_token = wallet.get("initial_balance", 100.0) * 0.02  # 2%

    return {
        "action": "deploy",
        "symbol": best.get("symbol"),
        "grid_range": [best.get("grid_range_low"), best.get("grid_range_high")],
        "capital": round(capital_per_token, 2),
        "leverage": clamp_leverage(best.get("suggested_leverage", best.get("max_leverage", DEFAULT_LEVERAGE))),
        "message": f"Ready to deploy {best.get('symbol')} grid with ${round(capital_per_token, 2)} margin",
    }


# ── WebSocket Endpoint ────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    session = None
    try:
        session = require_viewer_access(
            _ws_authorization(websocket),
            tenant=websocket.query_params.get("tenant") or websocket.headers.get("x-tenant-id"),
        )
    except HTTPException as exc:
        await websocket.close(code=4401 if exc.status_code == 401 else 4403, reason=str(exc.detail))
        return

    await manager.connect(websocket)
    try:
        _load_state_file()
        await websocket.send_json({
            "type": "snapshot",
            "data": _state_with_metadata(session),
        })

        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                cmd = json.loads(msg)

                if cmd.get("action") == "ping":
                    await websocket.send_json({"type": "pong", "ts": time.time()})
                elif cmd.get("action") == "subscribe":
                    await websocket.send_json({"type": "subscribed", "topics": cmd.get("topics", [])})

            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping", "ts": time.time()})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.warning(f"WS error: {e}")
        manager.disconnect(websocket)


# ── Broadcast task (polls state changes and pushes) ───────
_broadcast_task: Optional[asyncio.Task] = None


async def _broadcast_loop():
    """Poll state and push diffs to all WS clients every 1 second."""
    last_state = ""
    while True:
        await asyncio.sleep(1.0)
        _load_state_file()
        if not manager.active_connections:
            continue

        current = json.dumps(_state, sort_keys=True, default=str)
        if current != last_state:
            # Full state diff — UI handles it
            await manager.broadcast({
                "type": "state_update",
                "data": _state_with_metadata({"role": "viewer", "tenant": access_dashboard_tenant(), "expires_at": None}),
                "ts": time.time(),
            })
            last_state = current


def start_broadcast_task():
    global _broadcast_task
    if _broadcast_task is None or _broadcast_task.done():
        _broadcast_task = asyncio.create_task(_broadcast_loop())


# ── Serve grid_terminal UI static files ──────────────────
if UI_DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(UI_DIST_DIR / "assets")), name="assets")


def _serve_ui_index():
    """Serve the React app shell for SPA routes."""
    index = UI_DIST_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    index = UI_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    raise HTTPException(503, "UI not found — run: npm install && npm run build in grid_terminal/")


@app.get("/")
async def serve_ui():
    return _serve_ui_index()


@app.get("/{full_path:path}")
async def serve_ui_spa_fallback(full_path: str):
    """
    React SPA fallback.

    Without this, refreshing a client-side route like /terminal/slot_151 asks
    FastAPI for that exact path and returns 404. Serve index.html instead so
    the frontend router can restore the page.
    """
    # Keep API/static misses as real 404s instead of returning HTML.
    if full_path.startswith(("api/", "ws", "assets/")):
        raise HTTPException(404, "Not found")
    return _serve_ui_index()


# ── Startup ───────────────────────────────────────────────
_test_state_injected: dict | None = None

@app.on_event("startup")
async def startup():
    global _state, _test_state_injected
    # Try to load from file, but don't overwrite if already has test data
    _load_state_file()
    # If state file has no slots but test state file exists, use test state
    test_file = Path(os.getenv("GRID_TRADER_TEST_STATE_FILE", "/tmp/grid_trader_test_state.json"))
    if test_file.exists() and len(_state.get("slots", {})) == 0:
        try:
            raw = json.loads(test_file.read_text())
            if isinstance(raw, dict) and len(raw.get("slots", {})) > 0:
                _state = _coerce_state(raw)
                _test_state_injected = raw
                logger.info(f"Test state loaded: {len(raw.get('slots', {}))} slots from {test_file}")
        except Exception as e:
            logger.warning(f"Could not load test state: {e}")
    start_broadcast_task()
    logger.info("Grid API started | WS=/ws | REST=/api/*")
    logger.info(f"UI root: {UI_DIR}")


# ── CLI ───────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "grid_api:app",
        host="0.0.0.0",
        port=8765,
        reload=False,
        log_level="info",
    )