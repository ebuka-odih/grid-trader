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
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ── Paths ────────────────────────────────────────────────
GRID_TRADER_ROOT = Path(__file__).parent.resolve()
UI_DIR = Path("/home/forge1/.hermes/projects/grid_terminal")
UI_DIST_DIR = UI_DIR / "dist"

# ── Dynamic State File Selection ─────────────────────────────
# Always use the most recently modified state file for fresh data
from pathlib import Path as _Path

def _get_state_file() -> _Path:
    """Return the most recently modified state file."""
    main_file = _Path("/tmp/grid_trader_state.json")
    test_file = _Path("/tmp/grid_trader_test_state.json")
    
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

# ── Logging ──────────────────────────────────────────────
logger = logging.getLogger("grid_api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)

logger.info(f"Using state file: {BOT_STATE_FILE}")

# ── Shared state store ────────────────────────────────────
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
    "portfolio": {
        "max_exposure_pct": 95.0,
        "reserved_pct": 5.0,
    },
    "slots": {},              # slot_id -> slot data
    "completed_trades": [],   # last 50 closed trades
    "scanner_candidates": [], # top 10 scanner picks
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
DB_PATH = GRID_TRADER_ROOT / "multi_grid_trades.db"


def _load_db_performance(limit: int = 50) -> tuple[dict, list[dict]]:
    """Load closed-trade performance from SQLite for dashboard source-of-truth stats."""
    if not DB_PATH.exists():
        return {}, []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM grid_cycles WHERE closed_at IS NOT NULL")
        total_trades = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM grid_cycles WHERE closed_at IS NOT NULL AND COALESCE(total_pnl, 0) > 0")
        wins = int(cursor.fetchone()[0] or 0)
        losses = max(0, total_trades - wins)
        cursor.execute("SELECT COALESCE(SUM(total_pnl), 0) FROM grid_cycles WHERE closed_at IS NOT NULL")
        total_pnl = float(cursor.fetchone()[0] or 0.0)
        win_rate = (wins / total_trades * 100) if total_trades else 0.0
        cursor.execute("""
            SELECT grid_id, symbol, started_at, closed_at, close_reason,
                   total_pnl, realized_pnl, fills_count, duration_seconds,
                   upper_price, lower_price, num_grids, leverage, was_profitable
            FROM grid_cycles
            WHERE closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT ?
        """, (limit,))
        trades = [
            {
                "slot_id": row["grid_id"],
                "symbol": row["symbol"],
                "started_at": row["started_at"],
                "closed_at": row["closed_at"],
                "close_reason": row["close_reason"],
                "total_pnl": row["total_pnl"] or 0.0,
                "realized_pnl": row["realized_pnl"] or 0.0,
                "fills_count": row["fills_count"] or 0,
                "duration_seconds": row["duration_seconds"] or 0,
                "upper_price": row["upper_price"] or 0.0,
                "lower_price": row["lower_price"] or 0.0,
                "num_grids": row["num_grids"] or 0,
                "leverage": row["leverage"] or 0,
                "was_profitable": bool(row["was_profitable"]),
            }
            for row in cursor.fetchall()
        ]
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

    current_prices = raw.get("current_prices")
    normalized["current_prices"] = current_prices if isinstance(current_prices, dict) else {}

    portfolio_exposure = raw.get("portfolio_exposure")
    if isinstance(portfolio_exposure, dict):
        normalized["portfolio_exposure"] = portfolio_exposure

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


def _state_with_metadata() -> dict:
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
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    slots = _state.get("slots", {})
    active_realized = sum(_safe_float(s.get("realized_pnl", 0)) for s in slots.values())
    active_unrealized = sum(_safe_float(s.get("unrealized_pnl", 0)) for s in slots.values())

    # Update stats from active slots and DB-backed closed trade history before
    # calculating wallet, so DB closed PnL participates in balance.
    db_stats, db_trades = _load_db_performance(limit=50)
    closed_realized = _safe_float(db_stats.get("total_pnl"), 0.0) if db_stats else 0.0
    total_realized = closed_realized + active_realized

    # Balance = initial + realized PnL only. Equity includes open/unrealized PnL.
    initial_balance = _safe_float(_state.get("wallet", {}).get("initial_balance", 100.0), 100.0)
    corrected_balance = initial_balance + total_realized
    corrected_equity = corrected_balance + active_unrealized

    # Update wallet in returned state (don't mutate _state directly for cache stability)
    mutable_state = dict(_state)
    mutable_state["wallet"] = dict(_state.get("wallet", {}))
    mutable_state["wallet"]["balance"] = round(corrected_balance, 4)
    mutable_state["wallet"]["equity"] = round(corrected_equity, 4)
    mutable_state["wallet"]["realized_pnl"] = round(total_realized, 4)
    mutable_state["wallet"]["closed_realized_pnl"] = round(closed_realized, 4)
    mutable_state["wallet"]["active_realized_pnl"] = round(active_realized, 4)
    mutable_state["wallet"]["unrealized_pnl"] = round(active_unrealized, 4)

    mutable_state["stats"] = dict(_state.get("stats", {}))
    mutable_state["stats"].update(db_stats)
    mutable_state["stats"]["active_pnl"] = round(active_unrealized, 4)
    if "total_pnl" not in db_stats:
        mutable_state["stats"]["total_pnl"] = round(total_realized, 4)
    if db_trades:
        mutable_state["completed_trades"] = db_trades
    
    return {
        **mutable_state,
        "connected_clients": len(manager.active_connections),
        "state_age_seconds": state_age_seconds,
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


# ── REST Endpoints ────────────────────────────────────────
@app.get("/api/state")
async def get_state():
    """Full bot state snapshot."""
    _load_state_file()
    return _state_with_metadata()


@app.get("/api/slots")
async def get_slots():
    """Active grid slots."""
    return list(_state["slots"].values())


@app.get("/api/slots/{slot_id}")
async def get_slot(slot_id: int):
    slot = _state["slots"].get(str(slot_id)) or _state["slots"].get(slot_id)
    if slot is None:
        raise HTTPException(404, f"Slot {slot_id} not found")
    return slot


@app.get("/api/wallet")
async def get_wallet():
    _load_state_file()
    return _state_with_metadata()["wallet"]


@app.get("/api/stats")
async def get_stats():
    """Get trade stats from database (source of truth)."""
    try:
        stats, _ = _load_db_performance(limit=0)
        return stats or _state["stats"]
    except Exception as e:
        logger.error(f"Failed to load stats from DB: {e}")
        return _state["stats"]


@app.get("/api/scanner")
async def get_scanner():
    return _state["scanner_candidates"]


@app.get("/api/heartbeat")
async def get_heartbeat():
    return _state["heartbeat"]


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    """Get completed trades from database (source of truth)."""
    try:
        _, trades = _load_db_performance(limit=limit)
        return trades or _state["completed_trades"][-limit:]
    except Exception as e:
        logger.error(f"Failed to load trades from DB: {e}")
        # Fallback to in-memory state
        return _state["completed_trades"][-limit:]


@app.get("/api/prices")
async def get_prices():
    return _state["current_prices"]


@app.post("/api/pause")
async def pause_bot():
    _state["mode"] = "paused"
    await manager.broadcast({"type": "bot_paused"})
    return {"status": "paused"}


@app.post("/api/resume")
async def resume_bot():
    _state["mode"] = "running"
    await manager.broadcast({"type": "bot_resumed"})
    return {"status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "clients": len(manager.active_connections)}


# ── Portfolio Deployment Endpoints ───────────────────────
@app.get("/api/portfolio/status")
async def get_portfolio_status():
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
async def deploy_portfolio(config: dict):
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
    max_leverage = config.get("max_leverage", 10)

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
            "leverage": min(candidate.get("max_leverage", 10), max_leverage),
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
async def deploy_best_token():
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
        "leverage": min(best.get("max_leverage", 10), 10),
        "message": f"Ready to deploy {best.get('symbol')} grid with ${round(capital_per_token, 2)} margin",
    }


# ── WebSocket Endpoint ────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        _load_state_file()
        # Send initial state snapshot
        await websocket.send_json({
            "type": "snapshot",
            "data": _state_with_metadata(),
        })

        # Keep connection alive, handle client commands
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                cmd = json.loads(msg)

                if cmd.get("action") == "ping":
                    await websocket.send_json({"type": "pong", "ts": time.time()})
                elif cmd.get("action") == "subscribe":
                    # Client subscribes to specific updates — already getting all via broadcast
                    await websocket.send_json({"type": "subscribed", "topics": cmd.get("topics", [])})

            except asyncio.TimeoutError:
                # Keepalive ping
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
                "data": _state_with_metadata(),
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
    test_file = Path("/tmp/grid_trader_test_state.json")
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