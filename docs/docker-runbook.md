# Grid Trader Docker Runbook

This deployment removes the npm live server from production. The React terminal is built once into `frontend_dist/`, then FastAPI serves the static assets and `/api` from the same container.

## Build/update

```bash
cd /home/forge1/.hermes/projects/grid-trader
./docker/build_frontend.sh
docker compose build
```

## Start/restart/stop

```bash
docker compose up -d
docker compose restart
docker compose down
```

## Verify

```bash
docker compose ps
curl -fsS http://127.0.0.1:8765/api/state | jq '{mode, slots: (.slots|length), wallet, state_age_seconds}'
docker compose logs -f --tail=100 grid-trader
```

## Persistence

The container writes runtime state to Docker volumes:

- `grid_trader_data`: `/data/grid_trader_state.json`, `/data/multi_grid_trades.db`, Hummingbot adapter files.
- `grid_trader_logs`: `/app/logs/*.log`.

The image does **not** bake `.env` or live secrets into the image. Compose injects `.env` at runtime.

## Self-healing behavior

- Docker `restart: unless-stopped` restarts the whole container after a crash or host reboot.
- The entrypoint starts both critical processes:
  - `python multi_grid_manager.py`
  - `python grid_api.py`
- If either child exits, the entrypoint exits too, forcing Docker to restart the clean pair together.
- State, DB and lock paths are under `/data` so restarts keep trade history and avoid duplicate writers.

## Future best-practice direction

For production/live capital, split this into two containers after paper parity:

1. `manager`: trading brain only, writes state/DB.
2. `api`: read-only FastAPI/UI only, reads the shared state/DB volume.

That separation makes the dashboard restart independently from the trading brain. For now, one container is safer because the current code shares state paths and needs a tight single-writer guarantee.
