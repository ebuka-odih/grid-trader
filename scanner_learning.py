"""
Adaptive scanner learning layer.

This keeps token selection market-first while allowing recent trade outcomes to
bias ranking. Bad tokens are penalized temporarily; they are not permanently
banned because market conditions change.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class TokenLearningState:
    symbol: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    recent_failures: int = 0
    total_pnl: float = 0.0
    avg_duration_seconds: float = 0.0
    cooldown_until: float = 0.0
    last_close_reason: str = ""


@dataclass
class AdjustedCandidate:
    symbol: str
    market_score: float
    learning_score: float
    final_score: float
    cooldown_active: bool = False
    skip_reason: str = ""
    state: TokenLearningState | None = None


class ScannerLearning:
    """Score modifier for adaptive market scanning."""

    FAILURE_REASONS = {"timeout", "drawdown", "spike_close", "agent_close", "emergency"}

    def __init__(
        self,
        now_fn: Callable[[], float] | None = None,
        cooldown_seconds: int = 90 * 60,
        failure_threshold: int = 3,
        state_path: str | None = "scanner_learning_state.json",
    ):
        self._now_fn = now_fn or time.time
        self.cooldown_seconds = cooldown_seconds
        self.failure_threshold = failure_threshold
        self.state_path = Path(state_path) if state_path else None
        self.states: dict[str, TokenLearningState] = {}
        self._load()

    def get_state(self, symbol: str) -> TokenLearningState:
        if symbol not in self.states:
            self.states[symbol] = TokenLearningState(symbol=symbol)
        return self.states[symbol]

    def record_trade(
        self,
        symbol: str,
        total_pnl: float,
        close_reason: str,
        duration_seconds: float,
    ) -> TokenLearningState:
        state = self.get_state(symbol)
        state.trades += 1
        state.total_pnl += total_pnl
        state.last_close_reason = close_reason
        if state.trades == 1:
            state.avg_duration_seconds = duration_seconds
        else:
            state.avg_duration_seconds = (
                (state.avg_duration_seconds * (state.trades - 1) + duration_seconds) / state.trades
            )

        profitable = total_pnl > 0 and close_reason == "target_hit"
        if profitable:
            state.wins += 1
            state.recent_failures = max(0, state.recent_failures - 1)
        else:
            state.losses += 1
            if close_reason in self.FAILURE_REASONS or total_pnl <= 0:
                state.recent_failures += 1

        if state.recent_failures >= self.failure_threshold:
            state.cooldown_until = self._now_fn() + self.cooldown_seconds

        self.save()
        return state

    def score_candidate(self, candidate) -> AdjustedCandidate:
        symbol = candidate.symbol
        market_score = float(getattr(candidate, "grid_score", 0.0))
        state = self.get_state(symbol)
        now = self._now_fn()

        if state.cooldown_until and now < state.cooldown_until:
            return AdjustedCandidate(
                symbol=symbol,
                market_score=market_score,
                learning_score=-market_score,
                final_score=0.0,
                cooldown_active=True,
                skip_reason=f"cooldown until {state.cooldown_until:.0f}",
                state=state,
            )

        learning_score = 0.0
        if state.trades:
            win_rate = state.wins / state.trades
            avg_pnl = state.total_pnl / state.trades
            learning_score += (win_rate - 0.5) * 0.25
            learning_score += max(-0.25, min(0.25, avg_pnl))
            learning_score -= min(0.45, state.recent_failures * 0.15)
            if state.wins and state.avg_duration_seconds and state.avg_duration_seconds <= 120:
                learning_score += 0.10

        final_score = max(0.0, min(1.5, market_score + learning_score))
        return AdjustedCandidate(
            symbol=symbol,
            market_score=market_score,
            learning_score=round(learning_score, 4),
            final_score=round(final_score, 4),
            cooldown_active=False,
            skip_reason="",
            state=state,
        )

    def _load(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text())
            self.states = {
                symbol: TokenLearningState(**values)
                for symbol, values in raw.get("states", {}).items()
            }
        except Exception:
            self.states = {}

    def save(self):
        if not self.state_path:
            return
        payload = {"states": {symbol: asdict(state) for symbol, state in self.states.items()}}
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.state_path)

    def rank_candidates(self, candidates: list) -> list:
        """Return market candidates sorted by final learning-adjusted score."""
        scored = []
        for candidate in candidates:
            adjusted = self.score_candidate(candidate)
            setattr(candidate, "market_score", adjusted.market_score)
            setattr(candidate, "learning_score", adjusted.learning_score)
            setattr(candidate, "final_score", adjusted.final_score)
            setattr(candidate, "cooldown_active", adjusted.cooldown_active)
            setattr(candidate, "skip_reason", adjusted.skip_reason)
            if not adjusted.cooldown_active and adjusted.final_score > 0:
                candidate.grid_score = adjusted.final_score
                scored.append(candidate)
        scored.sort(key=lambda c: getattr(c, "final_score", c.grid_score), reverse=True)
        return scored
