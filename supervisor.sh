#!/bin/bash
# Grid Trader Supervisor Script
# Runs the grid trader in a loop, restarting on failure.
# Uses the same singleton launch path as ./run.sh multi.

set -u

cd "/home/forge1/.hermes/projects/grid-trader"
source "./venv/bin/activate"

SUPERVISOR_LOG="supervisor.log"
echo "[$(date)] Supervisor started" >> "$SUPERVISOR_LOG"

CHILD_PID=""
SLEEP_PID=""
cleanup_child() {
    if [ -n "$SLEEP_PID" ] && kill -0 "$SLEEP_PID" 2>/dev/null; then
        kill "$SLEEP_PID" 2>/dev/null || true
        wait "$SLEEP_PID" 2>/dev/null || true
        SLEEP_PID=""
    fi
    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
        CHILD_PID=""
    fi
}
trap 'cleanup_child; exit 0' TERM INT

while true; do
    echo "[$(date)] Starting grid trader (multi-grid mode with flock)..." >> "$SUPERVISOR_LOG"
    ./run.sh multi >> "$SUPERVISOR_LOG" 2>&1 &
    CHILD_PID=$!
    wait "$CHILD_PID"
    EXIT_CODE=$?
    CHILD_PID=""
    echo "[$(date)] Grid trader exited with code $EXIT_CODE. Restarting in 10 seconds..." >> "$SUPERVISOR_LOG"
    sleep 10 &
    SLEEP_PID=$!
    wait "$SLEEP_PID"
    SLEEP_PID=""
done
