"""
Decision Supervisor — fast deterministic gate for LLM trade decisions.

This component is the always-on correctness agent that sits between the LLM
portfolio picker and the risk monitor. It is intentionally rule-based and fast:
LLMs suggest trades, this supervisor rejects malformed/unsafe decisions before
any grid is deployed.
"""

from dataclasses import dataclass, field
import logging
import os
from typing import Iterable

from coin_scanner import CoinScore
from trading_agent import PreTradeDecision
from config import MIN_SAFE_LEVERAGE, clamp_leverage, resolve_profile_leverage, resolve_profile_max_leverage

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
    DEFAULT_MIN_CONFIDENCE = 0.10
    DEFAULT_MAX_GRID_WIDTH_PCT = 40.0
    DEFAULT_MIN_GRID_WIDTH_PCT = 0.25
    DEFAULT_MIN_GRIDS = 10
    DEFAULT_MAX_GRIDS = 20
    DEFAULT_MIN_ENTRY_QUALITY = float(os.getenv("MIN_ENTRY_QUALITY", "0.35"))
    STRONG_ENTRY_QUALITY = 0.70
    VALID_SPACING_MODES = {"balanced", "buy_weighted", "sell_weighted"}
    BORDERLINE_CONFIDENCE_GRACE = 0.05
    BORDERLINE_ENTRY_QUALITY_GRACE = 0.08
    BORDERLINE_GRID_WIDTH_TOLERANCE = 0.20

    @staticmethod
    def _clamp_grid_around_price(price: float, lower: float, upper: float, target_width_pct: float) -> tuple[float, float]:
        half_span = price * max(target_width_pct, 0.0) / 100.0 / 2.0
        current_mid = (float(lower) + float(upper)) / 2.0
        shift = price - current_mid
        new_lower = float(lower) + shift
        new_upper = float(upper) + shift
        current_half_span = max((new_upper - new_lower) / 2.0, 1e-9)
        if abs(current_half_span - half_span) > 1e-9:
            scale = half_span / current_half_span
            new_lower = price - (price - new_lower) * scale
            new_upper = price + (new_upper - price) * scale
        return round(new_lower, 6), round(new_upper, 6)

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
            confidence_gap = min_confidence - float(decision.confidence)
            entry_quality_hint = float(getattr(coin_score, "entry_quality_score", 0.0) or 0.0)
            if confidence_gap <= self.BORDERLINE_CONFIDENCE_GRACE and entry_quality_hint >= self.STRONG_ENTRY_QUALITY:
                warnings.append(
                    f"Confidence auto-raised from {decision.confidence:.2f} to minimum {min_confidence:.2f} because entry quality is strong"
                )
                decision.confidence = min_confidence
            else:
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
                overflow_pct = (width_pct - max_width) / max(max_width, 1e-9)
                if overflow_pct <= self.BORDERLINE_GRID_WIDTH_TOLERANCE:
                    decision.lower, decision.upper = self._clamp_grid_around_price(
                        coin_score.price,
                        decision.lower,
                        decision.upper,
                        max_width,
                    )
                    warnings.append(
                        f"Grid width auto-tightened from {width_pct:.2f}% to max {max_width:.2f}%"
                    )
                else:
                    reasons.append(f"Grid width {width_pct:.2f}% exceeds max {max_width:.2f}%")
            if width_pct < min_width:
                underflow_pct = (min_width - width_pct) / max(min_width, 1e-9)
                if underflow_pct <= self.BORDERLINE_GRID_WIDTH_TOLERANCE:
                    decision.lower, decision.upper = self._clamp_grid_around_price(
                        coin_score.price,
                        decision.lower,
                        decision.upper,
                        min_width,
                    )
                    warnings.append(
                        f"Grid width auto-widened from {width_pct:.2f}% to min {min_width:.2f}%"
                    )
                else:
                    reasons.append(f"Grid width {width_pct:.2f}% below min {min_width:.2f}%")

        min_grids = int(token_profile.get("min_grids", self.DEFAULT_MIN_GRIDS))
        max_grids = int(token_profile.get("max_grids", self.DEFAULT_MAX_GRIDS))
        if decision.num_grids < min_grids or decision.num_grids > max_grids:
            original_grids = decision.num_grids
            decision.num_grids = max(min_grids, min(max_grids, int(decision.num_grids)))
            warnings.append(
                f"Grid count auto-clamped from {original_grids} to {decision.num_grids} within {min_grids}-{max_grids}"
            )

        profile_leverage = resolve_profile_leverage(token_profile, fallback=decision.leverage)
        max_leverage = resolve_profile_max_leverage(token_profile)
        if decision.leverage < MIN_SAFE_LEVERAGE:
            reasons.append(f"Leverage {decision.leverage}x below high-frequency minimum {MIN_SAFE_LEVERAGE}x")
        else:
            clamped_leverage = clamp_leverage(decision.leverage, maximum=max_leverage)
            if clamped_leverage != decision.leverage:
                warnings.append(f"Leverage auto-clamped from {decision.leverage}x to {clamped_leverage}x")
                decision.leverage = clamped_leverage

        entry_quality = float(getattr(coin_score, "entry_quality_score", 0.0) or 0.0)
        min_entry_quality = float(token_profile.get("min_entry_quality", self.DEFAULT_MIN_ENTRY_QUALITY))
        if entry_quality < min_entry_quality:
            quality_gap = min_entry_quality - entry_quality
            if (
                quality_gap <= self.BORDERLINE_ENTRY_QUALITY_GRACE
                and decision.direction in {"long", "short"}
                and ((decision.direction == "long" and decision.market_regime == "trending_up")
                     or (decision.direction == "short" and decision.market_regime == "trending_down"))
                and float(getattr(coin_score, "pullback_depth_pct", 0.0) or 0.0) >= 0.35
            ):
                warnings.append(
                    f"Borderline entry quality {entry_quality:.2f} accepted because regime, direction, and pullback are aligned"
                )
            else:
                reasons.append(
                    f"Entry quality {entry_quality:.2f} below minimum {min_entry_quality:.2f}"
                )

        spacing_mode = str(getattr(coin_score, "entry_shape_spacing", "balanced") or "balanced")
        if spacing_mode not in self.VALID_SPACING_MODES:
            warnings.append(f"Unknown entry spacing mode: {spacing_mode}")

        template_name = str(getattr(coin_score, "entry_shape_template", "atr_box") or "atr_box")
        if entry_quality >= self.STRONG_ENTRY_QUALITY:
            warnings.append(
                f"Strong entry shape: template={template_name} spacing={spacing_mode} quality={entry_quality:.2f}"
            )
        elif template_name == "atr_box":
            warnings.append("Fallback atr_box entry shape in use")

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
