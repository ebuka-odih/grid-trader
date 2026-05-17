"""
Adaptive scanner learning layer.

This keeps token selection market-first while allowing recent trade outcomes to
bias ranking. Bad tokens are penalized temporarily; they are not permanently
banned because market conditions change.
"""

from __future__ import annotations

import json
import math
import os
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
    total_win_pnl: float = 0.0
    total_loss_abs: float = 0.0
    pnl_squared_sum: float = 0.0
    avg_duration_seconds: float = 0.0
    cooldown_until: float = 0.0
    last_close_reason: str = ""
    # Imbalance-specific tracker: append timestamp on every grid_imbalance
    # close, prune entries older than imbalance_window_seconds. When the
    # remaining count reaches imbalance_threshold the symbol gets a longer
    # cooldown (imbalance_cooldown_seconds). Wins do NOT reset this — the
    # signal is "this symbol hits one-directional fills under our current
    # grid params during current trend regime" and that doesn't go away
    # just because a few grids hit target between the bleeders.
    imbalance_close_ts: list[float] = field(default_factory=list)


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

    FAILURE_REASONS = {"timeout", "drawdown", "spike_close", "emergency", "exposure_breach", "grid_imbalance"}
    # Imbalance soft-blacklist: count grid_imbalance closes in a rolling
    # window per symbol; trip a longer cooldown if the count crosses
    # threshold. Tunable via env without code changes.
    IMBALANCE_THRESHOLD = int(os.getenv("IMBALANCE_SOFT_BLACKLIST_THRESHOLD", "2"))
    IMBALANCE_WINDOW_SECONDS = int(os.getenv("IMBALANCE_SOFT_BLACKLIST_WINDOW_SEC", "3600"))
    IMBALANCE_COOLDOWN_SECONDS = int(os.getenv("IMBALANCE_SOFT_BLACKLIST_COOLDOWN_SEC", "7200"))
    MIN_QUALITY_TRADES = int(os.getenv("MIN_SCANNER_QUALITY_TRADES", "5"))
    MIN_HISTORICAL_WIN_RATE = float(os.getenv("MIN_HISTORICAL_WIN_RATE", "0.80"))
    # User target: risk:reward above 1/5 means average reward should be at
    # least 0.2x average loss; higher-quality symbols score better.
    MIN_HISTORICAL_REWARD_RISK = float(os.getenv("MIN_HISTORICAL_REWARD_RISK", "0.20"))
    MIN_HISTORICAL_SHARPE_UNIT = float(os.getenv("MIN_HISTORICAL_SHARPE_UNIT", "0.0"))
    MIN_HISTORICAL_EXPECTANCY = float(os.getenv("MIN_HISTORICAL_EXPECTANCY", "0.0"))

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
        state.pnl_squared_sum += total_pnl * total_pnl
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
            state.total_win_pnl += max(0.0, total_pnl)
            state.recent_failures = max(0, state.recent_failures - 1)
        else:
            state.losses += 1
            if total_pnl < 0:
                state.total_loss_abs += abs(total_pnl)
            if close_reason in self.FAILURE_REASONS or total_pnl <= 0:
                state.recent_failures += 1

        if state.recent_failures >= self.failure_threshold:
            state.cooldown_until = self._now_fn() + self.cooldown_seconds

        # Imbalance soft-blacklist: track grid_imbalance closes only.
        now = self._now_fn()
        if close_reason == "grid_imbalance":
            state.imbalance_close_ts.append(now)
        cutoff = now - self.IMBALANCE_WINDOW_SECONDS
        state.imbalance_close_ts = [t for t in state.imbalance_close_ts if t >= cutoff]
        if len(state.imbalance_close_ts) >= self.IMBALANCE_THRESHOLD:
            new_until = now + self.IMBALANCE_COOLDOWN_SECONDS
            if new_until > state.cooldown_until:
                state.cooldown_until = new_until

        self.save()
        return state

    def _quality_metrics(self, state: TokenLearningState) -> dict[str, float]:
        trades = max(1, state.trades)
        win_rate = state.wins / trades
        avg_pnl = state.total_pnl / trades
        avg_win = state.total_win_pnl / state.wins if state.wins else 0.0
        avg_loss = state.total_loss_abs / state.losses if state.losses else 0.0
        reward_risk = (avg_win / avg_loss) if avg_loss > 0 else (999.0 if state.wins else 0.0)
        variance = max(0.0, (state.pnl_squared_sum / trades) - (avg_pnl * avg_pnl))
        std = math.sqrt(variance)
        sharpe_unit = avg_pnl / std if std > 0 else (999.0 if avg_pnl > 0 else 0.0)
        return {
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "reward_risk": reward_risk,
            "sharpe_unit": sharpe_unit,
        }

    def _quality_skip_reason(self, state: TokenLearningState) -> str:
        if state.trades < self.MIN_QUALITY_TRADES:
            return ""
        metrics = self._quality_metrics(state)
        reasons = []
        if metrics["avg_pnl"] <= self.MIN_HISTORICAL_EXPECTANCY:
            reasons.append(f"expectancy {metrics['avg_pnl']:.5f} <= {self.MIN_HISTORICAL_EXPECTANCY:.5f}")
        if metrics["win_rate"] < self.MIN_HISTORICAL_WIN_RATE:
            reasons.append(f"win_rate {metrics['win_rate']:.1%} < {self.MIN_HISTORICAL_WIN_RATE:.1%}")
        if metrics["reward_risk"] < self.MIN_HISTORICAL_REWARD_RISK:
            reasons.append(f"reward:risk {metrics['reward_risk']:.2f} < {self.MIN_HISTORICAL_REWARD_RISK:.2f}")
        if metrics["sharpe_unit"] < self.MIN_HISTORICAL_SHARPE_UNIT:
            reasons.append(f"sharpe_unit {metrics['sharpe_unit']:.3f} < {self.MIN_HISTORICAL_SHARPE_UNIT:.3f}")
        return "; ".join(reasons)

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

        quality_skip = self._quality_skip_reason(state)
        if quality_skip:
            return AdjustedCandidate(
                symbol=symbol,
                market_score=market_score,
                learning_score=-market_score,
                final_score=0.0,
                cooldown_active=True,
                skip_reason=f"quality gate: {quality_skip}",
                state=state,
            )

        learning_score = 0.0
        if state.trades:
            metrics = self._quality_metrics(state)
            win_rate = metrics["win_rate"]
            avg_pnl = metrics["avg_pnl"]
            if state.trades < self.MIN_QUALITY_TRADES:
                learning_score += (win_rate - 0.5) * 0.25
                learning_score += max(-0.15, min(0.15, avg_pnl))
            else:
                learning_score += (win_rate - 0.5) * 0.35
                learning_score += max(-0.35, min(0.35, avg_pnl * 8.0))
                learning_score += max(-0.20, min(0.20, (metrics["reward_risk"] - self.MIN_HISTORICAL_REWARD_RISK) * 0.08))
                learning_score += max(-0.20, min(0.20, metrics["sharpe_unit"] * 0.08))
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
            self.states = {}
            for symbol, values in raw.get("states", {}).items():
                allowed = TokenLearningState.__dataclass_fields__.keys()
                clean = {key: value for key, value in values.items() if key in allowed}
                self.states[symbol] = TokenLearningState(**clean)
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
