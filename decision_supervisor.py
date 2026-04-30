"""
Decision Supervisor — fast deterministic gate for LLM trade decisions.

This component is the always-on correctness agent that sits between the LLM
portfolio picker and the risk monitor. It is intentionally rule-based and fast:
LLMs suggest trades, this supervisor rejects malformed/unsafe decisions before
any grid is deployed.
"""

from dataclasses import dataclass, field
import logging
from typing import Iterable

from coin_scanner import CoinScore
from trading_agent import PreTradeDecision
from config import MIN_SAFE_LEVERAGE, MAX_SAFE_LEVERAGE

logger = logging.getLogger("decision_supervisor")


@dataclass
class DecisionReviewResult:
    """Result from the DecisionSupervisor pre-trade review."""
    approved: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DecisionSupervisor:
    """
    Dedicated fast decision-correctness agent.

    It does not replace the TradingAgent's strategy selection or the
    PortfolioRiskMonitor's exposure math. It catches obviously invalid or
    low-quality decisions before risk sizing and deployment.
    """

    VALID_DIRECTIONS = {"long", "short", "neutral"}
    VALID_REGIMES = {"trending_up", "trending_down", "ranging", "volatile"}
    DEFAULT_MIN_CONFIDENCE = 0.25
    DEFAULT_MAX_GRID_WIDTH_PCT = 8.0
    DEFAULT_MIN_GRID_WIDTH_PCT = 0.25
    DEFAULT_MIN_GRIDS = 10
    DEFAULT_MAX_GRIDS = 20

    def review_pre_trade_decision(
        self,
        decision: PreTradeDecision,
        coin_score: CoinScore,
        token_profile: dict | None = None,
        active_symbols: Iterable[str] | None = None,
        max_active_per_symbol: int = 1,
    ) -> DecisionReviewResult:
        """Validate one LLM pre-trade decision before risk/deployment."""
        token_profile = token_profile or {}
        active_symbols = list(active_symbols or [])
        reasons: list[str] = []
        warnings: list[str] = []

        if decision.symbol != coin_score.symbol:
            reasons.append(
                f"Decision symbol {decision.symbol} does not match scanner symbol {coin_score.symbol}"
            )

        active_count = active_symbols.count(decision.symbol)
        if active_count >= max_active_per_symbol:
            reasons.append(
                f"{decision.symbol} is already active {active_count} time(s); capacity={max_active_per_symbol}"
            )

        if decision.direction not in self.VALID_DIRECTIONS:
            reasons.append(f"Invalid direction: {decision.direction}")

        if decision.market_regime not in self.VALID_REGIMES:
            reasons.append(f"Invalid market regime: {decision.market_regime}")

        min_confidence = float(token_profile.get("min_confidence", self.DEFAULT_MIN_CONFIDENCE))
        if decision.confidence < min_confidence:
            reasons.append(
                f"Confidence {decision.confidence:.2f} below minimum {min_confidence:.2f}"
            )

        if decision.lower >= decision.upper:
            reasons.append(
                f"Invalid grid range: lower {decision.lower} must be below upper {decision.upper}"
            )
        elif not (decision.lower <= coin_score.price <= decision.upper):
            reasons.append(
                f"Current price {coin_score.price:.6f} is outside grid range {decision.lower:.6f}-{decision.upper:.6f}"
            )
        else:
            width_pct = ((decision.upper - decision.lower) / coin_score.price) * 100
            max_width = float(token_profile.get("max_grid_width_pct", self.DEFAULT_MAX_GRID_WIDTH_PCT))
            min_width = float(token_profile.get("min_grid_width_pct", self.DEFAULT_MIN_GRID_WIDTH_PCT))
            if width_pct > max_width:
                reasons.append(f"Grid width {width_pct:.2f}% exceeds max {max_width:.2f}%")
            if width_pct < min_width:
                reasons.append(f"Grid width {width_pct:.2f}% below min {min_width:.2f}%")

        min_grids = int(token_profile.get("min_grids", self.DEFAULT_MIN_GRIDS))
        max_grids = int(token_profile.get("max_grids", self.DEFAULT_MAX_GRIDS))
        if decision.num_grids < min_grids or decision.num_grids > max_grids:
            reasons.append(
                f"Grid count {decision.num_grids} outside allowed range {min_grids}-{max_grids}"
            )

        profile_leverage = int(token_profile.get("leverage", max(decision.leverage, MIN_SAFE_LEVERAGE)))
        raw_max_leverage = min(
            MAX_SAFE_LEVERAGE,
            int(token_profile.get("max_leverage", MAX_SAFE_LEVERAGE)),
        )
        max_leverage = max(MIN_SAFE_LEVERAGE, raw_max_leverage)
        if decision.leverage < MIN_SAFE_LEVERAGE:
            reasons.append(f"Leverage {decision.leverage}x below high-frequency minimum {MIN_SAFE_LEVERAGE}x")
        elif decision.leverage > max_leverage:
            reasons.append(f"Leverage {decision.leverage}x exceeds max {max_leverage}x")

        if decision.market_regime == "trending_up" and decision.direction == "short":
            warnings.append("Short decision conflicts with trending_up regime")
        if decision.market_regime == "trending_down" and decision.direction == "long":
            warnings.append("Long decision conflicts with trending_down regime")
        if decision.market_regime == "volatile" and decision.leverage > profile_leverage:
            warnings.append("Volatile regime with leverage above profile default")

        approved = not reasons
        if approved:
            logger.info(
                "✅ Decision approved: %s | dir=%s | conf=%.2f | lev=%sx",
                decision.symbol,
                decision.direction,
                decision.confidence,
                decision.leverage,
            )
        else:
            logger.warning("🧠 Decision rejected: %s | %s", decision.symbol, reasons)

        return DecisionReviewResult(approved=approved, reasons=reasons, warnings=warnings)
