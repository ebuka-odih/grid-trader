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

cleanup() {
  echo "[$(date -Is)] Shutting down grid-trader container..."
  [[ -n "${MANAGER_PID:-}" ]] && kill "${MANAGER_PID}" 2>/dev/null || true
  [[ -n "${API_PID:-}" ]] && kill "${API_PID}" 2>/dev/null || true
  wait || true
}
trap cleanup TERM INT

# Start the bot manager. It has its own flock/lock protection and writes state.
echo "[$(date -Is)] Starting multi-grid manager..."
python multi_grid_manager.py 2>&1 | tee -a /app/logs/multi_grid_manager.log &
MANAGER_PID=$!

# Start API/static UI server. It serves /api, /ws, and frontend_dist without npm live server.
echo "[$(date -Is)] Starting FastAPI dashboard on :8765..."
python grid_api.py 2>&1 | tee -a /app/logs/grid_api.log &
API_PID=$!

# Exit the container if either critical child exits; Docker Compose restart policy will self-heal it.
while true; do
  if ! kill -0 "$MANAGER_PID" 2>/dev/null; then
    echo "[$(date -Is)] multi_grid_manager exited; stopping container for Docker restart"
    kill "$API_PID" 2>/dev/null || true
    wait || true
    exit 20
  fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "[$(date -Is)] grid_api exited; stopping container for Docker restart"
    kill "$MANAGER_PID" 2>/dev/null || true
    wait || true
    exit 21
  fi
  sleep 5
 done
