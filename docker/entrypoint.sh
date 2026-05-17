#!/usr/bin/env bash
set -Eeuo pipefail

cd /app
mkdir -p /data /data/hummingbot /app/logs

# If an existing project-root DB was copied into the image during local builds,
# move it into the mounted persistent volume on first boot only. Runtime writes
# should happen under /data via GRID_TRADER_DB_FILE.
if [[ ! -f "${GRID_TRADER_DB_FILE}" && -f /app/multi_grid_trades.db ]]; then
  cp /app/multi_grid_trades.db "${GRID_TRADER_DB_FILE}" || true
fi

# Determine desired mode from persisted runtime_config (if present).
# In live mode we can optionally force a fresh DB on boot so old dry-run
# history never leaks into the dashboard.
MODE_MARKER_FILE="/data/runtime_mode.marker"
LAST_MODE=""
if [[ -f "${MODE_MARKER_FILE}" ]]; then
  LAST_MODE="$(cat "${MODE_MARKER_FILE}" 2>/dev/null || true)"
fi

DESIRED_MODE="$(python - <<'PY'
import json
import os

truthy = {"1", "true", "yes", "on"}

def parse_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in truthy

dry_run = parse_bool(os.environ.get("DRY_RUN"), True)
try:
    with open("/data/runtime_config.json", "r", encoding="utf-8") as f:
        payload = json.load(f)
    saved = payload.get("values", {}).get("DRY_RUN")
    dry_run = parse_bool(saved, dry_run)
except Exception:
    pass

print("dry" if dry_run else "live")
PY
)"

LIVE_CLEAR_DB_ON_START="${LIVE_CLEAR_DB_ON_START:-true}"
if [[ "${DESIRED_MODE}" == "live" && "${LIVE_CLEAR_DB_ON_START,,}" == "true" ]]; then
  if [[ -f "${GRID_TRADER_DB_FILE}" ]]; then
    ts="$(date +%Y%m%d_%H%M%S)"
    backup="${GRID_TRADER_DB_FILE%.db}.live_reset_${ts}.db"
    echo "[$(date -Is)] LIVE start reset: rotating DB ${GRID_TRADER_DB_FILE} -> ${backup}"
    mv "${GRID_TRADER_DB_FILE}" "${backup}"
  fi
  # Clear cached API/session state so the dashboard starts clean.
  rm -f /data/grid_trader_state.json /data/grid_trader_session.json || true
fi

printf "%s\n" "${DESIRED_MODE}" > "${MODE_MARKER_FILE}" || true

cleanup() {
  echo "[$(date -Is)] Shutting down grid-trader container..."
  [[ -n "${MANAGER_PID:-}" ]] && kill "${MANAGER_PID}" 2>/dev/null || true
  [[ -n "${API_PID:-}" ]] && kill "${API_PID}" 2>/dev/null || true
  [[ -n "${MANAGER_PID:-}" ]] && wait "${MANAGER_PID}" 2>/dev/null || true
  [[ -n "${API_PID:-}" ]] && wait "${API_PID}" 2>/dev/null || true
}
trap cleanup TERM INT

# Start the bot manager. It has its own flock/lock protection and writes state.
echo "[$(date -Is)] Starting multi-grid manager..."
# Use direct redirection so $! is the actual python PID (not a pipeline helper).
python multi_grid_manager.py >>/app/logs/multi_grid_manager.log 2>&1 &
MANAGER_PID=$!

# Start API/static UI server. It serves /api, /ws, and frontend_dist without npm live server.
echo "[$(date -Is)] Starting FastAPI dashboard on :8765..."
python grid_api.py >>/app/logs/grid_api.log 2>&1 &
API_PID=$!

# Exit the container if either critical child exits; Docker Compose restart policy will self-heal it.
while true; do
  if ! kill -0 "$MANAGER_PID" 2>/dev/null; then
    echo "[$(date -Is)] multi_grid_manager exited; stopping container for Docker restart"
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
    exit 20
  fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "[$(date -Is)] grid_api exited; stopping container for Docker restart"
    kill "$MANAGER_PID" 2>/dev/null || true
    wait "$MANAGER_PID" 2>/dev/null || true
    exit 21
  fi
  sleep 5
 done
