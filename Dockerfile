# Grid Trader runtime image: Python bot + FastAPI API + prebuilt React static UI.
# Build the UI first with ./docker/build_frontend.sh so frontend_dist/ exists.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    GRID_TRADER_STATE_FILE=/data/grid_trader_state.json \
    GRID_TRADER_TEST_STATE_FILE=/data/grid_trader_test_state.json \
    GRID_TRADER_MANAGER_LOCK_FILE=/data/grid_trader_manager.lock \
    GRID_TRADER_DB_FILE=/data/multi_grid_trades.db \
    GRID_TRADER_UI_DIR=/app/frontend_dist \
    GRID_TRADER_UI_DIST_DIR=/app/frontend_dist \
    HUMMINGBOT_HOME=/data/hummingbot

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . /app
RUN chmod +x /app/docker/entrypoint.sh /app/run.sh || true \
    && mkdir -p /data /app/logs /app/frontend_dist

EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/api/state 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('mode')=='running', 'manager not running'; assert d.get('started_at', 0) > 0, 'no start time'" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
