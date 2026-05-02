#!/usr/bin/env bash
set -euo pipefail

GRID_TERMINAL_DIR="${GRID_TERMINAL_DIR:-/home/forge1/.hermes/projects/grid_terminal}"
GRID_TRADER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$GRID_TERMINAL_DIR"
npm install
npm run lint
npm run build

rm -rf "$GRID_TRADER_DIR/frontend_dist"
mkdir -p "$GRID_TRADER_DIR/frontend_dist"
cp -a "$GRID_TERMINAL_DIR/dist/." "$GRID_TRADER_DIR/frontend_dist/"

echo "Frontend built and copied to $GRID_TRADER_DIR/frontend_dist"
