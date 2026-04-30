"""

Multi-Grid Manager v2 — runs up to 20 concurrent grid trades with cross-margin risk management.



v2 Changes:

- Shared OpenAI client (one for all TradingAgent instances)

- Token profiles from JSON (per-token leverage, sizing, direction bias)

- Portfolio Risk Monitor (exposure limits, correlation groups, emergency closures)

- Wallet Tracker (balance, per-position exposure, liquidation risk)

- Volatility-scaled order sizing (ATR-based, wallet-capped)

- Agent memory bridge (portfolio-level + cross-margin cascade patterns)



Each grid is still an independent async task with its own:

- DryRunEngine instance (isolated state)

- WebSocket price feed

- LLM agent mid-trade checks

- Close/adjust logic



The manager:

1. Scans market → gets top-20+ coins

2. Asks LLM to pick a PORTFOLIO of coins (with token profiles as context)

3. Risk monitor approves/adjusts each deployment

4. Deploys up to MAX_CONCURRENT_GRIDS grids concurrently

5. Each grid runs its own monitoring loop independently

6. Risk monitor runs every 30s for emergency checks

7. When a grid closes, slot frees up → deploy a new one

8. Wallet tracker updates on every grid state change

"""



import asyncio

import json

import os

import sqlite3
import logging
import math
import time

from dataclasses import dataclass, field

from typing import Optional



import websockets



from config import (

    BYBIT_WS_PUBLIC, SCAN_INTERVAL_SECONDS,

    TARGET_PNL_LOW, TARGET_PNL_HIGH, TARGET_PNL_PCT_LOW, TARGET_PNL_PCT_HIGH, MAX_DRAWDOWN_PCT,

    BASE_ORDER_SIZE_USDT, DEFAULT_LEVERAGE, BYBIT_API_KEY,

    MARGIN_TYPE, INITIAL_WALLET_BALANCE, TOKEN_PROFILES_PATH,

    MAX_TOTAL_WALLET_EXPOSURE_PCT, MAX_SINGLE_DIRECTION_EXPOSURE_PCT,

    PORTFOLIO_RESERVE_PCT, EMERGENCY_LIQUIDATION_BUFFER_PCT,

    RISK_CHECK_INTERVAL_SECONDS, MAX_SAFE_LEVERAGE, MIN_SAFE_LEVERAGE, MAX_TRADE_WALLET_EXPOSURE_PCT,
    MIN_ORDER_SIZE_USDT,
    MAX_CONCURRENT_GRIDS as CONFIG_MAX_CONCURRENT_GRIDS,
    SCANNER_TOP_N_PORTFOLIO,
    MIN_FREE_SLOTS_TO_SCAN as CONFIG_MIN_FREE_SLOTS_TO_SCAN,
    MAX_DEPLOYMENTS_PER_CYCLE as CONFIG_MAX_DEPLOYMENTS_PER_CYCLE,

    VOLATILITY_SCALE_ENABLED, VOLATILITY_SCALE_BASE_ATR,

    VOLATILITY_SCALE_MIN_FACTOR, VOLATILITY_SCALE_MAX_FACTOR,

)

from coin_scanner import CoinScanner, CoinScore

from dry_run_engine import DryRunEngine, DryRunState

from trading_agent import TradingAgent, PreTradeDecision, MidTradeDecision, create_shared_client

from portfolio_risk_monitor import PortfolioRiskMonitor

from decision_supervisor import DecisionSupervisor

from price_bus import PriceBus

from heartbeat_regulator import HeartbeatRegulator

from wallet_tracker import WalletTracker

from improvement_loop import ImprovementLoop

from telegram_alerter import TelegramAlerter

from grid_engine import GridEngine
from trade_close_optimizer import TradeCloseOptimizer, GridStatus



logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",

    handlers=[

        logging.StreamHandler(),

        logging.FileHandler("multi_grid_run.log"),

    ],

)

logger = logging.getLogger("multi_grid_manager")





# ── Config ──────────────────────────────────────────────────────



MAX_CONCURRENT_GRIDS = CONFIG_MAX_CONCURRENT_GRIDS

MID_TRADE_CHECK_INTERVAL = 120

GRID_MONITOR_TIMEOUT = 1800

STATUS_BROADCAST_INTERVAL = 60

HEARTBEAT_INTERVAL_SECONDS = 15

HEARTBEAT_MAX_TICK_AGE_SECONDS = 90

HEARTBEAT_DEPLOY_PAUSE_SECONDS = 30

SCANNER_TOP_N = SCANNER_TOP_N_PORTFOLIO

MIN_FREE_SLOTS_TO_SCAN = CONFIG_MIN_FREE_SLOTS_TO_SCAN
MAX_DEPLOYMENTS_PER_CYCLE = CONFIG_MAX_DEPLOYMENTS_PER_CYCLE

NEW_GRID_DEPLOY_DELAY = 5

MAX_GRIDS_PER_SYMBOL = int(os.getenv("MAX_GRIDS_PER_SYMBOL", "1"))
USE_LLM_BRAIN = os.getenv("USE_LLM_BRAIN", "false").strip().lower() in {"1", "true", "yes", "on"}
MIN_INTERNAL_GRID_LEVELS = int(os.getenv("MIN_INTERNAL_GRID_LEVELS", "10"))
MAX_INTERNAL_GRID_LEVELS = int(os.getenv("MAX_INTERNAL_GRID_LEVELS", "20"))
MIN_DEPLOY_LEVERAGE = int(os.getenv("MIN_DEPLOY_LEVERAGE", str(MIN_SAFE_LEVERAGE)))
MAX_DEPLOY_LEVERAGE = int(os.getenv("MAX_DEPLOY_LEVERAGE", str(MAX_SAFE_LEVERAGE)))
MAX_TRADE_MARGIN_PCT = float(os.getenv("MAX_TRADE_WALLET_EXPOSURE_PCT", str(MAX_TRADE_WALLET_EXPOSURE_PCT)))
TARGET_WALLET_EXPOSURE_PCT = float(os.getenv("TARGET_WALLET_EXPOSURE_PCT", "80"))
MIN_GRID_ORDER_SIZE_USDT = float(os.getenv("MIN_ORDER_SIZE_USDT", str(MIN_ORDER_SIZE_USDT)))

# Slot hygiene: free dead slots instead of letting stale/no-trade tokens sit for hours.
NO_FILL_GRID_TIMEOUT_SECONDS = int(os.getenv("NO_FILL_GRID_TIMEOUT_SECONDS", "900"))  # 15m with no fills
LOSING_STAGNANT_TIMEOUT_SECONDS = int(os.getenv("LOSING_STAGNANT_TIMEOUT_SECONDS", "1200"))  # 20m losing + no progress
STAGNANT_GRID_TIMEOUT_SECONDS = int(os.getenv("STAGNANT_GRID_TIMEOUT_SECONDS", "2400"))  # 40m no meaningful progress
MIN_PROGRESS_PRICE_MOVE_PCT = float(os.getenv("MIN_PROGRESS_PRICE_MOVE_PCT", "0.03"))
MIN_PROGRESS_PNL_MOVE_USDT = float(os.getenv("MIN_PROGRESS_PNL_MOVE_USDT", "0.01"))


def normalize_grid_density(
    num_grids: int,
    wallet_balance: float | None = None,
    max_trade_exposure_pct: float = MAX_TRADE_MARGIN_PCT,
    min_order_size_usdt: float = MIN_GRID_ORDER_SIZE_USDT,
) -> int:
    """Clamp internal grid density to scalping bounds and wallet budget.

    The bot runs one dense grid per symbol, but the whole trade must still fit
    inside the per-trade wallet cap. If a small wallet cannot afford 20 levels
    at the configured minimum order size, reduce density down toward the safe
    minimum instead of silently oversizing the trade.
    """
    try:
        requested = int(num_grids)
    except (TypeError, ValueError):
        requested = MIN_INTERNAL_GRID_LEVELS
    normalized = max(MIN_INTERNAL_GRID_LEVELS, min(MAX_INTERNAL_GRID_LEVELS, requested))

    if wallet_balance and wallet_balance > 0 and min_order_size_usdt > 0:
        max_trade_margin = wallet_balance * (max_trade_exposure_pct / 100.0)
        affordable_levels = int(max_trade_margin // min_order_size_usdt)
        if affordable_levels > 0:
            normalized = min(normalized, max(MIN_INTERNAL_GRID_LEVELS, affordable_levels))

    return normalized


def determine_deployment_pick_count(
    free_slots: int,
    available_count: int,
    max_deployments_per_cycle: int = MAX_DEPLOYMENTS_PER_CYCLE,
    current_total_exposure_pct: float | None = None,
    target_wallet_exposure_pct: float | None = None,
    per_grid_exposure_pct: float | None = None,
) -> int:
    """Decide how many fresh grids to attempt in one deployment cycle.

    When exposure inputs are provided, the pick count is driven by remaining
    wallet capacity. Example: with 1.2% exposure, 80% target, and ~2% per grid,
    the bot should try to add about 40 grids, bounded by free slots, scanner
    candidates, and the per-cycle cap.
    """
    free_slots = max(0, int(free_slots))
    available_count = max(0, int(available_count))
    base_limit = min(free_slots, available_count)
    if max_deployments_per_cycle > 0:
        base_limit = min(base_limit, int(max_deployments_per_cycle))

    if (
        current_total_exposure_pct is None
        or target_wallet_exposure_pct is None
        or per_grid_exposure_pct is None
        or per_grid_exposure_pct <= 0
    ):
        return base_limit

    remaining_exposure = max(0.0, float(target_wallet_exposure_pct) - max(0.0, float(current_total_exposure_pct)))
    if remaining_exposure <= 0:
        return 0

    exposure_needed_slots = int(math.ceil(remaining_exposure / float(per_grid_exposure_pct)))
    return min(base_limit, max(0, exposure_needed_slots))


def determine_capacity_aware_trade_budget_pct(
    max_total_wallet_exposure_pct: float,
    current_total_exposure_pct: float,
    active_slots: int,
    max_grids: int,
) -> float:
    """Spread remaining wallet exposure budget across remaining grid capacity."""
    total_cap = max(0.0, float(max_total_wallet_exposure_pct))
    current_exposure = max(0.0, float(current_total_exposure_pct))
    remaining_budget = max(0.0, total_cap - current_exposure)
    remaining_slots = max(1, int(max_grids) - max(0, int(active_slots)))
    return round(remaining_budget / remaining_slots, 4)


def build_algorithmic_fallback_decision(
    coin_score: CoinScore,
    token_profile: dict | None = None,
    wallet_balance: float | None = None,
    max_trade_exposure_pct: float = MAX_TRADE_MARGIN_PCT,
) -> PreTradeDecision:
    """Create a deterministic scanner-based fallback decision that passes supervision more often."""
    token_profile = token_profile or {}
    min_confidence = float(token_profile.get("min_confidence", DecisionSupervisor.DEFAULT_MIN_CONFIDENCE))
    min_width_pct = float(token_profile.get("min_grid_width_pct", DecisionSupervisor.DEFAULT_MIN_GRID_WIDTH_PCT))
    max_width_pct = float(token_profile.get("max_grid_width_pct", DecisionSupervisor.DEFAULT_MAX_GRID_WIDTH_PCT))
    safe_max_width_pct = max(min_width_pct, max_width_pct * 0.95)
    requested_width_pct = max(min_width_pct, min(coin_score.range_pct / 2.0, safe_max_width_pct))
    half_width = coin_score.price * (requested_width_pct / 100.0) / 2.0
    lower = max(0.0, coin_score.price - half_width)
    upper = coin_score.price + half_width

    requested_grids = token_profile.get("num_grids", coin_score.suggested_grids)
    normalized_grids = normalize_grid_density(
        requested_grids,
        wallet_balance=wallet_balance,
        max_trade_exposure_pct=max_trade_exposure_pct,
    )

    direction = str(token_profile.get("direction_bias", "neutral") or "neutral")
    if direction not in DecisionSupervisor.VALID_DIRECTIONS:
        direction = "neutral"

    leverage = max(
        MIN_DEPLOY_LEVERAGE,
        min(
            MAX_DEPLOY_LEVERAGE,
            int(token_profile.get("leverage", coin_score.suggested_leverage or DEFAULT_LEVERAGE)),
        ),
    )

    confidence = min(0.95, max(min_confidence + 0.05, float(getattr(coin_score, "grid_score", min_confidence))))

    return PreTradeDecision(
        symbol=coin_score.symbol,
        direction=direction,
        confidence=confidence,
        upper=upper,
        lower=lower,
        num_grids=normalized_grids,
        leverage=leverage,
        reasoning="Algorithmic scanner fallback with supervisor-safe confidence/range",
        market_regime="ranging",
        narrative="LLM output unavailable or invalid; falling back to scanner-ranked market-neutral deployment.",
    )


def symbol_has_bad_historical_expectancy(history: dict | None) -> bool:
    """Return True when closed-trade history says a symbol should cool down.

    We require at least 3 closed trades so a single unlucky close does not ban a
    token, then block negative-average symbols with weak win rate.
    """
    history = history or {}
    try:
        closed_trades = int(history.get("closed_trades") or history.get("trades") or 0)
        avg_pnl = float(history.get("avg_pnl") or 0.0)
        win_rate = float(history.get("win_rate") or 0.0)
    except (TypeError, ValueError):
        return False
    return closed_trades >= 3 and avg_pnl < 0 and win_rate < 35.0


def build_filter_decisions(
    available: list[CoinScore],
    num_to_pick: int,
    get_token_profile,
    wallet_balance: float | None = None,
    max_trade_exposure_pct: float = MAX_TRADE_MARGIN_PCT,
    symbol_performance: dict[str, dict] | None = None,
) -> list[PreTradeDecision]:
    """Deterministic scan → select decisions that replace the LLM brain.

    This filter keeps the fast multi-coin behavior but removes external LLM
    uncertainty. It favors liquid ranging coins with enough movement to scalp,
    rejects one-way/too-wild candidates, and emits supervisor-compatible grid
    decisions sized for small wallets.
    """
    ranked: list[tuple[float, CoinScore]] = []
    for coin_score in available:
        atr_pct = float(getattr(coin_score, "atr_pct", 0.0) or 0.0)
        range_pct = float(getattr(coin_score, "range_pct", 0.0) or 0.0)
        mean_reversion = float(getattr(coin_score, "mean_reversion_score", 0.0) or 0.0)
        grid_score = float(getattr(coin_score, "grid_score", 0.0) or 0.0)
        volume = float(getattr(coin_score, "volume_24h_usdt", 0.0) or 0.0)

        if grid_score < 0.45:
            continue
        if mean_reversion < 0.30:
            continue
        if atr_pct <= 0 or atr_pct > 5.0:
            continue
        if range_pct < 1.0 or range_pct > 18.0:
            continue

        history = (symbol_performance or {}).get(coin_score.symbol) or {}
        # Do not keep redeploying coins that have enough evidence of negative
        # expectancy. This avoids filling all slots with tokens that mostly
        # close from stale/no-fill/drawdown events.
        if symbol_has_bad_historical_expectancy(history):
            continue

        liquidity_bonus = min(0.15, volume / 100_000_000 * 0.05)
        volatility_fit = max(0.0, 1.0 - abs(atr_pct - 1.25) / 4.0)
        selection_score = (grid_score * 0.55) + (mean_reversion * 0.25) + (volatility_fit * 0.15) + liquidity_bonus
        ranked.append((selection_score, coin_score))

    ranked.sort(key=lambda item: item[0], reverse=True)

    picks: list[PreTradeDecision] = []
    for _, coin_score in ranked[: max(0, int(num_to_pick))]:
        profile = get_token_profile(coin_score.symbol) if get_token_profile else {}
        decision = build_algorithmic_fallback_decision(
            coin_score,
            token_profile=profile,
            wallet_balance=wallet_balance,
            max_trade_exposure_pct=max_trade_exposure_pct,
        )
        decision.reasoning = (
            "Deterministic scanner/filter selection: liquid ranging market, "
            f"score={coin_score.grid_score:.2f}, atr={coin_score.atr_pct:.2f}%, "
            f"mean_reversion={coin_score.mean_reversion_score:.2f}"
        )
        decision.narrative = "LLM brain disabled; scan-select-execute filter selected this grid."
        picks.append(decision)
    return picks


def symbol_grid_count(active_symbols, symbol: str) -> int:
    """Count active grids for one symbol."""
    return sum(1 for active_symbol in active_symbols if active_symbol == symbol)


def symbol_has_grid_capacity(active_symbols, symbol: str, max_per_symbol: int = MAX_GRIDS_PER_SYMBOL) -> bool:
    """Return True when another independent grid may be deployed for symbol."""
    return symbol_grid_count(active_symbols, symbol) < max_per_symbol





# ── Data Structures ─────────────────────────────────────────────



@dataclass

class GridSlot:

    """Represents one active grid trading slot."""

    slot_id: int

    symbol: str

    engine: DryRunEngine

    agent: Optional[TradingAgent]

    decision: PreTradeDecision

    state: DryRunState

    started_at: float

    task: Optional[asyncio.Task] = None

    close_reason: str = ""

    total_pnl: float = 0.0

    realized_pnl: float = 0.0

    unrealized_pnl: float = 0.0

    fills: int = 0

    duration: float = 0.0

    # v2: profile fields for risk tracking

    token_profile: dict = field(default_factory=dict)

    adjusted_leverage: int = 0

    adjusted_order_size: float = 0.0





def coin_score_to_dict(coin: CoinScore) -> dict:

    """Convert CoinScore to a dict the LLM can reason about."""

    return {

        "symbol": coin.symbol,

        "price": coin.price,

        "high_24h": coin.high_24h,

        "low_24h": coin.low_24h,

        "volume_24h_usdt": round(coin.volume_24h_usdt, 0),

        "atr_pct": coin.atr_pct,

        "range_pct": coin.range_pct,

        "mean_reversion_score": coin.mean_reversion_score,

        "grid_score": coin.grid_score,

        "suggested_upper": coin.suggested_upper,

        "suggested_lower": coin.suggested_lower,

        "suggested_grids": normalize_grid_density(coin.suggested_grids),

        "suggested_leverage": max(MIN_DEPLOY_LEVERAGE, min(MAX_DEPLOY_LEVERAGE, int(coin.suggested_leverage))),

    }





# ── Volatility-Scaled Sizing ────────────────────────────────────



def calculate_volatility_scaled_size(

    base_size: float,

    atr_pct: float,

    wallet_balance: float,

    max_wallet_exposure_pct: float,

    leverage: int,

    num_grids: int,

) -> float:

    """

    Scale order size based on ATR volatility and wallet constraints.



    Higher ATR → smaller orders (more volatile = less size).

    Wallet cap → never exceed max_wallet_exposure_pct.



    Formula:

    - vol_factor = BASE_ATR / actual_ATR (inverted: higher ATR = smaller factor)

    - Clamped to [MIN_FACTOR, MAX_FACTOR]

    - Then capped by wallet exposure limit

    """

    if not VOLATILITY_SCALE_ENABLED:

        return base_size



    # Volatility scaling: inverse ATR

    if atr_pct > 0:

        vol_factor = VOLATILITY_SCALE_BASE_ATR / atr_pct

    else:

        vol_factor = 1.0



    vol_factor = max(VOLATILITY_SCALE_MIN_FACTOR, min(VOLATILITY_SCALE_MAX_FACTOR, vol_factor))

    scaled_size = base_size * vol_factor



    # Wallet cap: order_size_usdt is margin per grid level. Leverage controls
    # notional/quantity, but the per-trade wallet cap is reserved margin, not
    # leveraged notional. Total trade margin = per-level margin * grid count.
    if wallet_balance > 0 and num_grids > 0:
        margin_levels = num_grids + 1
        max_trade_margin = max_wallet_exposure_pct / 100 * wallet_balance
        max_size = max_trade_margin / margin_levels
        scaled_size = min(scaled_size, max_size)



    # Floor at configurable dry/live minimum, but never above the wallet cap.
    if wallet_balance > 0 and num_grids > 0:
        margin_levels = num_grids + 1
        max_trade_margin = max_wallet_exposure_pct / 100 * wallet_balance
        cap_per_level = max_trade_margin / margin_levels
        floor = min(MIN_GRID_ORDER_SIZE_USDT, cap_per_level)
    else:
        floor = MIN_GRID_ORDER_SIZE_USDT
    scaled_size = max(floor, scaled_size)



    return round(scaled_size, 2)





# ── Multi-Grid Manager ──────────────────────────────────────────



class MultiGridManager:

    """

    Manages up to 20 concurrent grid trades with independent LLM agents.

    v2: Cross-margin aware with portfolio risk monitoring.

    """



    def __init__(self, max_grids: int = MAX_CONCURRENT_GRIDS):

        self.max_grids = max_grids



        # LLM brain is disabled by default. The production path is now
        # deterministic scan → select → execute → monitor → close.
        self.shared_client = create_shared_client() if USE_LLM_BRAIN else None
        self.close_optimizer = TradeCloseOptimizer(
            target_pnl_pct_low=TARGET_PNL_PCT_LOW,
            target_pnl_pct_high=TARGET_PNL_PCT_HIGH,
            max_drawdown_pct=MAX_DRAWDOWN_PCT,
            min_net_profit_usdt=float(os.getenv("MIN_NET_PROFIT_USDT", "0.02")),
            fee_buffer_multiplier=float(os.getenv("FEE_BUFFER_MULTIPLIER", "1.5")),
        )



        # Core components

        self.scanner = CoinScanner()

        self.grid_calc = GridEngine()

        self.journal = ImprovementLoop(db_path="sqlite:///multi_grid_trades.db")

        self.alerter = TelegramAlerter()
        # HeartbeatRegulator looks for a manager._push_api_state callable.
        # Keep this bound wrapper so heartbeat/deployment/fill freshness pushes
        # update the dashboard instead of leaving /api/state stale.
        self._push_api_state = lambda: _push_api_state(self)



        # v2: Risk management

        self.risk_monitor = PortfolioRiskMonitor(profiles_path=TOKEN_PROFILES_PATH)

        self.decision_supervisor = DecisionSupervisor()

        self.price_bus = PriceBus(ws_url=BYBIT_WS_PUBLIC)

        self.heartbeat = HeartbeatRegulator(

            self,

            interval_seconds=HEARTBEAT_INTERVAL_SECONDS,

            max_tick_age_seconds=HEARTBEAT_MAX_TICK_AGE_SECONDS,

            pause_seconds=HEARTBEAT_DEPLOY_PAUSE_SECONDS,

        )

        self.wallet_tracker = WalletTracker(initial_balance=INITIAL_WALLET_BALANCE)



        # Active grid slots

        self.slots: dict[int, GridSlot] = {}

        self._slot_counter = 0

        self._running = False
        self._started_at: Optional[float] = None
        self._deployment_paused_until = 0.0
        self._pause_reason: Optional[str] = None

        self._broadcaster_task: Optional[asyncio.Task] = None

        self._risk_monitor_task: Optional[asyncio.Task] = None

        self._heartbeat_task: Optional[asyncio.Task] = None



        # Performance tracking

        self._total_trades = 0

        self._total_pnl = 0.0

        self._wins = 0

        self._losses = 0

        self._completed_trades: list[dict] = []
        self._scanner_candidates: list[str] = []



        logger.info(

            f"🏗️ Multi-Grid Manager v2 initialized | max_grids={max_grids} | "

            f"margin={MARGIN_TYPE} | wallet=${INITIAL_WALLET_BALANCE:.2f} | "

            f"risk_monitor=active"

        )



    # ── Lifecycle ─────────────────────────────────────────────



    async def start(self):

        """Start the multi-grid trading system."""

        logger.info("=" * 70)

        logger.info("🤖 MULTI-GRID AGETIC TRADER v2 (Cross-Margin Dry-Run)")

        logger.info(f"  Max concurrent grids: {self.max_grids}")

        logger.info(f"  Margin mode: {MARGIN_TYPE}")

        logger.info(f"  Wallet balance: ${INITIAL_WALLET_BALANCE:.2f}")

        logger.info(f"  Max wallet exposure: {MAX_TOTAL_WALLET_EXPOSURE_PCT}%")

        logger.info(f"  Max single direction: {MAX_SINGLE_DIRECTION_EXPOSURE_PCT}%")

        logger.info(f"  Reserve: {PORTFOLIO_RESERVE_PCT}%")

        logger.info(f"  Emergency buffer: {EMERGENCY_LIQUIDATION_BUFFER_PCT}%")

        logger.info(f"  Risk check interval: {RISK_CHECK_INTERVAL_SECONDS}s")

        logger.info(f"  Heartbeat interval: {HEARTBEAT_INTERVAL_SECONDS}s")

        logger.info(f"  Volatility scaling: {'ON' if VOLATILITY_SCALE_ENABLED else 'OFF'}")

        logger.info(f"  Target PnL per grid: {TARGET_PNL_PCT_LOW}-{TARGET_PNL_PCT_HIGH}% of active filled margin")

        logger.info(f"  Max drawdown: {MAX_DRAWDOWN_PCT}%")

        logger.info(f"  Shared LLM client: ✓")

        logger.info(f"  No real orders will be placed!")

        logger.info("=" * 70)



        if not BYBIT_API_KEY or BYBIT_API_KEY == "your_api_key_here":

            logger.error("❌ API keys not set!")

            return



        self._running = True
        self._started_at = time.time()
        _push_api_state(self)

        await self.price_bus.start()



        # Start portfolio status broadcaster

        self._broadcaster_task = asyncio.create_task(self._portfolio_status_loop())



        # v2: Start risk monitor loop

        self._risk_monitor_task = asyncio.create_task(self._risk_monitor_loop())



        # v3: Heartbeat regulator keeps subsystems fresh and pauses deployment

        # if market data becomes stale.

        self._heartbeat_task = asyncio.create_task(self.heartbeat.run())



        try:

            while self._running:

                await self._deployment_cycle()

                await asyncio.sleep(10)

        except KeyboardInterrupt:

            logger.info("🛑 Stopped by user")

        except Exception as e:

            logger.error(f"❌ Fatal error: {e}", exc_info=True)

        finally:

            await self.close()



    async def close(self):

        """Shut down all grids gracefully."""

        self._running = False

        for task in [getattr(self, "_broadcaster_task", None), getattr(self, "_risk_monitor_task", None), getattr(self, "_heartbeat_task", None)]:

            if task and not task.done():

                task.cancel()

                try:

                    await task

                except asyncio.CancelledError:

                    pass

        for slot_id, slot in list(self.slots.items()):

            if slot.task and not slot.task.done():

                slot.task.cancel()

                try:

                    await slot.task

                except asyncio.CancelledError:

                    pass

        await self.price_bus.stop()

        await self.scanner.close()

        summary = self._get_portfolio_summary()

        wallet_state = self.wallet_tracker.get_wallet_state()

        wallet_stats = self.wallet_tracker.get_stats()

        logger.info(

            f"🛑 Multi-Grid Manager v2 stopped | "

            f"wallet=${wallet_state['balance']:.2f} | "

            f"pnl={wallet_stats['pnl_pct']:.1f}% | "

            f"trades={wallet_stats['total_trades']} | "

            f"win_rate={wallet_stats['win_rate']:.0f}%"

        )



    # ── v2: Risk Monitor Loop ──────────────────────────────────



    async def _risk_monitor_loop(self):

        """Run portfolio risk checks every RISK_CHECK_INTERVAL_SECONDS."""

        while self._running:

            await asyncio.sleep(RISK_CHECK_INTERVAL_SECONDS)

            if not self.slots:

                continue

            await self._run_emergency_checks()



async def _run_emergency_checks(self):
    """Check for emergency conditions and act on them."""
    wallet_state = self.wallet_tracker.get_wallet_state()
    exposure_pct = wallet_state.get("exposure_pct", 0)
    max_total = self.risk_monitor.portfolio_config.get("max_total_wallet_exposure_pct", 80)
    buffer = self.risk_monitor.portfolio_config.get("emergency_liquidation_buffer_pct", 10)
    
    actions = []
    if exposure_pct > max_total - buffer:
        actions.append({
            "action": "emergency_warning",
            "message": f"Exposure {exposure_pct:.1f}% > {max_total - buffer}%",
            "exposure": exposure_pct
        })


        for action in actions:

            if action["action"] == "close" and action.get("slot_id") in self.slots:

                slot = self.slots[action["slot_id"]]

                logger.warning(

                    f"🚨 RISK MONITOR: Closing grid #{action['slot_id']} {action['symbol']} | "

                    f"reason: {action['reason']} | urgency: {action['urgency']}"

                )

                # Cancel the grid's task — it will be caught in _monitor_grid

                if slot.task and not slot.task.done():

                    slot.task.cancel()



                # Telegram alert

                try:

                    await self.alerter.send_message(

                        f"🚨 EMERGENCY CLOSE: {action['symbol']} | {action['reason']} | urgency={action['urgency']}"

                    )

                except Exception:

                    pass



            elif action["action"] == "reduce" and action.get("slot_id") in self.slots:

                slot = self.slots[action["slot_id"]]

                logger.warning(

                    f"⚠️ RISK MONITOR: Reducing position #{action['slot_id']} {action['symbol']} | "

                    f"reason: {action['reason']}"

                )



        # Also update wallet tracker with current state

        self._update_wallet_tracker()



    def _update_wallet_tracker(self):

        """Sync wallet tracker with current grid states."""

        for slot_id, slot in self.slots.items():

            status = slot.engine.get_status()

            if status.get("active", False):

                profile = slot.token_profile or self.risk_monitor.get_token_profile(slot.symbol)

                self.wallet_tracker.update_position(

                    symbol=slot.symbol,

                    direction=slot.decision.direction,

                    order_size_usdt=slot.adjusted_order_size or profile.get("order_size_usdt", 5.0),

                    leverage=slot.adjusted_leverage or profile.get("leverage", 10),

                    unrealized_pnl=status.get("total_pnl", 0),

                    num_fills=status.get("fills", 0),

                )



    # ── Deployment Cycle ──────────────────────────────────────



    async def _deployment_cycle(self):

        """Check for free slots and deploy new grids if available."""

        free_slots = self.max_grids - len(self.slots)

        now = time.time()

        if now < getattr(self, "_deployment_paused_until", 0):

            remaining = self._deployment_paused_until - now

            logger.warning(f"💓 Deployment paused by heartbeat for {remaining:.0f}s")

            return

        if free_slots < MIN_FREE_SLOTS_TO_SCAN and len(self.slots) > 0:

            return



        logger.info(f"\n🔄 Deployment cycle | active={len(self.slots)}/{self.max_grids} | free={free_slots}")



        # Scan market

        scores = await self.scanner.scan()
        self._scanner_candidates = [s.symbol for s in scores[:SCANNER_TOP_N]] if scores else []

        if not scores:

            logger.warning("⚠️ No suitable coins found")

            return



        # Filter via risk monitor (blacklist check)

        active_symbols = [slot.symbol for slot in self.slots.values()]

        available = [

            s for s in scores

            if symbol_has_grid_capacity(active_symbols, s.symbol)

            and not self.risk_monitor.is_blacklisted(s.symbol)

        ]



        if not available:

            logger.info("📊 All candidate symbols are at per-symbol capacity or blacklisted")

            return



        # Historical expectancy is a ranking/safety signal, but it must not keep
        # the portfolio under-deployed. When wallet exposure is already near the
        # configured target, block historically bad symbols. When exposure is far
        # below target, keep them available so the bot can reach 70–80% wallet
        # usage with more independent grid opportunities.
        symbol_performance = _load_symbol_performance()
        predeploy_wallet_state = self.wallet_tracker.get_wallet_state()
        current_exposure_pct = float(predeploy_wallet_state.get("exposure_pct", 0.0) or 0.0)
        target_exposure_pct = min(
            float(self.risk_monitor.portfolio_config.get("max_total_wallet_exposure_pct", MAX_TOTAL_WALLET_EXPOSURE_PCT)),
            TARGET_WALLET_EXPOSURE_PCT,
        )
        if current_exposure_pct >= target_exposure_pct * 0.90:
            available = [
                s for s in available
                if not symbol_has_bad_historical_expectancy(symbol_performance.get(s.symbol))
            ]
            if not available:
                logger.info("📊 All remaining candidates are blocked by negative historical expectancy")
                return
        else:
            logger.info(
                "📈 Exposure %.1f%% is below target %.1f%%; historical losers are penalized, not hard-blocked",
                current_exposure_pct,
                target_exposure_pct,
            )

        # Prepare top coins for the agent to pick a PORTFOLIO

        # v2: Include token profile context in the coin data

        top_coins = []

        for s in available[:SCANNER_TOP_N]:

            coin_dict = coin_score_to_dict(s)

            profile = self.risk_monitor.get_token_profile(s.symbol)

            coin_dict["profile"] = {

                "leverage": max(MIN_DEPLOY_LEVERAGE, min(MAX_DEPLOY_LEVERAGE, int(profile.get("leverage", DEFAULT_LEVERAGE)))),

                "order_size_usdt": profile.get("order_size_usdt", 5.0),

                "direction_bias": profile.get("direction_bias", "neutral"),

                "max_wallet_exposure_pct": min(float(profile.get("max_wallet_exposure_pct", MAX_TRADE_MARGIN_PCT)), MAX_TRADE_MARGIN_PCT),

            }

            top_coins.append(coin_dict)



        # Ask LLM to pick multiple coins (portfolio selection)

        num_to_pick = determine_deployment_pick_count(
            free_slots=free_slots,
            available_count=len(available),
            max_deployments_per_cycle=MAX_DEPLOYMENTS_PER_CYCLE,
            current_total_exposure_pct=current_exposure_pct,
            target_wallet_exposure_pct=target_exposure_pct,
            per_grid_exposure_pct=MAX_TRADE_MARGIN_PCT,
        )
        logger.info(
            "📈 Exposure-driven deployment target | exposure=%.1f%% target=%.1f%% per_grid<=%.1f%% pick=%s available=%s free=%s",
            current_exposure_pct,
            target_exposure_pct,
            MAX_TRADE_MARGIN_PCT,
            num_to_pick,
            len(available),
            free_slots,
        )
        # Deterministic filter selects the deployment portfolio by default.
        # The old LLM picker can be re-enabled explicitly with USE_LLM_BRAIN=true.
        filter_picks = build_filter_decisions(
            available=available,
            num_to_pick=num_to_pick,
            get_token_profile=self.risk_monitor.get_token_profile,
            wallet_balance=self.wallet_tracker.get_balance(),
            max_trade_exposure_pct=MAX_TRADE_MARGIN_PCT,
            symbol_performance=symbol_performance,
        )
        llm_picks = self._agent_pick_portfolio(top_coins, num_to_pick) if USE_LLM_BRAIN else []

        fallback_pool = [
            build_algorithmic_fallback_decision(
                s,
                token_profile=self.risk_monitor.get_token_profile(s.symbol),
                wallet_balance=self.wallet_tracker.get_balance(),
                max_trade_exposure_pct=MAX_TRADE_MARGIN_PCT,
            )
            for s in available
        ]

        if not filter_picks and not llm_picks:
            logger.warning("🧠 Filter found no deployment candidates, using algorithmic fallback queue")

        queued_decisions: list[PreTradeDecision] = []
        seen_symbols: set[str] = set()
        for decision in [*filter_picks, *(llm_picks or []), *fallback_pool]:
            if decision.symbol in seen_symbols:
                continue
            seen_symbols.add(decision.symbol)
            queued_decisions.append(decision)

        # Deploy each pick sequentially, recalculating exposure after every success.
        wallet_balance = self.wallet_tracker.get_balance()
        deployed_this_cycle = 0

        for decision in queued_decisions:

            if len(self.slots) >= self.max_grids or deployed_this_cycle >= num_to_pick:

                break



            # Find matching CoinScore

            coin_score = next(

                (s for s in available if s.symbol == decision.symbol),

                None

            )

            if not coin_score:

                logger.warning(f"🤖 Agent picked {decision.symbol} but not in available coins, skipping")

                continue



            # Normalize LLM/scanner density against wallet budget before supervision,
            # risk sizing, and deployment.
            decision.num_grids = normalize_grid_density(decision.num_grids, wallet_balance=wallet_balance)
            coin_score.suggested_grids = normalize_grid_density(coin_score.suggested_grids, wallet_balance=wallet_balance)

            if not symbol_has_grid_capacity(active_symbols, decision.symbol):

                continue



            # v2: Get token profile

            token_profile = self.risk_monitor.get_token_profile(decision.symbol)



            # v2.1: Dedicated decision supervisor — fast correctness gate before

            # portfolio exposure math or deployment. LLM proposes; supervisor

            # verifies that the decision is sane, in-range, and not duplicated.

            review = self.decision_supervisor.review_pre_trade_decision(

                decision=decision,

                coin_score=coin_score,

                token_profile=token_profile,

                active_symbols=active_symbols,

                max_active_per_symbol=MAX_GRIDS_PER_SYMBOL,

            )

            if not review.approved:

                logger.warning(

                    f"🧠 {decision.symbol} REJECTED by decision supervisor: {review.reasons}"

                )

                continue

            for warning in review.warnings:

                logger.warning(f"🧠 {decision.symbol} supervisor warning: {warning}")



            # v2: Risk check — approve or adjust the deployment.
            # Recalculate budget before each sequential dispatch so we keep filling
            # capacity until the configured wallet exposure limit is reached.
            wallet_state = self.wallet_tracker.get_wallet_state()
            wallet_balance = wallet_state.get("balance", wallet_balance)
            dynamic_trade_budget_pct = determine_capacity_aware_trade_budget_pct(
                max_total_wallet_exposure_pct=self.risk_monitor.portfolio_config.get(
                    "max_total_wallet_exposure_pct",
                    MAX_TOTAL_WALLET_EXPOSURE_PCT,
                ),
                current_total_exposure_pct=wallet_state.get("exposure_pct", 0.0),
                active_slots=len(self.slots),
                max_grids=self.max_grids,
            )
            effective_trade_budget_pct = min(
                float(token_profile.get("max_wallet_exposure_pct", MAX_TRADE_MARGIN_PCT)),
                MAX_TRADE_MARGIN_PCT,
                dynamic_trade_budget_pct,
            )

            if effective_trade_budget_pct <= 0:
                logger.info("🛡️ Exposure budget exhausted for this cycle; stopping sequential dispatch")
                break

            decision.num_grids = normalize_grid_density(
                decision.num_grids,
                wallet_balance=wallet_balance,
                max_trade_exposure_pct=effective_trade_budget_pct,
            )

            risk_result = self.risk_monitor.check_deploy(
                symbol=decision.symbol,
                direction=decision.direction,
                leverage=decision.leverage,
                order_size_usdt=token_profile.get("order_size_usdt", BASE_ORDER_SIZE_USDT),
                wallet_balance=wallet_balance,
                active_grids=self.slots,
                num_grids=decision.num_grids,
                max_trade_pct_override=effective_trade_budget_pct,
            )

            if not risk_result["approved"]:
                logger.warning(f"🛡️ {decision.symbol} REJECTED by risk monitor: {risk_result['reasons']}")
                continue

            # Apply risk-adjusted params
            decision.leverage = risk_result["adjusted_leverage"]
            adjusted_order_size = risk_result["adjusted_order_size"]

            # v2: Volatility-scaled sizing
            adjusted_order_size = calculate_volatility_scaled_size(
                base_size=adjusted_order_size,
                atr_pct=coin_score.atr_pct,
                wallet_balance=wallet_balance,
                max_wallet_exposure_pct=effective_trade_budget_pct,
                leverage=decision.leverage,
                num_grids=decision.num_grids,
            )



            # Override grid params with agent + risk-adjusted decision

            coin_score.suggested_upper = decision.upper

            coin_score.suggested_lower = decision.lower

            coin_score.suggested_grids = decision.num_grids

            coin_score.suggested_leverage = decision.leverage



            # Deploy with adjusted params

            await self._deploy_grid(

                coin_score, decision,

                token_profile=token_profile,

                adjusted_leverage=risk_result["adjusted_leverage"],

                adjusted_order_size=adjusted_order_size,

            )

            deployed_this_cycle += 1
            active_symbols.append(decision.symbol)



            # Rate limit between deployments

            await asyncio.sleep(NEW_GRID_DEPLOY_DELAY)



    def _agent_pick_portfolio(self, top_coins: list[dict], num_picks: int) -> list[PreTradeDecision]:

        """

        Ask the LLM to pick MULTIPLE coins for concurrent grid trading.

        v2: Uses shared client, includes token profile context.

        """

        # Create a temporary agent with shared client for portfolio selection

        portfolio_agent = TradingAgent(client=self.shared_client)



        system_prompt = f"""You are an expert crypto grid trading agent managing a PORTFOLIO of concurrent grid trades in CROSS-MARGIN mode. You can run up to {self.max_grids} grids simultaneously.



CROSS-MARGIN RULES:

- All positions share one wallet — correlated positions increase risk

- Diversify across sectors to avoid cascade liquidation

- Respect direction_bias in each coin's profile (if it says "long", lean long)

- Respect leverage and order_size from profiles only after applying safety caps
- HARD RISK LIMIT: use high-frequency leverage from {MIN_DEPLOY_LEVERAGE}x to {MAX_DEPLOY_LEVERAGE}x; keep wallet risk controlled by margin size, not by low leverage
- HARD RISK LIMIT: each new grid trade may reserve at most {MAX_TRADE_MARGIN_PCT:.1f}% of wallet margin total across all grid levels



Your job: Pick {num_picks} DIFFERENT coins for grid trading right now. Diversify across:

- Different sectors (L1, DeFi, Meme, AI, etc.)

- Different volatility profiles (mix of conservative and aggressive)

- Different directions (some long, some short, some neutral)

- Avoid picking multiple coins from the same correlation group



For each coin, decide: direction, grid range, number of internal grid levels, and leverage.
Use exchange-style dense grids: exactly ONE active grid engine per symbol, with {MIN_INTERNAL_GRID_LEVELS}-{MAX_INTERNAL_GRID_LEVELS} internal limit levels inside its upper/lower range.

Use the profile suggestions as a guide but you can deviate if market conditions warrant it.



You must respond with valid JSON only. Format:

{{

  "picks": [

    {{

      "symbol": "COIN/USDT:USDT",

      "direction": "long|short|neutral",

      "confidence": 0.0-1.0,

      "upper": float,

      "lower": float,

      "num_grids": int,

      "leverage": int,

      "reasoning": "brief explanation",

      "market_regime": "trending_up|trending_down|ranging|volatile",

      "narrative": "1-sentence market context"

    }}

  ],

  "portfolio_strategy": "brief explanation of overall portfolio approach"

}}



Rules:

- Pick exactly {num_picks} coins (or fewer if not enough good options)

- Each coin MUST be from the provided list

- Do NOT pick the same coin twice
- Do NOT pick a coin that is already active; existing active symbols already have their one dense grid engine
- leverage MUST be an integer from {MIN_DEPLOY_LEVERAGE} to {MAX_DEPLOY_LEVERAGE}; prefer high leverage for fast scalping while keeping total grid margin under the 2% wallet cap
- num_grids MUST be an integer from {MIN_INTERNAL_GRID_LEVELS} to {MAX_INTERNAL_GRID_LEVELS}; prefer 10 for small wallet/min order safety, 15–20 only when range and 2% margin budget support it

- Diversify: don't pick 5 coins from the same sector

- Consider correlations: avoid picking 5 coins that all move together

- Higher confidence = stronger conviction

- Target PnL per grid: {TARGET_PNL_PCT_LOW}-{TARGET_PNL_PCT_HIGH}% of active filled margin (not static dollars)

- Max drawdown: {MAX_DRAWDOWN_PCT}%

- Wallet balance: ${self.wallet_tracker.get_balance():.2f}"""



        wallet_state = self.wallet_tracker.get_wallet_state()

        user_prompt = f"""Currently active grids: {len(self.slots)}/{self.max_grids}

Free slots available: {self.max_grids - len(self.slots)}

Wallet: balance=${wallet_state['balance']:.2f} | exposure={wallet_state['exposure_pct']:.1f}% | free={wallet_state['free_pct']:.1f}%



Top coins from scanner (ranked by algorithmic score, with token profiles):

{json.dumps(top_coins, indent=2)}



Pick {num_picks} coins for concurrent grid trading. Respond with JSON only."""



        raw = portfolio_agent._call_llm(system_prompt, user_prompt, max_tokens=800)

        if not raw:

            return []



        parsed = portfolio_agent._parse_json(raw)

        if not parsed or "picks" not in parsed:

            logger.warning(f"🤖 Portfolio pick failed to parse: {raw[:200]}")

            return []



        picks = []

        for p in parsed["picks"]:

            try:

                decision = PreTradeDecision(

                    symbol=p["symbol"],

                    direction=p.get("direction", "neutral"),

                    confidence=float(p.get("confidence", 0.5)),

                    upper=float(p["upper"]),

                    lower=float(p["lower"]),

                    num_grids=normalize_grid_density(int(p["num_grids"])),

                    leverage=max(MIN_DEPLOY_LEVERAGE, min(MAX_DEPLOY_LEVERAGE, int(p["leverage"]))),

                    reasoning=p.get("reasoning", ""),

                    market_regime=p.get("market_regime", "ranging"),

                    narrative=p.get("narrative", ""),

                )

                picks.append(decision)

                logger.info(

                    f"🤖 PORTFOLIO PICK: {decision.symbol} | dir={decision.direction} | "

                    f"regime={decision.market_regime} | conf={decision.confidence:.2f} | "

                    f"grid={decision.lower:.4f}-{decision.upper:.4f} | "

                    f"grids={decision.num_grids} | lev={decision.leverage}x"

                )

            except (KeyError, ValueError) as e:

                logger.error(f"🤖 Portfolio pick parse error: {e} | raw: {p}")

                continue



        if parsed.get("portfolio_strategy"):

            logger.info(f"🤖 Portfolio strategy: {parsed['portfolio_strategy']}")



        return picks



    # ── Grid Deployment ───────────────────────────────────────



    async def _deploy_grid(

        self,

        coin_score: CoinScore,

        decision: PreTradeDecision,

        token_profile: dict = None,

        adjusted_leverage: int = 0,

        adjusted_order_size: float = 0.0,

    ):

        """Deploy a single grid as an async task with v2 risk-aware params."""

        self._slot_counter += 1

        slot_id = self._slot_counter



        token_profile = token_profile or self.risk_monitor.get_token_profile(coin_score.symbol)



        # Per-grid LLM agent removed from default runtime. Monitoring/close is
        # handled by deterministic fee-aware rules.
        agent = TradingAgent(client=self.shared_client) if USE_LLM_BRAIN else None



        # Deploy biased grid using risk-adjusted order size so actual grid quantities

        # match the portfolio risk monitor and wallet tracker.

        final_order_size = adjusted_order_size or token_profile.get("order_size_usdt", BASE_ORDER_SIZE_USDT)

        engine = DryRunEngine()

        state = self._deploy_biased_grid(

            engine,

            coin_score,

            decision.direction,

            order_size_usdt=final_order_size,

        )



        # Record cycle start with v2 runtime metadata so open grids are auditable

        # even before they close.

        effective_leverage = adjusted_leverage or decision.leverage

        self.journal.record_cycle_start(

            grid_id=state.grid.grid_id, symbol=state.grid.symbol,

            upper=state.grid.upper_price, lower=state.grid.lower_price,

            num_grids=state.grid.num_grids, leverage=state.grid.leverage,

            direction=decision.direction,

            adjusted_leverage=effective_leverage,

            adjusted_order_size=final_order_size,

        )



        slot = GridSlot(

            slot_id=slot_id,

            symbol=coin_score.symbol,

            engine=engine,

            agent=agent,

            decision=decision,

            state=state,

            started_at=time.time(),

            token_profile=token_profile,

            adjusted_leverage=adjusted_leverage or decision.leverage,

            adjusted_order_size=final_order_size,

        )



        # Launch monitoring as independent async task

        slot.task = asyncio.create_task(

            self._monitor_grid(slot),

            name=f"grid_{slot_id}_{coin_score.symbol.replace('/', '_')}",

        )

        self.slots[slot_id] = slot



        # v2: Register position in wallet tracker

        self.wallet_tracker.update_position(

            symbol=coin_score.symbol,

            direction=decision.direction,

            order_size_usdt=slot.adjusted_order_size,

            leverage=slot.adjusted_leverage,

            num_fills=0,

        )



        logger.info(

            f"🚀 Grid #{slot_id} deployed: {coin_score.symbol} | "

            f"dir={decision.direction} | lev={slot.adjusted_leverage}x | "

            f"size=${slot.adjusted_order_size:.2f} | "

            f"margin={MARGIN_TYPE} | "

            f"active={len(self.slots)}/{self.max_grids}"

        )
        _push_api_state(self)



        # Telegram alert

        try:

            await self.alerter.alert_grid_opened(

                symbol=coin_score.symbol,

                upper=state.grid.upper_price,

                lower=state.grid.lower_price,

                grids=state.grid.num_grids,

                leverage=state.grid.leverage,

                score=coin_score.grid_score,

            )

        except Exception:

            pass



    def _deploy_biased_grid(

        self,

        engine: DryRunEngine,

        coin_score: CoinScore,

        direction: str,

        order_size_usdt: float = BASE_ORDER_SIZE_USDT,

    ) -> DryRunState:

        """Deploy a direction-biased grid to an isolated engine."""

        grid = self.grid_calc.calculate_grid_levels(

            symbol=coin_score.symbol,

            upper=coin_score.suggested_upper,

            lower=coin_score.suggested_lower,

            num_grids=normalize_grid_density(coin_score.suggested_grids),

            current_price=coin_score.price,

            leverage=coin_score.suggested_leverage,

            order_size_usdt=order_size_usdt,

        )



        # Apply direction bias

        if direction in ("long", "short"):

            price = coin_score.price

            levels_below = [l for l in grid.grid_levels if l.price < price]

            levels_above = [l for l in grid.grid_levels if l.price >= price]



            if direction == "long":

                for lvl in levels_below:

                    lvl.side = "Buy"

                sell_count = max(1, len(levels_above) // 3)

                for i, lvl in enumerate(levels_above):

                    lvl.side = "Sell" if i >= len(levels_above) - sell_count else "Buy"

            elif direction == "short":

                for lvl in levels_above:

                    lvl.side = "Sell"

                buy_count = max(1, len(levels_below) // 3)

                for i, lvl in enumerate(levels_below):

                    lvl.side = "Buy" if i < buy_count else "Sell"



        state = DryRunState(

            grid=grid,

            started_at=time.time(),

            current_price=coin_score.price,

        )

        for level in grid.grid_levels:

            level.status = "placed"



        engine.state = state

        return state



    # ── Grid Monitoring (per-grid independent loop) ───────────



    def _stagnation_close_reason(

        self,

        *,

        age_seconds: float,

        fills: int,

        total_pnl: float,

        seconds_since_progress: float,

    ) -> str | None:

        """Return close reason when a grid is occupying a slot without useful progress."""

        if fills <= 0 and age_seconds >= NO_FILL_GRID_TIMEOUT_SECONDS:

            return "no_fills_timeout"

        # Funded-challenge rule: never crystallize a losing filled position just
        # because it is old or stagnant. Keep it open until it recovers to
        # positive PnL, then close/recycle gracefully.
        if fills > 0 and total_pnl < 0:

            return None

        if seconds_since_progress >= STAGNANT_GRID_TIMEOUT_SECONDS:

            return "stagnant_no_progress"

        return None



    # ── Grid Monitoring (per-grid independent loop) ───────────



    async def _monitor_grid(self, slot: GridSlot):

        """Monitor one grid using the shared PriceBus plus periodic LLM checks."""

        close_reason = "timeout"

        last_agent_check = time.time()

        start = time.time()

        last_status = 0

        last_progress_time = start

        last_progress_price = float(getattr(slot.state, "current_price", 0.0) or 0.0)

        last_progress_fills = 0

        last_progress_pnl = 0.0

        price_queue = None



        logger.info(f"📡 [#{slot.slot_id}] Starting shared-bus monitoring for {slot.symbol}")



        try:

            price_queue = await self.price_bus.subscribe(slot.symbol)



            while self._running and slot.state.is_active:

                try:

                    price = await asyncio.wait_for(price_queue.get(), timeout=5)

                    event = slot.engine.on_price_update(price)

                    if event in {"target_hit", "drawdown", "spike_close"}:

                        close_reason = event

                        break

                except asyncio.TimeoutError:

                    # No tick within the timeout; keep periodic checks alive.

                    pass



                now = time.time()

                status = slot.engine.get_status()

                current_price = float(status.get("current_price") or 0.0)

                current_fills = int(status.get("fills") or 0)

                current_pnl = float(status.get("total_pnl") or 0.0)

                # Fee-aware PnL collection: if closed + open PnL is enough
                # after estimated fees/spread buffer, close the grid and free
                # capital for the next scan-selected opportunity.
                if current_fills > 0 and current_price > 0:
                    spread_price = current_price * 0.0002
                    close_decision = self.close_optimizer.should_close(
                        GridStatus(
                            fills=current_fills,
                            realized_pnl=float(status.get("realized_pnl") or 0.0),
                            unrealized_pnl=float(status.get("unrealized_pnl") or 0.0),
                            allocated_margin=float(status.get("allocated_margin_usdt") or 0.0),
                            order_size_usdt=float(slot.adjusted_order_size or 0.0),
                            avg_entry_price=float(status.get("entry_price") or current_price),
                            current_bid=current_price - spread_price / 2,
                            current_ask=current_price + spread_price / 2,
                            current_mark=current_price,
                            direction=str(status.get("position_side") or slot.decision.direction or "neutral").lower(),
                            grid_levels=int(status.get("num_grids") or slot.decision.num_grids or 0),
                            filled_levels=int(status.get("filled_levels") or current_fills),
                            age_seconds=now - start,
                        )
                    )
                    if close_decision.should_close:
                        logger.info(
                            f"💰 [#{slot.slot_id}] Fee-aware profit lock CLOSE | "
                            f"net=${close_decision.net_pnl:.4f} | gross=${close_decision.gross_pnl:.4f} | "
                            f"fees=${close_decision.fee_estimate:.4f} | {close_decision.reason}"
                        )
                        close_reason = "profit_lock"
                        break
                price_move_pct = 0.0

                if last_progress_price:

                    price_move_pct = abs(current_price - last_progress_price) / abs(last_progress_price) * 100

                pnl_move = abs(current_pnl - last_progress_pnl)

                if (

                    current_fills != last_progress_fills

                    or price_move_pct >= MIN_PROGRESS_PRICE_MOVE_PCT

                    or pnl_move >= MIN_PROGRESS_PNL_MOVE_USDT

                ):

                    last_progress_time = now

                    last_progress_price = current_price

                    last_progress_fills = current_fills

                    last_progress_pnl = current_pnl

                stale_reason = self._stagnation_close_reason(

                    age_seconds=now - start,

                    fills=current_fills,

                    total_pnl=current_pnl,

                    seconds_since_progress=now - last_progress_time,

                )

                if stale_reason:

                    logger.warning(

                        f"🧹 [#{slot.slot_id}] Closing stagnant grid | reason={stale_reason} | "

                        f"symbol={slot.symbol} | age={(now - start) / 60:.1f}m | "

                        f"no_progress={(now - last_progress_time) / 60:.1f}m | "

                        f"fills={current_fills} | pnl=${current_pnl:.4f} | price=${current_price:.6f}"

                    )

                    close_reason = stale_reason

                    break



                # Periodic per-grid status

                if now - last_status >= 60:

                    status = slot.engine.get_status()

                    logger.info(

                        f"📊 [#{slot.slot_id}] {slot.symbol} | "

                        f"price=${status['current_price']:.4f} | "

                        f"pnl=${status['total_pnl']:.4f} | "

                        f"fills={status['fills']} | "

                        f"pos={status.get('position_side', '')} {status.get('position_qty', 0):.6f}"

                    )

                    last_status = now



                    # v2: Update wallet tracker with latest PnL

                    self.wallet_tracker.update_position(

                        symbol=slot.symbol,

                        direction=slot.decision.direction,

                        order_size_usdt=slot.adjusted_order_size,

                        leverage=slot.adjusted_leverage,

                        unrealized_pnl=status.get("total_pnl", 0),

                        num_fills=status.get("fills", 0),

                    )



                # Optional legacy LLM mid-trade check. Disabled by default so the
                # strategy remains deterministic and auditable.
                if USE_LLM_BRAIN and slot.agent and now - last_agent_check >= MID_TRADE_CHECK_INTERVAL:

                    try:

                        # v2: Include portfolio context in mid-trade check

                        grid_status = slot.engine.get_status()

                        grid_status["portfolio_wallet"] = self.wallet_tracker.get_wallet_state()

                        grid_status["token_profile"] = slot.token_profile

                        mid_decision = slot.agent.decide_mid_trade(grid_status)

                        if mid_decision.action == "close":

                            logger.info(f"🤖 [#{slot.slot_id}] Agent says CLOSE! {mid_decision.reasoning}")

                            close_reason = "agent_close"

                            break

                        elif mid_decision.action != "hold":

                            logger.info(

                                f"🤖 [#{slot.slot_id}] Agent: {mid_decision.action} | "

                                f"{mid_decision.reasoning}"

                            )

                    except Exception as e:

                        logger.error(f"🤖 [#{slot.slot_id}] Mid-trade check error: {e}")

                    last_agent_check = now



                # v2: Token profile timeout (per-token grid timeout)

                profile_timeout = slot.token_profile.get("grid_timeout_minutes", GRID_MONITOR_TIMEOUT // 60)

                profile_timeout_seconds = profile_timeout * 60

                if now - start > profile_timeout_seconds:

                    if current_fills > 0 and current_pnl < 0:

                        logger.warning(

                            f"⏰ [#{slot.slot_id}] Grid timeout ({profile_timeout}min) but PnL is negative "
                            f"(${current_pnl:.4f}); holding until position returns positive"

                        )

                        last_progress_time = now

                        start = now - profile_timeout_seconds + 60

                    else:

                        logger.warning(f"⏰ [#{slot.slot_id}] Grid timeout ({profile_timeout}min)")

                        close_reason = "timeout"

                        break



        except asyncio.CancelledError:

            close_reason = slot.close_reason or "cancelled"

            logger.info(f"🛑 [#{slot.slot_id}] Grid task cancelled | reason={close_reason}")

        except Exception as e:

            logger.error(f"❌ [#{slot.slot_id}] Price bus monitor error: {e}")

            close_reason = "price_bus_error"

        finally:

            if price_queue is not None:

                await self.price_bus.unsubscribe(slot.symbol, price_queue)



        # ── Grid Closed: Record Results ──────────────────────

        await self._on_grid_closed(slot, close_reason)



    async def _on_grid_closed(self, slot: GridSlot, close_reason: str):

        """Handle a grid closing — record results, update wallet, free the slot."""

        status = slot.engine.get_status()

        total_pnl = status.get("total_pnl", 0)

        realized = status.get("realized_pnl", 0)

        unrealized = status.get("unrealized_pnl", 0)

        fills = status.get("fills", 0)

        duration = time.time() - slot.started_at



        # Update slot

        slot.close_reason = close_reason

        slot.total_pnl = total_pnl

        slot.realized_pnl = realized

        slot.unrealized_pnl = unrealized

        slot.fills = fills

        slot.duration = duration



        # Record to journal

        try:

            self.journal.record_cycle_close(

                grid_id=slot.state.grid.grid_id,

                total_pnl=total_pnl,

                realized_pnl=realized,

                unrealized_pnl=unrealized,

                fills=fills,

                duration=duration,

                close_reason=close_reason,

                wallet_balance=self.wallet_tracker.get_wallet_state().get("balance", 0.0),

                wallet_exposure_pct=self.wallet_tracker.get_wallet_state().get("exposure_pct", 0.0),

                direction=slot.decision.direction,

                adjusted_leverage=slot.adjusted_leverage,

                adjusted_order_size=slot.adjusted_order_size,

            )

        except Exception as e:

            logger.error(f"Journal record error: {e}")



        # Track stats

        self._total_trades += 1

        self._total_pnl += total_pnl

        if total_pnl > 0:

            self._wins += 1

        else:

            self._losses += 1



        self._completed_trades.append({

            "slot_id": slot.slot_id,

            "symbol": slot.symbol,

            "direction": slot.decision.direction,

            "leverage": slot.adjusted_leverage,

            "order_size": slot.adjusted_order_size,

            "pnl": total_pnl,

            "fills": fills,

            "duration_min": round(duration / 60, 1),

            "close_reason": close_reason,

        })



        # Update adaptive scanner learning immediately so future scans avoid

        # tokens that repeatedly timeout/draw down, without permanently banning them.

        try:

            scanner_learning = getattr(getattr(self, "scanner", None), "learning", None)

            if scanner_learning:

                scanner_learning.record_trade(

                    symbol=slot.symbol,

                    total_pnl=total_pnl,

                    close_reason=close_reason,

                    duration_seconds=duration,

                )

        except Exception as e:

            logger.error(f"Scanner learning update error: {e}")



        # v2: Update wallet tracker — remove position, add realized PnL

        self.wallet_tracker.remove_position(slot.symbol, realized_pnl=realized)



        emoji = "✅" if total_pnl > 0 else "❌"

        wallet_state = self.wallet_tracker.get_wallet_state()

        logger.info(

            f"\n{emoji} [#{slot.slot_id}] GRID CLOSED: {slot.symbol}\n"

            f"  Direction: {slot.decision.direction}\n"

            f"  PnL: ${total_pnl:.4f} (realized=${realized:.4f}, unrealized=${unrealized:.4f})\n"

            f"  Fills: {fills} | Duration: {duration/60:.1f}min\n"

            f"  Leverage: {slot.adjusted_leverage}x | Size: ${slot.adjusted_order_size:.2f}\n"

            f"  Close reason: {close_reason}\n"

            f"  Wallet: ${wallet_state['balance']:.2f} | Exposure: {wallet_state['exposure_pct']:.1f}%\n"

            f"  Portfolio: {len(self.slots)-1} active | "

            f"Total trades: {self._total_trades} | "

            f"Total PnL: ${self._total_pnl:.4f} | "

            f"Win rate: {self._wins/(self._total_trades):.0%}"

        )



        # Post-trade learning (LLM). Optional: disabled by default when
        # USE_LLM_BRAIN=false, so deterministic dry-run slots intentionally
        # have slot.agent=None.
        if slot.agent is not None:
            try:
                cycle_result = {
                    "slot_id": slot.slot_id,
                    "symbol": slot.symbol,
                    "direction": slot.decision.direction,
                    "market_regime": slot.decision.market_regime,
                    "confidence": slot.decision.confidence,
                    "grid_range": f"${slot.state.grid.lower_price:.4f}-${slot.state.grid.upper_price:.4f}",
                    "leverage": slot.adjusted_leverage,
                    "order_size": slot.adjusted_order_size,
                    "num_grids": slot.state.grid.num_grids,
                    "total_pnl": total_pnl,
                    "realized_pnl": realized,
                    "fills": fills,
                    "duration_min": round(duration / 60, 1),
                    "close_reason": close_reason,
                    # v2: Portfolio context
                    "wallet_balance": wallet_state["balance"],
                    "wallet_exposure_pct": wallet_state["exposure_pct"],
                }
                learning = slot.agent.analyze_post_trade(cycle_result)
                if learning:
                    logger.info(f"🧠 [#{slot.slot_id}] Learning: {learning.suggestion}")
                    # v2: Record learning to journal
                    try:
                        self.journal.record_learning(
                            symbol=slot.symbol,
                            what_worked=learning.what_worked,
                            what_failed=learning.what_failed,
                            suggestion=learning.suggestion,
                            pattern=learning.pattern_observed,
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"🧠 Post-trade learning error: {e}")


        # Telegram alert

        try:

            await self.alerter.alert_grid_closed(slot.symbol, total_pnl, close_reason)

        except Exception:

            pass



        # Remove from active slots (frees the slot for new deployment)

        self.slots.pop(slot.slot_id, None)



        logger.info(f"🔓 Slot #{slot.slot_id} freed | Active grids: {len(self.slots)}/{self.max_grids}")



    # ── Portfolio Status Broadcaster ──────────────────────────



    async def _portfolio_status_loop(self):

        """Periodically broadcast portfolio-wide status with v2 wallet data."""

        while self._running:

            await asyncio.sleep(STATUS_BROADCAST_INTERVAL)

            if not self.slots:

                continue

            self._log_portfolio_status()



    def _log_portfolio_status(self):

        """Log a snapshot of all active grids with wallet state."""

        wallet_state = self.wallet_tracker.get_wallet_state()

        risk = self.wallet_tracker.check_liquidation_risk()

        exposure = self.risk_monitor.get_portfolio_exposure(

            self.slots, wallet_state["balance"]

        )



        logger.info(f"\n{'='*70}")

        logger.info(f"📊 PORTFOLIO STATUS v2 | Active: {len(self.slots)}/{self.max_grids}")

        logger.info(f"💰 Wallet: ${wallet_state['balance']:.2f} | "

                    f"Exposure: {wallet_state['exposure_pct']:.1f}% | "

                    f"Free: {wallet_state['free_pct']:.1f}% | "

                    f"Risk: {risk['risk_level']}")

        logger.info(f"📈 Long: {exposure['long_exposure_pct']:.1f}% | "

                    f"Short: {exposure['short_exposure_pct']:.1f}% | "

                    f"Neutral: {exposure['neutral_exposure_pct']:.1f}%")

        if exposure["group_exposures"]:

            for gname, gpct in exposure["group_exposures"].items():

                logger.info(f"  🔗 {gname}: {gpct:.1f}%")

        logger.info(f"{'='*70}")



        total_portfolio_pnl = 0.0

        total_fills = 0



        for slot_id, slot in sorted(self.slots.items()):

            status = slot.engine.get_status()

            pnl = status.get("total_pnl", 0)

            fills = status.get("fills", 0)

            price = status.get("current_price", 0)

            duration = (time.time() - slot.started_at) / 60



            emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"

            total_portfolio_pnl += pnl

            total_fills += fills



            logger.info(

                f"  {emoji} [#{slot_id:2d}] {slot.symbol:18s} | "

                f"dir={slot.decision.direction:7s} | "

                f"pnl=${pnl:+.4f} | "

                f"fills={fills:2d} | "

                f"lev={slot.adjusted_leverage}x | "

                f"${slot.adjusted_order_size:.1f} | "

                f"${price:.4f} | "

                f"{duration:.0f}min"

            )



        logger.info(f"{'─'*70}")

        win_rate = f"{self._wins/(self._total_trades):.0%}" if self._total_trades > 0 else "N/A"

        logger.info(
 f" 💰 Portfolio PnL: ${total_portfolio_pnl:+.4f} (active) + "

 f"${self._total_pnl:.4f} (closed) | "

 f"Total fills: {total_fills} | "

 f"Completed: {self._total_trades} | "

 f"Win rate: {win_rate}"

 )

        logger.info(f"{'='*70}\n")

        _push_api_state(self)



def _serialize_slots(slots: dict) -> dict:
    """Serialize grid slots to JSON-safe dict."""
    result = {}
    for slot_id, slot in slots.items():
        status = slot.engine.get_status()
        result[str(slot_id)] = {
            "slot_id": slot.slot_id,
            "symbol": slot.symbol,
            "direction": getattr(slot.decision, "direction", "unknown"),
            "leverage": slot.adjusted_leverage,
            "adjusted_leverage": slot.adjusted_leverage,
            "order_size": slot.adjusted_order_size,
            "adjusted_order_size": slot.adjusted_order_size,
            "order_size_usdt": slot.adjusted_order_size,
            "pnl": status.get("total_pnl", 0),
            "realized_pnl": status.get("realized_pnl", 0),
            "unrealized_pnl": status.get("unrealized_pnl", 0),
            "fills": status.get("fills", 0),
            "current_price": status.get("current_price", 0),
            "duration_min": round((time.time() - slot.started_at) / 60, 1),
            "status": "active",
            "close_reason": getattr(slot, "close_reason", None),
            "grid_id": slot.state.grid.grid_id if slot.state else None,
            "upper_price": slot.state.grid.upper_price if slot.state else None,
            "lower_price": slot.state.grid.lower_price if slot.state else None,
        "num_grids": slot.state.grid.num_grids if slot.state else None,
        "fill_log": status.get("fill_log", []),
        "grid_levels": status.get("grid_levels", []),
        "allocated_margin_usdt": status.get("allocated_margin_usdt", 0),
    }
    return result


# Shared state file path — written by bot, read by grid_api.py
BOT_STATE_FILE = "/tmp/grid_trader_state.json"
TRADE_DB_FILE = os.path.join(os.path.dirname(__file__), "multi_grid_trades.db")


def _load_symbol_performance() -> dict[str, dict]:
    """Load per-symbol closed-trade expectancy for deployment filtering."""
    if not os.path.exists(TRADE_DB_FILE):
        return {}
    try:
        conn = sqlite3.connect(TRADE_DB_FILE)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT symbol,
                   COUNT(*) AS closed_trades,
                   COALESCE(AVG(total_pnl), 0) AS avg_pnl,
                   COALESCE(SUM(CASE WHEN COALESCE(total_pnl, 0) > 0 THEN 1 ELSE 0 END), 0) AS wins
            FROM grid_cycles
            WHERE closed_at IS NOT NULL
            GROUP BY symbol
            """
        ).fetchall()
        conn.close()
        performance = {}
        for row in rows:
            closed_trades = int(row["closed_trades"] or 0)
            wins = int(row["wins"] or 0)
            performance[row["symbol"]] = {
                "closed_trades": closed_trades,
                "avg_pnl": float(row["avg_pnl"] or 0.0),
                "win_rate": (wins / closed_trades * 100.0) if closed_trades else 0.0,
            }
        return performance
    except Exception as e:
        logger.warning(f"Could not load symbol performance filter: {e}")
        return {}


def _load_closed_trade_source_of_truth() -> tuple[dict, list[dict]]:
    """Return closed-trade stats/history from SQLite.

    The dashboard must not use the manager's in-memory counters for lifetime
    closed trade totals because those counters reset whenever the dry-run
    process restarts. SQLite is the durable source of truth.
    """
    if not os.path.exists(TRADE_DB_FILE):
        return {}, []
    try:
        conn = sqlite3.connect(TRADE_DB_FILE)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT COUNT(*) AS total_trades,
                   COALESCE(SUM(CASE WHEN was_profitable=1 THEN 1 ELSE 0 END), 0) AS wins,
                   COALESCE(SUM(total_pnl), 0) AS total_pnl
            FROM grid_cycles
            WHERE closed_at IS NOT NULL
            """
        ).fetchone()
        total_trades = int(row["total_trades"] or 0)
        wins = int(row["wins"] or 0)
        losses = max(0, total_trades - wins)
        stats = {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / total_trades * 100), 2) if total_trades else 0.0,
            "total_pnl": round(float(row["total_pnl"] or 0.0), 4),
        }
        trades = []
        for r in cur.execute(
            """
            SELECT grid_id, symbol, started_at, closed_at, close_reason,
                   total_pnl, realized_pnl, fills_count, duration_seconds,
                   upper_price, lower_price, num_grids, leverage, was_profitable
            FROM grid_cycles
            WHERE closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT 50
            """
        ).fetchall():
            trades.append({
                "slot_id": r["grid_id"],
                "symbol": r["symbol"],
                "started_at": r["started_at"],
                "closed_at": r["closed_at"],
                "close_reason": r["close_reason"],
                "total_pnl": r["total_pnl"],
                "realized_pnl": r["realized_pnl"],
                "fills_count": r["fills_count"],
                "duration_seconds": r["duration_seconds"],
                "upper_price": r["upper_price"],
                "lower_price": r["lower_price"],
                "num_grids": r["num_grids"],
                "leverage": r["leverage"],
                "was_profitable": bool(r["was_profitable"]),
            })
        conn.close()
        return stats, trades
    except Exception as e:
        logger.warning(f"Could not load DB closed-trade source of truth: {e}")
        return {}, []

def _push_api_state(manager):
    """Dump current bot state to shared file for grid_api.py to serve."""
    try:
        wallet_state = manager.wallet_tracker.get_wallet_state()
        active_pnl = sum(s.engine.get_status().get("total_pnl", 0) for s in manager.slots.values())
        exposure = manager.risk_monitor.get_portfolio_exposure(manager.slots, wallet_state["balance"])
        heartbeat_snapshot = getattr(manager.heartbeat, "last_snapshot", None)
        db_stats, db_trades = _load_closed_trade_source_of_truth()
        closed_stats = db_stats or {
            "total_trades": manager._total_trades,
            "wins": manager._wins,
            "losses": manager._losses,
            "win_rate": round(manager._wins / manager._total_trades * 100, 1) if manager._total_trades > 0 else 0,
            "total_pnl": round(manager._total_pnl, 4),
        }
        heartbeat_state = {
            "active": len(manager.slots),
            "stale": len(getattr(heartbeat_snapshot, "stale_symbols", []) or []),
            "last_run": getattr(heartbeat_snapshot, "timestamp", None),
            "actions": getattr(heartbeat_snapshot, "actions", []) or [],
            "price_bus_up": manager.price_bus.is_running if hasattr(manager.price_bus, "is_running") else True,
            "paused": manager._deployment_paused_until > time.time(),
            "pause_reason": getattr(manager, "_pause_reason", None),
        }
        state = {
            "mode": "running" if manager._running else ("paused" if manager._deployment_paused_until > time.time() else "stopped"),
            "started_at": getattr(manager, "_started_at", None),
            "wallet": {
                "balance": wallet_state["balance"],
                "initial_balance": wallet_state["initial_balance"],
                "exposure_pct": wallet_state["exposure_pct"],
                "total_exposure_pct": wallet_state["exposure_pct"],
                "total_exposure_usdt": wallet_state["total_exposure_usdt"],
                "position_count": wallet_state["position_count"],
                "unrealized_pnl": wallet_state["total_unrealized_pnl"],
                "realized_pnl": wallet_state["total_realized_pnl"],
            },
            "slots": _serialize_slots(manager.slots),
            "completed_trades": db_trades or manager._completed_trades[-50:],
            "scanner_candidates": getattr(manager, "_scanner_candidates", [])[:10],
            "heartbeat": heartbeat_state,
            "stats": {
                **closed_stats,
                "active_pnl": round(active_pnl, 4),
            },
            "portfolio_exposure": exposure,
            "current_prices": {
                slot.symbol: slot.engine.get_status().get("current_price", 0)
                for slot in manager.slots.values()
            },
            "last_update": time.time(),
        }
        tmp_path = f"{BOT_STATE_FILE}.{os.getpid()}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(state, f, default=str, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, BOT_STATE_FILE)
    except Exception as e:
        logger.warning(f"API state push failed: {e}")


# ── Entry Point ─────────────────────────────────────────────────



async def main():

    manager = MultiGridManager(max_grids=MAX_CONCURRENT_GRIDS)

    try:

        await manager.start()

    finally:

        await manager.close()





if __name__ == "__main__":

    asyncio.run(main())
