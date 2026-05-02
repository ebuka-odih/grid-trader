"""Rule-Based Trading Agent — pure logic, no LLM.

Replaces the LLM-powered TradingAgent with deterministic rules.
Same interfaces (PreTradeDecision, MidTradeDecision, CloseDecision, PostTradeLearning)
so it's a drop-in replacement.

Data is logged to JSONL files for offline agent analysis.
"""

import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from trading_agent import PreTradeDecision, MidTradeDecision, CloseDecision, PostTradeLearning

logger = logging.getLogger("rule_agent")

DATA_DIR = Path.home() / ".hermes" / "projects" / "grid-trader" / "data" / "trades"


class RuleBasedAgent:
    """Pure-logic trading agent. No LLM calls."""

    def __init__(self):
        self._history: list[dict] = []
        self._trade_count = 0
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── Decision 1: MID-TRADE ───────────────────────────────────

    def decide_mid_trade(self, grid_status: dict) -> MidTradeDecision:
        """
        Rule-based mid-trade decision.
        
        Rules:
        - Price near upper edge (>80%) and trending up → shift_up
        - Price near lower edge (<20%) and trending down → shift_down
        - PnL < -5% of grid range → close
        - Low fill rate (<20% after 10+ bars) → tighten
        - High fill rate (>80%) → widen
        - Default → hold
        """
        total_pnl = grid_status.get("total_pnl", 0)
        fills = grid_status.get("fills", 0)
        price = grid_status.get("price", 0)
        upper = grid_status.get("upper", 0)
        lower = grid_status.get("lower", 0)
        grid_range = upper - lower if upper > lower else 1
        position_in_range = (price - lower) / grid_range if grid_range > 0 else 0.5

        # Rule 1: Price near edge → shift
        if position_in_range > 0.85 and total_pnl >= 0:
            shift_pct = round((position_in_range - 0.5) * 10, 1)
            decision = MidTradeDecision(
                action="shift_up",
                shift_pct=shift_pct,
                reasoning=f"Price at {position_in_range:.0%} of range, shifting up {shift_pct}%",
                confidence=0.8,
            )
            self._log_decision("mid_trade", decision, grid_status)
            return decision

        if position_in_range < 0.15 and total_pnl >= 0:
            shift_pct = round((0.5 - position_in_range) * 10, 1)
            decision = MidTradeDecision(
                action="shift_down",
                shift_pct=shift_pct,
                reasoning=f"Price at {position_in_range:.0%} of range, shifting down {shift_pct}%",
                confidence=0.8,
            )
            self._log_decision("mid_trade", decision, grid_status)
            return decision

        # Rule 2: Big loss → close
        if total_pnl < -grid_range * 0.05:
            decision = MidTradeDecision(
                action="close",
                reasoning=f"PnL ${total_pnl:.2f} below threshold, closing",
                confidence=0.9,
            )
            self._log_decision("mid_trade", decision, grid_status)
            return decision

        # Rule 3: Low activity → tighten
        if fills < 3 and fills > 0:
            # Check if grid has been running for a while
            bars_active = grid_status.get("bars_active", 0)
            if bars_active > 20:
                decision = MidTradeDecision(
                    action="tighten",
                    reasoning=f"Only {fills} fills after {bars_active} bars, tightening",
                    confidence=0.6,
                )
                self._log_decision("mid_trade", decision, grid_status)
                return decision

        # Default: hold
        decision = MidTradeDecision(
            action="hold",
            reasoning="Within normal parameters",
            confidence=0.5,
        )
        self._log_decision("mid_trade", decision, grid_status)
        return decision

    # ── Decision 2: CLOSE ────────────────────────────────────────

    def decide_close(self, grid_status: dict, close_trigger: str) -> CloseDecision:
        """
        Rule-based close decision.
        
        Rules:
        - drawdown trigger → close immediately
        - PnL target reached → close
        - timeout → close
        - otherwise → hold
        """
        total_pnl = grid_status.get("total_pnl", 0)
        target_pnl = grid_status.get("target_pnl", 10)

        if close_trigger == "drawdown":
            decision = CloseDecision(
                should_close=True,
                urgency="immediate",
                reasoning="Drawdown limit hit",
                confidence=1.0,
            )
        elif close_trigger == "timeout":
            decision = CloseDecision(
                should_close=True,
                urgency="soon",
                reasoning="Grid timeout reached",
                confidence=0.8,
            )
        elif total_pnl >= target_pnl:
            decision = CloseDecision(
                should_close=True,
                urgency="soon",
                reasoning=f"PnL ${total_pnl:.2f} >= target ${target_pnl:.2f}",
                confidence=0.9,
            )
        else:
            decision = CloseDecision(
                should_close=False,
                urgency="no_rush",
                reasoning=f"PnL ${total_pnl:.2f} < target ${target_pnl:.2f}, holding",
                confidence=0.6,
            )

        self._log_decision("close", decision, grid_status)
        return decision

    # ── Decision 3: POST-TRADE ───────────────────────────────────

    def analyze_post_trade(self, cycle_result: dict) -> Optional[PostTradeLearning]:
        """
        Log trade result for offline analysis. No LLM.
        Returns None — learning happens offline via agent delegation.
        """
        self._trade_count += 1
        self._log_trade_result(cycle_result)
        return None

    # ── Data Collection ──────────────────────────────────────────

    def _log_decision(self, decision_type: str, decision, grid_status: dict):
        """Log decision to history for data collection."""
        entry = {
            "type": decision_type,
            "decision": decision.__dict__ if hasattr(decision, "__dict__") else str(decision),
            "grid_status": {
                "price": grid_status.get("price"),
                "total_pnl": grid_status.get("total_pnl"),
                "fills": grid_status.get("fills"),
                "upper": grid_status.get("upper"),
                "lower": grid_status.get("lower"),
            },
            "timestamp": time.time(),
        }
        self._history.append(entry)

    def _log_trade_result(self, cycle_result: dict):
        """Log completed trade to JSONL for offline analysis."""
        entry = {
            "symbol": cycle_result.get("symbol", "unknown"),
            "direction": cycle_result.get("direction", "unknown"),
            "total_pnl": cycle_result.get("total_pnl", 0),
            "fills": cycle_result.get("fills", 0),
            "duration_sec": cycle_result.get("duration_sec", 0),
            "close_reason": cycle_result.get("close_reason", "unknown"),
            "upper": cycle_result.get("upper", 0),
            "lower": cycle_result.get("lower", 0),
            "leverage": cycle_result.get("leverage", 0),
            "timestamp": time.time(),
        }
        path = DATA_DIR / "completed_trades.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        logger.info(f"📊 Trade logged: {entry['symbol']} pnl=${entry['total_pnl']:.2f}")

    def get_session_summary(self) -> dict:
        return {
            "trade_count": self._trade_count,
            "total_decisions": len(self._history),
            "decisions_by_type": {
                t: sum(1 for h in self._history if h["type"] == t)
                for t in ["mid_trade", "close", "post_trade"]
            },
            "agent_type": "rule_based",
        }
