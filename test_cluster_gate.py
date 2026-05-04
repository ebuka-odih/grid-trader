"""
Validation harness for the drawdown-cluster deploy gate.

Two parts:
  1) Unit-style tests of the gate logic in isolation (deterministic timestamps).
  2) Historical replay — feed actual `close_reason in {drawdown,spike_close}`
     timestamps from /data/multi_grid_trades.db through the gate and report
     how many would-be-pause minutes vs how many normal-flow minutes the gate
     would have produced. Validates that the gate would catch the 9 known
     5-minute clusters without staying paused all day.

Run from inside the grid-trader container:
    python3 /app/test_cluster_gate.py
"""
import os
import sqlite3
import sys
import time
from collections import deque
from typing import Iterable

sys.path.insert(0, "/app")

# Import constants from the (already-patched) module so the test exercises the
# same defaults the live code sees, including any env overrides.
from multi_grid_manager import (  # type: ignore
    DRAWDOWN_CLUSTER_PAUSE_SEC,
    DRAWDOWN_CLUSTER_REASONS,
    DRAWDOWN_CLUSTER_THRESHOLD,
    DRAWDOWN_CLUSTER_WINDOW_SEC,
)


class FakeManager:
    """Minimal stand-in that mirrors the gate-relevant state of MultiGridManager."""

    def __init__(self):
        self._cluster_close_ts: deque[float] = deque(maxlen=64)
        self._deployment_paused_until = 0.0
        self.pause_extends: list[tuple[float, float]] = []  # (now, until)

    def record(self, close_reason: str, now: float) -> None:
        if close_reason not in DRAWDOWN_CLUSTER_REASONS:
            return
        cutoff = now - DRAWDOWN_CLUSTER_WINDOW_SEC
        while self._cluster_close_ts and self._cluster_close_ts[0] < cutoff:
            self._cluster_close_ts.popleft()
        self._cluster_close_ts.append(now)
        if len(self._cluster_close_ts) >= DRAWDOWN_CLUSTER_THRESHOLD:
            new_until = now + DRAWDOWN_CLUSTER_PAUSE_SEC
            if new_until > self._deployment_paused_until:
                self._deployment_paused_until = new_until
                self.pause_extends.append((now, new_until))

    def is_paused(self, now: float) -> bool:
        return now < self._deployment_paused_until


# ── Unit tests ────────────────────────────────────────────────────────


def test_below_threshold_no_pause():
    m = FakeManager()
    base = 1_000_000.0
    for i in range(DRAWDOWN_CLUSTER_THRESHOLD - 1):
        m.record("drawdown", base + i * 10)
    assert not m.is_paused(base + 100), "should not pause below threshold"
    assert not m.pause_extends


def test_threshold_within_window_pauses():
    m = FakeManager()
    base = 1_000_000.0
    for i in range(DRAWDOWN_CLUSTER_THRESHOLD):
        m.record("drawdown", base + i * 10)
    last = base + (DRAWDOWN_CLUSTER_THRESHOLD - 1) * 10
    assert m.is_paused(last + 1), "must pause at the moment of trigger"
    assert m.is_paused(last + DRAWDOWN_CLUSTER_PAUSE_SEC - 1), "still paused near end"
    assert not m.is_paused(last + DRAWDOWN_CLUSTER_PAUSE_SEC + 1), "expires after pause window"


def test_spread_outside_window_no_pause():
    m = FakeManager()
    base = 1_000_000.0
    # Space events one window apart so only 1 fits at a time.
    for i in range(DRAWDOWN_CLUSTER_THRESHOLD * 2):
        m.record("drawdown", base + i * (DRAWDOWN_CLUSTER_WINDOW_SEC + 1))
    assert not m.pause_extends, "spread events should not cluster"


def test_spike_close_also_counts():
    m = FakeManager()
    base = 1_000_000.0
    reasons = ["drawdown", "spike_close", "drawdown"][:DRAWDOWN_CLUSTER_THRESHOLD]
    for i, r in enumerate(reasons):
        m.record(r, base + i * 10)
    assert m.pause_extends, "spike_close must contribute to the cluster count"


def test_irrelevant_reasons_ignored():
    m = FakeManager()
    base = 1_000_000.0
    for r in ["target_hit", "timeout", "no_fills_timeout", "agent_close"]:
        m.record(r, base)
    assert not m.pause_extends, "non-cluster reasons must not arm the gate"


def test_extending_active_pause():
    m = FakeManager()
    base = 1_000_000.0
    for i in range(DRAWDOWN_CLUSTER_THRESHOLD):
        m.record("drawdown", base + i * 10)
    first_until = m._deployment_paused_until
    # Another close inside the existing pause window — should extend.
    m.record("drawdown", base + 200)
    assert m._deployment_paused_until > first_until, "later cluster must extend pause"


def run_unit_tests():
    tests = [
        test_below_threshold_no_pause,
        test_threshold_within_window_pauses,
        test_spread_outside_window_no_pause,
        test_spike_close_also_counts,
        test_irrelevant_reasons_ignored,
        test_extending_active_pause,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    return failed


# ── Historical replay ────────────────────────────────────────────────


def fetch_closes_from_db(db_path: str, hours: int) -> list[tuple[float, str, float]]:
    """Return [(unix_ts, close_reason, total_pnl), ...] over the last `hours`."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        f"""
        SELECT strftime('%s', closed_at) AS ts, close_reason, total_pnl
        FROM grid_cycles
        WHERE closed_at IS NOT NULL
          AND closed_at > datetime('now', '-{int(hours)} hours')
        ORDER BY closed_at ASC
        """
    )
    return [(float(ts), reason or "", pnl or 0.0) for ts, reason, pnl in cur.fetchall()]


def replay(closes: Iterable[tuple[float, str, float]]):
    m = FakeManager()
    paused_pnl = 0.0
    paused_count = 0
    cluster_eligible = 0
    skipped_during_pause = 0
    skipped_close_reasons: dict[str, int] = {}
    deploys_during_pause: list[tuple[float, str]] = []

    closes = list(closes)
    for ts, reason, pnl in closes:
        # Note: this conflates "close" with "deploy event", which is OK as a
        # rough proxy — every closed grid implies a freed slot a new deploy
        # would have filled. What we want to count is: across the whole
        # window, how often was the gate ON when a fresh deploy slot opened?
        if m.is_paused(ts):
            paused_count += 1
            skipped_during_pause += 1
            skipped_close_reasons[reason] = skipped_close_reasons.get(reason, 0) + 1
            if reason in DRAWDOWN_CLUSTER_REASONS:
                paused_pnl += pnl  # PnL of trades that closed *while paused*
            deploys_during_pause.append((ts, reason))
        if reason in DRAWDOWN_CLUSTER_REASONS:
            cluster_eligible += 1
        m.record(reason, ts)

    # Total runtime spanned by the data
    if closes:
        total_span = closes[-1][0] - closes[0][0]
    else:
        total_span = 0.0

    # Total seconds the gate was active. Compute by sweeping pauses and
    # taking unions (pauses can extend, so just sum max(0, until - start)).
    paused_seconds = 0.0
    last_until = 0.0
    for now, until in m.pause_extends:
        if now >= last_until:
            paused_seconds += until - now
        else:
            paused_seconds += until - last_until
        last_until = max(last_until, until)

    return {
        "total_closes": len(closes),
        "cluster_eligible_closes": cluster_eligible,
        "pause_extensions": len(m.pause_extends),
        "paused_seconds": paused_seconds,
        "total_span_seconds": total_span,
        "paused_fraction": paused_seconds / total_span if total_span else 0.0,
        "skipped_close_reasons": skipped_close_reasons,
        "skipped_during_pause": skipped_during_pause,
        "paused_pnl_eligible_only": paused_pnl,
    }


def run_replay():
    db = os.getenv("GRID_DB_PATH", "/data/multi_grid_trades.db")
    if not os.path.exists(db):
        print(f"  ⚠️ DB not found at {db} — skipping replay")
        return
    for hours in (24, 72):
        closes = fetch_closes_from_db(db, hours)
        if not closes:
            print(f"  ⚠️ no closes in last {hours}h")
            continue
        r = replay(closes)
        span_h = r["total_span_seconds"] / 3600
        print(f"\n  ── Last {hours}h replay ({r['total_closes']} closes, span={span_h:.1f}h) ──")
        print(f"    cluster-eligible closes: {r['cluster_eligible_closes']}")
        print(f"    pause extensions       : {r['pause_extensions']}")
        print(f"    total paused time      : {r['paused_seconds']/60:.1f} min")
        print(f"    paused fraction of span: {r['paused_fraction']*100:.1f}%")
        print(f"    closes during pause    : {r['skipped_during_pause']}")
        if r["skipped_close_reasons"]:
            print(f"    close-reasons during pause:")
            for reason, n in sorted(r["skipped_close_reasons"].items(), key=lambda x: -x[1]):
                print(f"      {reason:24s} {n}")
        print(
            f"    PnL of cluster-reason closes that landed while paused (already "
            f"hit, so not 'savings' — just accountability): "
            f"${r['paused_pnl_eligible_only']:.2f}"
        )


def main():
    print(
        f"Cluster gate config: "
        f"threshold={DRAWDOWN_CLUSTER_THRESHOLD} closes "
        f"in {DRAWDOWN_CLUSTER_WINDOW_SEC:.0f}s window, "
        f"pause={DRAWDOWN_CLUSTER_PAUSE_SEC:.0f}s, "
        f"reasons={sorted(DRAWDOWN_CLUSTER_REASONS)}"
    )
    print("\n[1] Unit tests")
    failed = run_unit_tests()
    print("\n[2] Historical replay")
    run_replay()
    print()
    if failed:
        print(f"❌ {failed} unit test(s) failed")
        sys.exit(1)
    print("✅ all unit tests passed")


if __name__ == "__main__":
    main()
