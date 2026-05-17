from __future__ import annotations

from dataclasses import dataclass


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


@dataclass
class EntryShapePlan:
    lower: float
    upper: float
    num_grids: int
    template_name: str
    spacing_mode: str = "balanced"
    buy_density_bias: float = 0.5
    sell_density_bias: float = 0.5
    notes: str = ""


def compute_entry_quality(
    *,
    market_regime: str,
    range_position: float,
    vwap_distance_pct: float,
    pullback_depth_pct: float,
    atr_pct: float,
) -> float:
    """Return a normalized 0-1 score for how favorable the current entry location is."""
    rp = _clamp(range_position)
    vwap_abs = abs(float(vwap_distance_pct))
    vwap_signed = float(vwap_distance_pct)
    pullback = max(0.0, float(pullback_depth_pct))
    atr = max(0.0, float(atr_pct))

    if market_regime == "ranging":
        edge_bonus = min(abs(rp - 0.5) * 2.0, 1.0)
        vwap_bonus = min(vwap_abs / 1.0, 1.0)
        chop_penalty = 0.35 if 0.35 <= rp <= 0.65 else 0.0
        weak_pullback_penalty = 0.25 if pullback < 0.4 else 0.0
        score = 0.18 + edge_bonus * 0.45 + vwap_bonus * 0.17 - chop_penalty - weak_pullback_penalty
    elif market_regime == "trending_up":
        pocket_bonus = max(0.0, 1.0 - min(abs(rp - 0.38) / 0.26, 1.0))
        vwap_bonus = min(max(-vwap_signed, 0.0) / 1.4, 1.0)
        pullback_bonus = min(pullback / 1.25, 1.0)
        extension_penalty = 0.18 if rp >= 0.82 and vwap_signed > 0.35 else 0.0
        shallow_penalty = 0.12 if pullback < 0.25 else 0.0
        score = 0.18 + pocket_bonus * 0.26 + vwap_bonus * 0.28 + pullback_bonus * 0.28 - extension_penalty - shallow_penalty
    elif market_regime == "trending_down":
        pocket_bonus = max(0.0, 1.0 - min(abs(rp - 0.62) / 0.26, 1.0))
        vwap_bonus = min(max(vwap_signed, 0.0) / 1.4, 1.0)
        pullback_bonus = min(pullback / 1.25, 1.0)
        extension_penalty = 0.18 if rp <= 0.18 and vwap_signed < -0.35 else 0.0
        shallow_penalty = 0.12 if pullback < 0.25 else 0.0
        score = 0.18 + pocket_bonus * 0.26 + vwap_bonus * 0.28 + pullback_bonus * 0.28 - extension_penalty - shallow_penalty
    elif market_regime == "volatile":
        score = 0.22 + min(vwap_abs / 2.0, 0.18) + min(pullback / 3.0, 0.16) - min(max(atr - 2.5, 0.0) / 4.0, 0.28)
    else:
        score = 0.4 + min(vwap_abs / 3.0, 0.15) + min(pullback / 3.0, 0.15)

    if atr > 3.5:
        score -= min((atr - 3.5) / 3.0, 0.15)

    return round(_clamp(score), 4)


def plan_entry_shape(
    *,
    current_price: float,
    market_regime: str,
    atr: float,
    swing_high: float,
    swing_low: float,
    range_position: float,
    vwap_price: float,
    pullback_depth_pct: float,
) -> EntryShapePlan:
    """Build regime-aware bounds instead of a raw price±ATR box."""
    price = float(current_price)
    atr = max(float(atr), max(price * 0.0025, 1e-6))
    swing_high = max(float(swing_high), price)
    swing_low = min(float(swing_low), price)
    rp = _clamp(range_position)
    pullback = max(0.0, float(pullback_depth_pct))
    vwap = float(vwap_price)
    range_span = max(swing_high - swing_low, atr)

    if market_regime == "ranging":
        lower = min(swing_low + atr * 0.5, price - atr * 1.8)
        upper = max(swing_high - atr * 1.0, price + atr * 3.0)
        lower = min(lower, vwap - atr * 1.0)
        upper = max(upper, vwap + atr * 3.0)
        return EntryShapePlan(
            lower=round(lower, 4),
            upper=round(upper, 4),
            num_grids=14 if range_span >= atr * 4 else 12,
            template_name="range_reversion",
            spacing_mode="balanced",
            buy_density_bias=0.55 if rp <= 0.35 else 0.5,
            sell_density_bias=0.55 if rp >= 0.65 else 0.5,
            notes="anchor to range structure and fade edges, not a centered ATR box",
        )

    if market_regime == "trending_up":
        pocket_floor = min(price - atr * 1.35, vwap - atr * 0.9)
        lower = max(swing_low + atr * 0.15, pocket_floor)
        upper = max(price + atr * (1.7 + min(pullback / 1.4, 0.9)), swing_high + atr * 0.35)
        return EntryShapePlan(
            lower=round(lower, 4),
            upper=round(upper, 4),
            num_grids=12,
            template_name="trend_pullback_long",
            spacing_mode="buy_weighted",
            buy_density_bias=0.72,
            sell_density_bias=0.28,
            notes="favor buy-side density below spot so the grid builds into trend pullbacks instead of chasing breakouts",
        )

    if market_regime == "trending_down":
        lower = min(price - atr * (1.7 + min(pullback / 1.4, 0.9)), swing_low - atr * 0.35)
        pocket_ceiling = max(price + atr * 1.35, vwap + atr * 0.9)
        upper = min(swing_high - atr * 0.15, pocket_ceiling)
        return EntryShapePlan(
            lower=round(lower, 4),
            upper=round(upper, 4),
            num_grids=12,
            template_name="trend_pullback_short",
            spacing_mode="sell_weighted",
            buy_density_bias=0.28,
            sell_density_bias=0.72,
            notes="favor sell-side density above spot so the grid builds into counter-trend rebounds instead of chasing breakdowns",
        )

    lower = price - atr * 2.0
    upper = price + atr * 2.0
    if market_regime == "volatile":
        lower = min(lower, swing_low - atr * 0.5)
        upper = max(upper, swing_high + atr * 0.5)
        template_name = "volatility_expansion"
        spacing_mode = "wide"
        notes = "widen bounds to absorb violent moves"
    else:
        template_name = "hybrid_box"
        spacing_mode = "balanced"
        notes = "fallback structure-aware box"

    return EntryShapePlan(
        lower=round(lower, 4),
        upper=round(upper, 4),
        num_grids=10,
        template_name=template_name,
        spacing_mode=spacing_mode,
        notes=notes,
    )
