#!/usr/bin/env python3
"""Capture and compare grid-trader behavior snapshots for pre/post-fix analysis."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "analysis" / "behavior_snapshots"
DEFAULT_DB_CANDIDATES = [
    Path(os.getenv("GRID_TRADER_DB_FILE", "")).expanduser() if os.getenv("GRID_TRADER_DB_FILE") else None,
    Path("/data/multi_grid_trades.db"),
    ROOT / "multi_grid_trades.db",
]
DEFAULT_ENDPOINTS = {
    "state": "http://127.0.0.1:8765/api/state",
    "wallet": "http://127.0.0.1:8765/api/wallet",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_label(label: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label.strip())
    return cleaned.strip("_") or "snapshot"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def json_get(url: str, timeout: int = 5) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"_error": str(exc), "_url": url}


def run_git(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        return None


def get_git_info() -> dict[str, Any]:
    status = run_git(["status", "--short"])
    return {
        "commit": run_git(["rev-parse", "HEAD"]),
        "branch": run_git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "dirty": bool(status),
        "status_short": status.splitlines()[:100] if status else [],
    }


def get_effective_config() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    import runtime_config  # noqa: F401  # applies runtime overlay before config import
    import config

    keys = [
        "DRY_RUN",
        "TRADING_MODE",
        "BASE_ORDER_SIZE_USDT",
        "MAX_CONCURRENT_GRIDS",
        "DEFAULT_LEVERAGE",
        "MIN_SAFE_LEVERAGE",
        "MAX_SAFE_LEVERAGE",
        "MIN_DEPLOY_LEVERAGE",
        "MAX_DEPLOY_LEVERAGE",
        "MAX_SCANNER_LEVERAGE",
        "TARGET_WALLET_EXPOSURE_PCT",
        "HARD_FLOOR_MAX_PCT",
        "MAX_TOTAL_WALLET_EXPOSURE_PCT",
    ]
    return {key: getattr(config, key, None) for key in keys}


def resolve_db_path(explicit: str | None) -> Path:
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates.extend([p for p in DEFAULT_DB_CANDIDATES if p])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No DB found. Checked: {[str(p) for p in candidates]}")


def sql_dt_param(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    # SQLite stores these timestamps as naive UTC strings like
    # '2026-05-07 09:19:50.767113'. Normalize query params to the same shape so
    # comparisons work even when callers pass ISO8601 with timezone suffixes.
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def build_where(since: datetime | None, until: datetime | None) -> tuple[str, list[str]]:
    clauses = ["closed_at IS NOT NULL"]
    params: list[str] = []
    if since:
        clauses.append("datetime(closed_at) >= datetime(?)")
        params.append(sql_dt_param(since))
    if until:
        clauses.append("datetime(closed_at) <= datetime(?)")
        params.append(sql_dt_param(until))
    return " AND ".join(clauses), params


def query_db(db_path: Path, since: datetime | None, until: datetime | None, limit: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    where_all, params_all = build_where(since, until)
    where_filled = where_all + " AND COALESCE(fills_count, 0) > 0"

    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(grid_cycles)").fetchall()}
    order_size_expr = "adjusted_order_size" if "adjusted_order_size" in existing_cols else "0.0 AS adjusted_order_size"
    adjusted_leverage_expr = "adjusted_leverage" if "adjusted_leverage" in existing_cols else "leverage AS adjusted_leverage"
    direction_expr = "direction" if "direction" in existing_cols else "'unknown' AS direction"

    total_closed = int(cur.execute(f"SELECT COUNT(*) FROM grid_cycles WHERE {where_all}", params_all).fetchone()[0] or 0)
    total_with_fills = int(cur.execute(f"SELECT COUNT(*) FROM grid_cycles WHERE {where_filled}", params_all).fetchone()[0] or 0)

    summary_row = cur.execute(
        f"""
        SELECT
            COALESCE(SUM(total_pnl), 0) AS total_pnl,
            COALESCE(AVG(total_pnl), 0) AS avg_pnl,
            COALESCE(AVG(duration_seconds), 0) AS avg_duration_seconds,
            COALESCE(AVG(fills_count), 0) AS avg_fills,
            COALESCE(SUM(CASE WHEN COALESCE(total_pnl, 0) > 0 THEN 1 ELSE 0 END), 0) AS wins,
            COALESCE(SUM(CASE WHEN COALESCE(total_pnl, 0) <= 0 THEN 1 ELSE 0 END), 0) AS non_wins,
            COALESCE(MAX(total_pnl), 0) AS best_trade,
            COALESCE(MIN(total_pnl), 0) AS worst_trade
        FROM grid_cycles
        WHERE {where_filled}
        """,
        params_all,
    ).fetchone()

    recent_rows = cur.execute(
        f"""
        SELECT grid_id, symbol, close_reason, total_pnl, realized_pnl,
               fills_count, duration_seconds, closed_at, leverage,
               {adjusted_leverage_expr}, {order_size_expr}, {direction_expr}
        FROM grid_cycles
        WHERE {where_filled}
        ORDER BY closed_at DESC
        LIMIT ?
        """,
        [*params_all, limit],
    ).fetchall()

    reason_rows = cur.execute(
        f"""
        SELECT close_reason, COUNT(*) AS trades, ROUND(COALESCE(SUM(total_pnl), 0), 4) AS total_pnl
        FROM grid_cycles
        WHERE {where_filled}
        GROUP BY close_reason
        ORDER BY trades DESC, total_pnl DESC
        """,
        params_all,
    ).fetchall()

    symbol_rows = cur.execute(
        f"""
        SELECT symbol,
               COUNT(*) AS trades,
               ROUND(COALESCE(SUM(total_pnl), 0), 4) AS total_pnl,
               ROUND(COALESCE(AVG(total_pnl), 0), 4) AS avg_pnl,
               ROUND(COALESCE(AVG(fills_count), 0), 2) AS avg_fills,
               ROUND(COALESCE(AVG(duration_seconds), 0), 2) AS avg_duration_seconds
        FROM grid_cycles
        WHERE {where_filled}
        GROUP BY symbol
        ORDER BY trades DESC, total_pnl DESC
        LIMIT 15
        """,
        params_all,
    ).fetchall()

    leverage_rows = cur.execute(
        f"""
        SELECT COALESCE({adjusted_leverage_expr}, leverage, 0) AS leverage_bucket,
               COUNT(*) AS trades,
               ROUND(COALESCE(SUM(total_pnl), 0), 4) AS total_pnl
        FROM grid_cycles
        WHERE {where_filled}
        GROUP BY leverage_bucket
        ORDER BY leverage_bucket ASC
        """,
        params_all,
    ).fetchall()

    latest_closed_at = cur.execute(
        f"SELECT MAX(closed_at) FROM grid_cycles WHERE {where_all}",
        params_all,
    ).fetchone()[0]
    earliest_closed_at = cur.execute(
        f"SELECT MIN(closed_at) FROM grid_cycles WHERE {where_all}",
        params_all,
    ).fetchone()[0]

    recent = []
    direction_counter: Counter[str] = Counter()
    for row in recent_rows:
        direction_counter[str(row["direction"] or "unknown")] += 1
        recent.append({
            "grid_id": row["grid_id"],
            "symbol": row["symbol"],
            "close_reason": row["close_reason"],
            "total_pnl": round(float(row["total_pnl"] or 0.0), 4),
            "realized_pnl": round(float(row["realized_pnl"] or 0.0), 4),
            "fills_count": int(row["fills_count"] or 0),
            "duration_seconds": round(float(row["duration_seconds"] or 0.0), 2),
            "closed_at": row["closed_at"],
            "leverage": int(row["leverage"] or 0),
            "adjusted_leverage": int(row["adjusted_leverage"] or 0),
            "adjusted_order_size": round(float(row["adjusted_order_size"] or 0.0), 4),
            "direction": row["direction"] or "unknown",
        })

    conn.close()

    wins = int(summary_row["wins"] or 0)
    return {
        "db_path": str(db_path),
        "window": {
            "since": to_iso(since),
            "until": to_iso(until),
            "earliest_closed_at": earliest_closed_at,
            "latest_closed_at": latest_closed_at,
        },
        "counts": {
            "total_closed_cycles": total_closed,
            "total_trades_with_fills": total_with_fills,
            "wins": wins,
            "non_wins": int(summary_row["non_wins"] or 0),
            "win_rate": round((wins / total_with_fills * 100), 2) if total_with_fills else 0.0,
        },
        "pnl": {
            "total": round(float(summary_row["total_pnl"] or 0.0), 4),
            "average": round(float(summary_row["avg_pnl"] or 0.0), 4),
            "best_trade": round(float(summary_row["best_trade"] or 0.0), 4),
            "worst_trade": round(float(summary_row["worst_trade"] or 0.0), 4),
        },
        "activity": {
            "avg_duration_seconds": round(float(summary_row["avg_duration_seconds"] or 0.0), 2),
            "avg_duration_minutes": round(float(summary_row["avg_duration_seconds"] or 0.0) / 60.0, 2),
            "avg_fills": round(float(summary_row["avg_fills"] or 0.0), 2),
        },
        "close_reasons": [dict(row) for row in reason_rows],
        "top_symbols": [dict(row) for row in symbol_rows],
        "leverage_breakdown": [dict(row) for row in leverage_rows],
        "recent_trades": recent,
        "recent_direction_mix": dict(direction_counter),
    }


def summarize_state(state: dict[str, Any], wallet: dict[str, Any]) -> dict[str, Any]:
    slots = state.get("slots") if isinstance(state, dict) else {}
    slots = slots if isinstance(slots, dict) else {}
    active = []
    for raw_slot in slots.values():
        if not isinstance(raw_slot, dict):
            continue
        active.append({
            "slot_id": raw_slot.get("slot_id"),
            "symbol": raw_slot.get("symbol"),
            "leverage": raw_slot.get("adjusted_leverage") or raw_slot.get("leverage"),
            "pnl": raw_slot.get("pnl"),
            "fills": raw_slot.get("fills"),
            "duration_min": raw_slot.get("duration_min"),
            "status": raw_slot.get("status"),
            "close_reason": raw_slot.get("close_reason"),
        })
    return {
        "mode": state.get("mode") if isinstance(state, dict) else None,
        "started_at": state.get("started_at") if isinstance(state, dict) else None,
        "state_age_seconds": state.get("state_age_seconds") if isinstance(state, dict) else None,
        "heartbeat": state.get("heartbeat") if isinstance(state, dict) else None,
        "wallet": wallet,
        "active_slot_count": len(active),
        "active_slots": active[:25],
    }


def capture_snapshot(args: argparse.Namespace) -> int:
    label = sanitize_label(args.label)
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    since = parse_dt(args.since)
    until = parse_dt(args.until)
    db_path = resolve_db_path(args.db)

    snapshot = {
        "label": label,
        "captured_at": to_iso(utc_now()),
        "notes": args.notes,
        "git": get_git_info(),
        "effective_config": get_effective_config(),
        "api": {
            "state": json_get(DEFAULT_ENDPOINTS["state"], timeout=args.timeout),
            "wallet": json_get(DEFAULT_ENDPOINTS["wallet"], timeout=args.timeout),
        },
        "db": query_db(db_path, since=since, until=until, limit=args.recent_limit),
    }
    snapshot["runtime_summary"] = summarize_state(snapshot["api"]["state"], snapshot["api"]["wallet"])

    ts = utc_now().strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"{ts}__{label}.json"
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

    print(f"Saved snapshot: {path}")
    print(f"Window: since={snapshot['db']['window']['since']} until={snapshot['db']['window']['until']}")
    print(
        "Trades with fills: {trades} | Win rate: {win_rate}% | Total PnL: ${pnl} | Active slots: {active}".format(
            trades=snapshot["db"]["counts"]["total_trades_with_fills"],
            win_rate=snapshot["db"]["counts"]["win_rate"],
            pnl=snapshot["db"]["pnl"]["total"],
            active=snapshot["runtime_summary"]["active_slot_count"],
        )
    )
    print("Top close reasons:")
    for row in snapshot["db"]["close_reasons"][:5]:
        print(f"  - {row['close_reason']}: {row['trades']} trades | pnl=${row['total_pnl']}")
    return 0


KEY_MAP = {
    "trades_with_fills": ("db", "counts", "total_trades_with_fills"),
    "win_rate": ("db", "counts", "win_rate"),
    "total_pnl": ("db", "pnl", "total"),
    "avg_pnl": ("db", "pnl", "average"),
    "best_trade": ("db", "pnl", "best_trade"),
    "worst_trade": ("db", "pnl", "worst_trade"),
    "avg_fills": ("db", "activity", "avg_fills"),
    "avg_duration_min": ("db", "activity", "avg_duration_minutes"),
    "active_slots": ("runtime_summary", "active_slot_count"),
    "wallet_balance": ("runtime_summary", "wallet", "balance"),
    "wallet_exposure_pct": ("runtime_summary", "wallet", "exposure_pct"),
}


def dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = data
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def compare_snapshots(args: argparse.Namespace) -> int:
    before = json.loads(Path(args.before).read_text())
    after = json.loads(Path(args.after).read_text())

    print(f"Compare: {before.get('label')}  ->  {after.get('label')}")
    print(f"Files: {args.before}  ->  {args.after}")
    for label, path in KEY_MAP.items():
        left = dig(before, path)
        right = dig(after, path)
        delta = None
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            delta = round(right - left, 4)
        print(f"- {label}: {left} -> {right}" + (f" (delta {delta:+})" if delta is not None else ""))

    def reason_map(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        rows = dig(data, ("db", "close_reasons")) or []
        return {str(row.get("close_reason")): row for row in rows if isinstance(row, dict)}

    before_reasons = reason_map(before)
    after_reasons = reason_map(after)
    all_reasons = sorted(set(before_reasons) | set(after_reasons))
    print("\nClose reason breakdown:")
    for reason in all_reasons:
        left = before_reasons.get(reason, {})
        right = after_reasons.get(reason, {})
        l_trades = left.get("trades", 0)
        r_trades = right.get("trades", 0)
        l_pnl = left.get("total_pnl", 0)
        r_pnl = right.get("total_pnl", 0)
        print(
            f"- {reason}: trades {l_trades} -> {r_trades} (delta {r_trades - l_trades:+}), "
            f"pnl ${l_pnl} -> ${r_pnl} (delta {float(r_pnl) - float(l_pnl):+0.4f})"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture and compare grid-trader behavior snapshots")
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="Capture a behavior snapshot")
    capture.add_argument("--label", required=True, help="Short label like pre_fix or post_fix")
    capture.add_argument("--notes", default="", help="Optional notes saved into the snapshot")
    capture.add_argument("--since", help="Only include DB trades closed at/after this ISO timestamp")
    capture.add_argument("--until", help="Only include DB trades closed at/before this ISO timestamp")
    capture.add_argument("--db", help="Explicit SQLite DB path")
    capture.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory to store snapshot JSON files")
    capture.add_argument("--recent-limit", type=int, default=20, help="Number of recent trades to embed")
    capture.add_argument("--timeout", type=int, default=5, help="HTTP timeout for API calls")
    capture.set_defaults(func=capture_snapshot)

    compare = sub.add_parser("compare", help="Compare two snapshot JSON files")
    compare.add_argument("before", help="Path to the earlier snapshot JSON")
    compare.add_argument("after", help="Path to the later snapshot JSON")
    compare.set_defaults(func=compare_snapshots)

    return parser


if __name__ == "__main__":
    parser = build_parser()
    ns = parser.parse_args()
    raise SystemExit(ns.func(ns))
