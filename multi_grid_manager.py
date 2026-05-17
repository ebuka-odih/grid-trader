"""

Multi-Grid Manager v2 — runs up to 20 concurrent grid trades with cross-margin risk management.



v2 Changes:

- RuleBasedAgent (pure logic, no LLM)

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
2. Picks coins algorithmically (scanner scores + sector diversification)
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
import inspect
import fcntl

# Overlay runtime_config + decrypted secrets onto os.environ BEFORE any
# import that reads env at module-level (config, coin_scanner, etc).
import runtime_config  # noqa: F401  side-effect: applies overlay on import

from collections import deque
from dataclasses import dataclass, field

from typing import Optional, Dict, Any



import websockets



from config import (

    BYBIT_WS_PUBLIC, SCAN_INTERVAL_SECONDS,

    TARGET_PNL_LOW, TARGET_PNL_HIGH, TARGET_PNL_PCT_LOW, TARGET_PNL_PCT_HIGH, MAX_DRAWDOWN_PCT,

    BASE_ORDER_SIZE_USDT, DEFAULT_LEVERAGE, BYBIT_API_KEY,

    MARGIN_TYPE, INITIAL_WALLET_BALANCE, TOKEN_PROFILES_PATH,

    MAX_TOTAL_WALLET_EXPOSURE_PCT, MAX_SINGLE_DIRECTION_EXPOSURE_PCT,

    PORTFOLIO_RESERVE_PCT, EMERGENCY_LIQUIDATION_BUFFER_PCT,

    RISK_CHECK_INTERVAL_SECONDS, MAX_SAFE_LEVERAGE, MIN_SAFE_LEVERAGE, MAX_TRADE_WALLET_EXPOSURE_PCT,
    DEFAULT_LEVERAGE, clamp_leverage, resolve_profile_leverage,
    MIN_ORDER_SIZE_USDT,

    VOLATILITY_SCALE_ENABLED, VOLATILITY_SCALE_BASE_ATR,

    VOLATILITY_SCALE_MIN_FACTOR, VOLATILITY_SCALE_MAX_FACTOR,

    DRY_RUN,

)

from coin_scanner import CoinScanner, CoinScore

from dry_run_engine import DryRunEngine, DryRunState

from trading_agent import PreTradeDecision, MidTradeDecision
from rule_agent import RuleBasedAgent

from portfolio_risk_monitor import PortfolioRiskMonitor

from decision_supervisor import DecisionSupervisor

from price_bus import PriceBus

from heartbeat_regulator import HeartbeatRegulator

from wallet_tracker import WalletTracker

from improvement_loop import ImprovementLoop

from telegram_alerter import TelegramAlerter

from grid_engine import GridEngine



logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",

    handlers=[

        logging.StreamHandler(),

        logging.FileHandler("multi_grid_run.log"),

    ],

)

logger = logging.getLogger("multi_grid_manager")


# ── Event ring buffer for the dashboard log panel ────────────────
# Captures logger records whose message contains one of the signal emojis
# we use across the engine (smart-close, scale-out, recovery, wallet, etc).
# Buffer is read from `_push_api_state` and surfaced to the frontend.
from collections import deque  # noqa: E402

_EVENT_BUFFER: deque = deque(maxlen=200)
_EVENT_SIGNAL_PATTERNS = (
    "🛑", "🎯", "🩹", "⚖️", "💰", "⏱",      # core trade-management events
    "🧠", "🤖", "📉", "📈", "📊",            # smart-close + scanner
    "🔄", "❄️", "⚡",                         # recenter, freeze, spike
    "🧪", "🔴", "✅", "❌",                    # cycle / live / close-result
    "🧹", "⏰",                                # stagnation, timeout
    "🏗️",                                     # init
    "DRY FILL", "LIVE FILL",                  # fills
    "Wallet restored", "Runtime",             # startup
    "GRID CLOSED", "GRID OPEN",               # legacy markers
)


class _EventBufferHandler(logging.Handler):
    """Capture INFO+/WARNING+ logs that match any signal pattern."""
    def emit(self, record: logging.LogRecord):
        try:
            msg = record.getMessage()
            if record.levelno < logging.INFO:
                return
            if not any(p in msg for p in _EVENT_SIGNAL_PATTERNS):
                return
            _EVENT_BUFFER.append({
                "ts": time.time(),
                "level": record.levelname,
                "logger": record.name,
                "message": msg,
            })
        except Exception:
            pass


_event_handler = _EventBufferHandler()
_event_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_event_handler)


def _drain_recent_events(limit: int = 50) -> list:
    """Return the most recent events (newest first)."""
    if not _EVENT_BUFFER:
        return []
    items = list(_EVENT_BUFFER)
    return items[-limit:][::-1]





# ── Config ──────────────────────────────────────────────────────



from config import MAX_CONCURRENT_GRIDS

MID_TRADE_CHECK_INTERVAL = 300  # 5 minutes between agent checks (was 2min)

GRID_MONITOR_TIMEOUT = 1800

STATUS_BROADCAST_INTERVAL = 60

HEARTBEAT_INTERVAL_SECONDS = 15

HEARTBEAT_MAX_TICK_AGE_SECONDS = 300

HEARTBEAT_DEPLOY_PAUSE_SECONDS = 10

SCANNER_TOP_N = int(os.getenv("SCANNER_TOP_N_PORTFOLIO", "80"))

MIN_FREE_SLOTS_TO_SCAN = int(os.getenv("MIN_FREE_SLOTS_TO_SCAN", "1"))

NEW_GRID_DEPLOY_DELAY = int(os.getenv("NEW_GRID_DEPLOY_DELAY", "2"))

MAX_DEPLOYMENTS_PER_CYCLE = int(os.getenv("MAX_DEPLOYMENTS_PER_CYCLE", "10"))

MAX_GRIDS_PER_SYMBOL = int(os.getenv("MAX_GRIDS_PER_SYMBOL", "1"))
MIN_INTERNAL_GRID_LEVELS = int(os.getenv("MIN_INTERNAL_GRID_LEVELS", "10"))
MAX_INTERNAL_GRID_LEVELS = int(os.getenv("MAX_INTERNAL_GRID_LEVELS", "20"))
MAX_TRADE_MARGIN_PCT = float(os.getenv("MAX_TRADE_WALLET_EXPOSURE_PCT", str(MAX_TRADE_WALLET_EXPOSURE_PCT)))
MIN_GRID_ORDER_SIZE_USDT = float(os.getenv("MIN_ORDER_SIZE_USDT", str(MIN_ORDER_SIZE_USDT)))

# Slot hygiene: free dead slots instead of letting stale/no-trade tokens sit for hours.
NO_FILL_GRID_TIMEOUT_SECONDS = int(os.getenv("NO_FILL_GRID_TIMEOUT_SECONDS", "900"))  # 15m with no fills
LOSING_STAGNANT_TIMEOUT_SECONDS = int(os.getenv("LOSING_STAGNANT_TIMEOUT_SECONDS", "1800"))  # 30m losing + no progress (was 20m)
STAGNANT_GRID_TIMEOUT_SECONDS = int(os.getenv("STAGNANT_GRID_TIMEOUT_SECONDS", "2400"))  # 40m no meaningful progress
MIN_PROGRESS_PRICE_MOVE_PCT = float(os.getenv("MIN_PROGRESS_PRICE_MOVE_PCT", "0.03"))
MIN_PROGRESS_PNL_MOVE_USDT = float(os.getenv("MIN_PROGRESS_PNL_MOVE_USDT", "0.01"))
LIVE_WALLET_SYNC_INTERVAL_SECONDS = float(os.getenv("LIVE_WALLET_SYNC_INTERVAL_SECONDS", "20"))
LIVE_MIN_AVAILABLE_MARGIN_TO_DEPLOY = float(os.getenv("LIVE_MIN_AVAILABLE_MARGIN_TO_DEPLOY", "1.0"))

# Drawdown-cluster deploy gate: when N grids close with reason in
# {drawdown, spike_close} inside a sliding window, the market is in a
# cross-symbol candle event. Pause new deploys so we don't immediately
# stuff fresh grids into the same volatility — they'd get filled at the
# spike and hit the floor too. Reason ratio (24h DB sample): drawdown
# closes account for ~80% of total losing PnL, with 27% of them clustered
# in 9 five-minute windows; this gate addresses that pattern.
DRAWDOWN_CLUSTER_WINDOW_SEC = float(os.getenv("DRAWDOWN_CLUSTER_WINDOW_SEC", "300"))
DRAWDOWN_CLUSTER_THRESHOLD = int(os.getenv("DRAWDOWN_CLUSTER_THRESHOLD", "3"))
DRAWDOWN_CLUSTER_PAUSE_SEC = float(os.getenv("DRAWDOWN_CLUSTER_PAUSE_SEC", "600"))
DRAWDOWN_CLUSTER_REASONS = frozenset({"drawdown", "spike_close"})

# Full set of engine smart-close events that finalise a grid. The manager's
# tick loop must break on any of these so the close is persisted with the
# engine's actual reason — otherwise the position lives until the profile
# timeout fires and the close is silently relabelled "timeout", which both
# corrupts post-trade analysis and starves the cluster gate above of its
# inputs. Keep in sync with grid_core.CloseReason. `partial_close` is
# intentionally excluded — it's a scale-out that keeps the grid running.
ENGINE_FINAL_CLOSE_EVENTS = frozenset({
    "target_hit", "drawdown", "spike_close", "exposure_breach",
    "grid_imbalance", "time_decay", "momentum_exit",
})


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


def symbol_grid_count(active_symbols, symbol: str) -> int:
    """Count active grids for one symbol."""
    return sum(1 for active_symbol in active_symbols if active_symbol == symbol)


def symbol_has_grid_capacity(active_symbols, symbol: str, max_per_symbol: int = MAX_GRIDS_PER_SYMBOL) -> bool:
    """Return True when another independent grid may be deployed for symbol."""
    return symbol_grid_count(active_symbols, symbol) < max_per_symbol


def _candidate_market_regime(coin: CoinScore) -> str:
    """Use scanner-computed regime when available, fallback to legacy heuristics."""
    regime = str(getattr(coin, "market_regime", "") or "").strip()
    if regime:
        return regime
    if coin.mean_reversion_score > 0.7:
        return "ranging"
    if coin.atr_pct > 2.5 or coin.range_pct > 6:
        return "volatile"
    return "ranging"


def build_scanner_candidate_decision(
    coin: CoinScore,
    token_profile: dict | None = None,
    wallet_balance: float | None = None,
) -> PreTradeDecision:
    """Build the same draft decision the algorithmic picker would create for a scanner score."""
    token_profile = token_profile or {}
    confidence = max(0.0, min(0.9, float(getattr(coin, "grid_score", 0.0) or 0.0)))
    profile_leverage = resolve_profile_leverage(token_profile, fallback=coin.suggested_leverage or DEFAULT_LEVERAGE)
    return PreTradeDecision(
        symbol=coin.symbol,
        direction=getattr(coin, "trend_direction", "neutral") or "neutral",
        confidence=round(confidence, 2),
        upper=coin.suggested_upper,
        lower=coin.suggested_lower,
        num_grids=normalize_grid_density(coin.suggested_grids, wallet_balance=wallet_balance),
        leverage=profile_leverage,
        reasoning=(
            f"Scanner prefilter: score={coin.grid_score:.3f} mr={coin.mean_reversion_score:.2f} "
            f"range={coin.range_pct:.1f}% vol=${coin.volume_24h_usdt/1e6:.0f}M"
        ),
        market_regime=_candidate_market_regime(coin),
        narrative=f"Prefilter draft for {coin.symbol}",
    )


def prefilter_scanner_candidates_for_deploy(
    candidates: list[CoinScore],
    token_profile_by_symbol: dict[str, dict] | None = None,
    wallet_balance: float | None = None,
    decision_supervisor: DecisionSupervisor | None = None,
    active_symbols: list[str] | None = None,
    max_active_per_symbol: int = MAX_GRIDS_PER_SYMBOL,
) -> list[CoinScore]:
    """Discard scanner candidates that would obviously fail the supervisor anyway."""
    decision_supervisor = decision_supervisor or DecisionSupervisor()
    token_profile_by_symbol = token_profile_by_symbol or {}
    active_symbols = list(active_symbols or [])
    deployable: list[CoinScore] = []

    for coin in candidates:
        token_profile = token_profile_by_symbol.get(coin.symbol, {})
        draft_decision = build_scanner_candidate_decision(
            coin,
            token_profile=token_profile,
            wallet_balance=wallet_balance,
        )
        review = decision_supervisor.review_pre_trade_decision(
            decision=draft_decision,
            coin_score=coin,
            token_profile=token_profile,
            active_symbols=active_symbols,
            max_active_per_symbol=max_active_per_symbol,
        )
        if review.approved:
            coin.suggested_lower = draft_decision.lower
            coin.suggested_upper = draft_decision.upper
            coin.suggested_grids = draft_decision.num_grids
            coin.suggested_leverage = draft_decision.leverage
            coin.market_regime = draft_decision.market_regime or coin.market_regime
            coin.trend_direction = draft_decision.direction or coin.trend_direction
            deployable.append(coin)
        else:
            logger.info(
                "🚫 PREFILTER REJECT: %s | %s",
                coin.symbol,
                "; ".join(review.reasons),
            )

    return deployable


# ── Data Structures ─────────────────────────────────────────────



@dataclass

class GridSlot:

    """Represents one active grid trading slot."""

    slot_id: int

    symbol: str

    engine: DryRunEngine

    agent: RuleBasedAgent

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

    coin_score: Optional[CoinScore] = None

    adjusted_leverage: int = 0

    adjusted_order_size: float = 0.0





def coin_score_to_dict(coin: CoinScore) -> dict:

    """Convert CoinScore to a dict for logging/display."""

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

        "suggested_leverage": clamp_leverage(coin.suggested_leverage),

        "trend_direction": coin.trend_direction,

        "market_regime": coin.market_regime,

        "entry_quality_score": coin.entry_quality_score,

        "range_position": coin.range_position,

        "vwap_distance_pct": coin.vwap_distance_pct,

        "pullback_depth_pct": coin.pullback_depth_pct,

        "slope_score": coin.slope_score,

        "acceleration_score": coin.acceleration_score,

        "entry_shape_template": coin.entry_shape_template,

        "entry_shape_spacing": coin.entry_shape_spacing,

        "entry_buy_density_bias": coin.entry_buy_density_bias,

        "entry_sell_density_bias": coin.entry_sell_density_bias,

        "entry_shape_notes": coin.entry_shape_notes,

    }



def _entry_shape_from_coin_score(coin_score: Optional[CoinScore]) -> dict:

    """Extract entry-shape telemetry from a scanner score with stable defaults."""

    if coin_score is None:

        return {

            "entry_quality_score": 0.0,

            "entry_shape_template": "atr_box",

            "entry_shape_spacing": "balanced",

            "entry_buy_density_bias": 0.5,

            "entry_sell_density_bias": 0.5,

            "entry_shape_notes": "",

        }

    return {

        "entry_quality_score": float(getattr(coin_score, "entry_quality_score", 0.0) or 0.0),

        "entry_shape_template": getattr(coin_score, "entry_shape_template", "atr_box") or "atr_box",

        "entry_shape_spacing": getattr(coin_score, "entry_shape_spacing", "balanced") or "balanced",

        "entry_buy_density_bias": float(getattr(coin_score, "entry_buy_density_bias", 0.5) or 0.5),

        "entry_sell_density_bias": float(getattr(coin_score, "entry_sell_density_bias", 0.5) or 0.5),

        "entry_shape_notes": getattr(coin_score, "entry_shape_notes", "") or "",

    }



def _fill_danger_from_slot(slot: GridSlot) -> dict:

    """Summarize same-side fill danger at close/export time."""

    adaptive = getattr(getattr(slot, "engine", None), "_adaptive", None)

    exposure_cap = getattr(adaptive, "exposure_cap", None)

    exposure = getattr(exposure_cap, "exposure", None)

    config = getattr(adaptive, "config", None)

    consecutive_same_side = int(getattr(exposure, "consecutive_same_side", 0) or 0)

    max_same_side_fills = int(getattr(config, "max_same_side_fills", 0) or 0)

    if max_same_side_fills <= 0:

        return {

            "fill_danger": "low",

            "fill_danger_score": 0.0,

            "fill_danger_same_side_fills": consecutive_same_side,

            "fill_danger_max_same_side_fills": max_same_side_fills,

        }

    score = min(1.0, max(0.0, consecutive_same_side / max_same_side_fills))

    if score >= 1.0:

        label = "critical"

    elif score >= 0.8:

        label = "high"

    elif score >= 0.5:

        label = "medium"

    else:

        label = "low"

    return {

        "fill_danger": label,

        "fill_danger_score": round(score, 4),

        "fill_danger_same_side_fills": consecutive_same_side,

        "fill_danger_max_same_side_fills": max_same_side_fills,

    }


# ── Sector Classification for Diversification ───────────────

_SECTOR_MAP: dict[str, str] = {
    # L1
    "SUI": "L1", "APT": "L1", "AVAX": "L1", "NEAR": "L1", "FTM": "L1",
    "MATIC": "L1", "POL": "L1", "TON": "L1", "SEI": "L1", "TIA": "L1",
    "DOT": "L1", "ATOM": "L1", "ALGO": "L1", "ICP": "L1", "MINA": "L1",
    # L2
    "ARB": "L2", "OP": "L2", "STRK": "L2", "ZK": "L2", "MANTA": "L2",
    "BASE": "L2", "IMX": "L2",
    # DeFi
    "AAVE": "DeFi", "UNI": "DeFi", "LINK": "DeFi", "MKR": "DeFi",
    "CRV": "DeFi", "SNX": "DeFi", "COMP": "DeFi", "SUSHI": "DeFi",
    "DYDX": "DeFi", "PENDLE": "DeFi", "JUP": "DeFi", "RAY": "DeFi",
    "ONDO": "DeFi", "MORPHO": "DeFi", "EIGEN": "DeFi",
    # Meme
    "DOGE": "Meme", "SHIB": "Meme", "PEPE": "Meme", "WIF": "Meme",
    "FLOKI": "Meme", "BONK": "Meme", "BRETT": "Meme", "TURBO": "Meme",
    "MOG": "Meme", "PONKE": "Meme", "NEIRO": "Meme", "GIGA": "Meme",
    "FARTCOIN": "Meme", "MEGA": "Meme", "POPCAT": "Meme", "CAT": "Meme",
    # AI
    "FET": "AI", "RENDER": "AI", "TAO": "AI", "WLD": "AI",
    "ARKM": "AI", "AI16Z": "AI", "AIXBT": "AI", "VIRTUAL": "AI",
    "AIGENSYN": "AI", "GRASS": "AI", "ZEREBRO": "AI",
    # Gaming
    "AXS": "Gaming", "GALA": "Gaming", "IMX": "Gaming",
    "BEAM": "Gaming", "RON": "Gaming", "YGG": "Gaming",
    # RWA / TradFi
    "RWA": "RWA", "ONDO": "RWA", "POLYX": "RWA", "CFG": "RWA",
    # Infrastructure
    "FIL": "Infra", "AR": "Infra", "STORJ": "Infra", "THETA": "Infra",
    # DePIN
    "HNT": "DePIN", "IOTX": "DePIN", "AKT": "DePIN",
}

def _get_sector(symbol: str) -> str:
    """Classify a symbol into a sector. Unknown tokens get 'Other'."""
    base = symbol.replace("/USDT:USDT", "").replace("/USDT", "").upper()
    return _SECTOR_MAP.get(base, "Other")





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



        # v2: No LLM client needed — using RuleBasedAgent



        # Core components

        self.scanner = CoinScanner()

        self.grid_calc = GridEngine()

        self.journal = ImprovementLoop(db_path=f"sqlite:///{os.getenv('GRID_TRADER_DB_FILE', 'multi_grid_trades.db')}")

        self.alerter = TelegramAlerter()
        # HeartbeatRegulator looks for a manager._push_api_state callable.
        # Keep this bound wrapper so heartbeat/deployment/fill freshness pushes
        # update the dashboard instead of leaving /api/state stale.
        self._push_api_state = lambda: _push_api_state(self)



        # v2: Risk management

        self.risk_monitor = PortfolioRiskMonitor(profiles_path=TOKEN_PROFILES_PATH)

        self.decision_supervisor = DecisionSupervisor()

        self.price_bus = PriceBus(ws_url=BYBIT_WS_PUBLIC)
        
        # v4: Live trading — execution WebSocket for fill notifications
        self._live_engines: Dict[str, Any] = {}  # symbol -> LiveEngine
        self._execution_ws = None
        self._execution_task = None

        self.heartbeat = HeartbeatRegulator(

            self,

            interval_seconds=HEARTBEAT_INTERVAL_SECONDS,

            max_tick_age_seconds=HEARTBEAT_MAX_TICK_AGE_SECONDS,

            pause_seconds=HEARTBEAT_DEPLOY_PAUSE_SECONDS,

        )

        self.wallet_tracker = WalletTracker(initial_balance=INITIAL_WALLET_BALANCE)
        self._last_live_wallet_sync = 0.0



        # Active grid slots

        self.slots: dict[int, GridSlot] = {}

        self._slot_counter = 0
        self._run_id = f"{os.getpid()}-{int(time.time() * 1000)}"

        self._running = False
        self._started_at: Optional[float] = None
        self._deployment_paused_until = 0.0

        # Track recently-rejected symbols to avoid re-picking them every cycle
        self._recently_rejected: dict[str, float] = {}  # symbol -> rejection timestamp
        self._rejection_cooldown = int(os.getenv("REJECTION_COOLDOWN_SECONDS", "180"))

        # Cross-symbol drawdown-cluster gate: timestamps of recent close events
        # whose reason indicates a price-driven exit (drawdown / spike_close).
        # When the count inside DRAWDOWN_CLUSTER_WINDOW_SEC reaches
        # DRAWDOWN_CLUSTER_THRESHOLD, deployments pause for
        # DRAWDOWN_CLUSTER_PAUSE_SEC.
        self._cluster_close_ts: deque[float] = deque(maxlen=64)

        self._broadcaster_task: Optional[asyncio.Task] = None

        self._risk_monitor_task: Optional[asyncio.Task] = None

        self._heartbeat_task: Optional[asyncio.Task] = None
        self._manual_close_task: Optional[asyncio.Task] = None



        # Performance tracking

        self._total_trades = 0

        self._total_pnl = 0.0

        self._wins = 0

        self._losses = 0

        self._completed_trades: list[dict] = []

        # Restore wallet + cumulative stats from the closed-trade DB so that a
        # container rebuild does not wipe accumulated PnL (only the in-memory
        # objects reset; the DB on the persistent volume keeps the truth).
        self._restore_wallet_from_db()

        logger.info(

            f"🏗️ Multi-Grid Manager v2 initialized | max_grids={max_grids} | "

            f"margin={MARGIN_TYPE} | wallet=${self.wallet_tracker.get_balance():.2f} | "

            f"risk_monitor=active"

        )

    def _record_cluster_close(self, close_reason: str) -> None:
        """
        Record a price-driven close for the cross-symbol cluster gate.
        When the rolling count of cluster-eligible closes inside the window
        crosses the threshold, pause new deployments. Idempotent: extending
        an already-active pause is fine.
        """
        if close_reason not in DRAWDOWN_CLUSTER_REASONS:
            return
        now = time.time()
        cutoff = now - DRAWDOWN_CLUSTER_WINDOW_SEC
        # Prune old entries
        while self._cluster_close_ts and self._cluster_close_ts[0] < cutoff:
            self._cluster_close_ts.popleft()
        self._cluster_close_ts.append(now)
        if len(self._cluster_close_ts) >= DRAWDOWN_CLUSTER_THRESHOLD:
            new_until = now + DRAWDOWN_CLUSTER_PAUSE_SEC
            if new_until > self._deployment_paused_until:
                self._deployment_paused_until = new_until
                logger.warning(
                    f"⚠️ DRAWDOWN CLUSTER: {len(self._cluster_close_ts)} "
                    f"price-driven closes in last "
                    f"{DRAWDOWN_CLUSTER_WINDOW_SEC:.0f}s — pausing deploys for "
                    f"{DRAWDOWN_CLUSTER_PAUSE_SEC:.0f}s"
                )

    def _session_start_file(self) -> str:
        """Path of the persisted session-start marker."""
        state_file = os.getenv("GRID_TRADER_STATE_FILE", "/tmp/grid_trader_state.json")
        return os.path.join(os.path.dirname(state_file) or ".", "grid_trader_session.json")

    def _restore_session_start(self) -> float:
        """
        Return the persisted session-start timestamp so the dashboard runtime
        survives container restarts. Creates the marker file on first run.
        Honours an env reset: setting GRID_TRADER_RESET_RUNTIME=1 forces a
        fresh start (file is rewritten with the current time).
        """
        path = self._session_start_file()
        if os.getenv("GRID_TRADER_RESET_RUNTIME", "0") == "1":
            ts = time.time()
            self._write_session_start(path, ts)
            logger.info(f"⏱  Runtime reset (env): session_start={ts}")
            return ts
        try:
            if os.path.exists(path):
                with open(path) as f:
                    payload = json.load(f)
                ts = float(payload.get("started_at") or 0.0)
                if ts > 0:
                    age_h = (time.time() - ts) / 3600
                    logger.info(f"⏱  Runtime restored: session_start={ts} (age={age_h:.1f}h)")
                    return ts
        except Exception as e:
            logger.warning(f"Could not read session-start file: {e}")
        ts = time.time()
        self._write_session_start(path, ts)
        logger.info(f"⏱  Runtime fresh: session_start={ts}")
        return ts

    def _write_session_start(self, path: str, ts: float):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                json.dump({"started_at": ts}, f)
        except Exception as e:
            logger.warning(f"Could not write session-start file: {e}")

    def _restore_wallet_from_db(self):
        """Replay closed-trade totals from the DB onto the fresh WalletTracker."""
        try:
            stats, _trades = _load_closed_trade_source_of_truth()
        except Exception as e:
            logger.warning(f"Wallet restore: could not read closed-trade DB: {e}")
            return
        if not stats:
            return
        total_pnl = float(stats.get("total_pnl") or 0.0)
        total_trades = int(stats.get("total_trades") or 0)
        wins = int(stats.get("wins") or 0)
        losses = int(stats.get("losses") or 0)
        if total_trades > 0:
            self._total_trades = total_trades
            self._total_pnl = total_pnl
            self._wins = wins
            self._losses = losses
        if total_pnl != 0.0:
            self.wallet_tracker.restore_realized_pnl(total_pnl)

    @staticmethod
    def _extract_live_wallet_from_balance(balance: dict) -> tuple[float, float | None, float | None]:
        """Extract equity/available/used margin fields from a ccxt Bybit balance payload."""
        total = balance.get("total") or {}
        free = balance.get("free") or {}
        used = balance.get("used") or {}

        info = balance.get("info") or {}
        result = info.get("result") or {}
        rows = result.get("list") or []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}

        def _f(value, default=None):
            try:
                if value is None or value == "":
                    return default
                return float(value)
            except (TypeError, ValueError):
                return default

        equity = _f(row.get("totalEquity"))
        if equity is None:
            equity = _f(row.get("totalWalletBalance"))
        if equity is None:
            equity = _f(total.get("USDT"), 0.0)

        available = _f(row.get("totalAvailableBalance"))
        if available is None:
            available = _f(free.get("USDT"))

        margin_used = _f(row.get("totalInitialMargin"))
        if margin_used is None:
            margin_used = _f(used.get("USDT"))

        return equity or 0.0, available, margin_used

    async def _sync_live_wallet(self, *, force: bool = False):
        """Refresh wallet tracker from exchange while in live mode."""
        if DRY_RUN:
            return
        now = time.time()
        if not force and (now - self._last_live_wallet_sync) < LIVE_WALLET_SYNC_INTERVAL_SECONDS:
            return
        try:
            balance = await self.grid_calc.exchange.fetch_balance({"type": "swap"})
            equity, available, margin_used = self._extract_live_wallet_from_balance(balance)
            if equity > 0:
                self.wallet_tracker.set_live_balance(
                    equity=equity,
                    available_margin=available,
                    margin_used=margin_used,
                )
                self._last_live_wallet_sync = now
        except Exception as exc:
            logger.warning(f"Live wallet sync failed: {exc}")



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

        logger.info(f"  Mode: {'DRY-RUN (simulated orders)' if DRY_RUN else 'LIVE (real orders)'}")

        logger.info("=" * 70)



        if (not DRY_RUN) and (not BYBIT_API_KEY or BYBIT_API_KEY == "your_api_key_here"):

            logger.error("❌ API keys not set for live mode!")

            return

        if DRY_RUN and (not BYBIT_API_KEY or BYBIT_API_KEY == "your_api_key_here"):

            logger.info("🧪 Dry-run mode without Bybit API keys — live trading is impossible")



        self._running = True
        # Use the persisted session start so the dashboard runtime survives
        # container rebuilds. _restore_session_start() seeds it from disk on
        # first start; subsequent starts read the same file.
        if self._started_at is None:
            self._started_at = self._restore_session_start()
        _push_api_state(self)

        # Handle SIGTERM for graceful shutdown (supervisor sends SIGTERM)
        import signal
        def _sigterm_handler(signum, frame):
            logger.info("🛑 SIGTERM received — initiating graceful shutdown")
            self._running = False
        signal.signal(signal.SIGTERM, _sigterm_handler)

        await self.price_bus.start()
        
        # v4: Start execution WebSocket for live trading
        if not DRY_RUN:
            await self._start_execution_ws()
            await self._sync_live_wallet(force=True)



        # Start portfolio status broadcaster

        self._broadcaster_task = asyncio.create_task(self._portfolio_status_loop())



        # v2: Start risk monitor loop

        self._risk_monitor_task = asyncio.create_task(self._risk_monitor_loop())



        # Patch B: orphan reaper — flatten any Bybit position the bot

        # is not managing in a slot (catches WS-race orphans).

        self._orphan_reaper_task = asyncio.create_task(self._orphan_reaper_loop())



        # v3: Heartbeat regulator keeps subsystems fresh and pauses deployment

        # if market data becomes stale.

        self._heartbeat_task = asyncio.create_task(self.heartbeat.run())



        # Admin "Apply" watcher: when the UI writes /data/restart.signal,
        # exit cleanly so docker-compose's restart policy brings us back
        # up with the freshly-overlaid runtime_config + secrets.

        self._restart_watcher_task = asyncio.create_task(self._restart_signal_watcher())
        self._manual_close_task = asyncio.create_task(self._manual_close_request_watcher())



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



    async def _start_execution_ws(self):
        """Start WebSocket for execution (fill) updates."""
        import websockets
        import json
        from config import BYBIT_WS_PRIVATE
        
        ws_url = BYBIT_WS_PRIVATE
        
        async def _execution_loop():
            """Connect to Bybit private WS and listen for executions."""
            while self._running:
                try:
                    async with websockets.connect(ws_url) as ws:
                        # Authenticate
                        from config import BYBIT_API_KEY, BYBIT_API_SECRET
                        import hmac
                        import hashlib
                        import time as _time
                        
                        expires = int(_time.time() * 1000) + 10000
                        signature = hmac.new(
                            BYBIT_API_SECRET.encode(),
                            f"GET/realtime{expires}".encode(),
                            hashlib.sha256
                        ).hexdigest()
                        
                        auth_msg = {
                            "op": "auth",
                            "args": [BYBIT_API_KEY, expires, signature]
                        }
                        await ws.send(json.dumps(auth_msg))
                        
                        # Subscribe to execution
                        sub_msg = {"op": "subscribe", "args": ["execution"]}
                        await ws.send(json.dumps(sub_msg))
                        
                        logger.info("🔴 Execution WebSocket connected")
                        
                        async for message in ws:
                            try:
                                data = json.loads(message)
                                if data.get("topic") == "execution":
                                    for exec_data in data.get("data", []):
                                        await self._handle_execution(exec_data)
                            except Exception as e:
                                logger.error(f"Execution WS message error: {e}")
                
                except Exception as e:
                    logger.error(f"Execution WS connection error: {e}")
                    await asyncio.sleep(5)
        
        self._execution_task = asyncio.create_task(_execution_loop())
    
    @staticmethod
    def _normalize_ws_symbol(raw: str) -> str:
        """Convert Bybit V5 raw symbol (e.g. "ORCAUSDT") to ccxt unified
        ("ORCA/USDT:USDT"). If `raw` doesn't end in USDT we return it unchanged
        (the registry lookup will then simply miss, which is fine)."""
        if not raw:
            return raw
        if "/" in raw or ":" in raw:
            return raw
        for quote in ("USDT", "USDC", "USD"):
            if raw.endswith(quote) and len(raw) > len(quote):
                base = raw[: -len(quote)]
                return f"{base}/{quote}:{quote}"
        return raw

    async def _handle_execution(self, exec_data: dict):
        """Handle execution update from WebSocket."""
        symbol = exec_data.get("symbol", "")
        # Bybit V5 execution stream returns the venue order id under `orderId`.
        # We previously read `orderLinkId`, which is the *client*-supplied id;
        # we never set one when placing orders, so it was always empty and
        # every fill failed to match a level → grids closed as
        # `no_fills_timeout` while real positions accumulated on the exchange.
        order_id = (
            exec_data.get("orderId")
            or exec_data.get("orderID")
            or exec_data.get("orderLinkId")
            or ""
        )
        side = exec_data.get("side", "")
        qty = float(exec_data.get("execQty", 0) or 0)
        price = float(exec_data.get("execPrice", 0) or 0)
        exec_type = exec_data.get("execType", "")

        # Only forward actual trade fills (skip funding, settlement, etc.).
        if exec_type and exec_type != "Trade":
            return
        if qty <= 0 or not order_id:
            return

        # Bybit V5 execution stream emits raw venue symbols (e.g. "ORCAUSDT");
        # we register LiveEngines under ccxt-unified symbols (e.g. "ORCA/USDT:USDT").
        # Try the raw symbol first, then a normalized variant.
        engine = self._live_engines.get(symbol)
        if engine is None:
            engine = self._live_engines.get(self._normalize_ws_symbol(symbol))
        if engine and hasattr(engine, "notify_fill"):
            engine.notify_fill(order_id, side, qty, price)
            logger.info(
                f"🔴 Execution routed: {symbol} {side} {qty} @ {price} (order_id={order_id[:12]}…)"
            )
        else:
            logger.warning(
                f"⚠️ Execution unrouted: {symbol} {side} {qty} @ {price} "
                f"(no LiveEngine registered for symbol; order_id={order_id[:12]}…)"
            )
    
    def register_live_engine(self, symbol: str, engine):
        """Register a LiveEngine for execution routing."""
        self._live_engines[symbol] = engine
        logger.info(f"🔴 LiveEngine registered for {symbol}")
    
    def unregister_live_engine(self, symbol: str):
        """Unregister a LiveEngine."""
        self._live_engines.pop(symbol, None)
    
    async def close(self):

        """Shut down all grids gracefully."""

        self._running = False
        
        # v4: Stop execution WebSocket
        if self._execution_task and not self._execution_task.done():
            self._execution_task.cancel()
            try:
                await self._execution_task
            except asyncio.CancelledError:
                pass

        for task in [
            getattr(self, "_broadcaster_task", None),
            getattr(self, "_risk_monitor_task", None),
            getattr(self, "_heartbeat_task", None),
            getattr(self, "_restart_watcher_task", None),
            getattr(self, "_manual_close_task", None),
        ]:

            if task and not task.done():

                task.cancel()

                try:

                    await task

                except asyncio.CancelledError:

                    pass

        # Record PnL for all active grids before cancelling tasks
        for slot_id, slot in list(self.slots.items()):
            try:
                status = slot.engine.get_status() if hasattr(slot.engine, 'get_status') else {}
                total_pnl = status.get("total_pnl", 0)
                realized = status.get("realized_pnl", 0)
                unrealized = status.get("unrealized_pnl", 0)
                fills = status.get("fills", 0)
                duration = time.time() - slot.started_at

                self.journal.record_cycle_close(
                    grid_id=slot.state.grid.grid_id,
                    total_pnl=total_pnl,
                    realized_pnl=realized,
                    unrealized_pnl=unrealized,
                    fills=fills,
                    duration=duration,
                    close_reason="manager_shutdown",
                    wallet_balance=self.wallet_tracker.get_wallet_state().get("balance", 0.0),
                    wallet_exposure_pct=self.wallet_tracker.get_wallet_state().get("exposure_pct", 0.0),
                    direction=slot.decision.direction,
                    adjusted_leverage=slot.adjusted_leverage,
                    adjusted_order_size=slot.adjusted_order_size,
                    fill_danger=_fill_danger_from_slot(slot)["fill_danger"],
                    fill_danger_score=_fill_danger_from_slot(slot)["fill_danger_score"],
                )
                self._total_trades += 1
                self._total_pnl += total_pnl
                if total_pnl > 0:
                    self._wins += 1
                else:
                    self._losses += 1
                logger.info(f"📝 Shutdown PnL saved: {slot.symbol} | pnl=${total_pnl:.4f} | fills={fills}")
            except Exception as e:
                logger.error(f"Failed to save shutdown PnL for slot {slot_id}: {e}")

        for slot_id, slot in list(self.slots.items()):

            if slot.task and not slot.task.done():

                slot.task.cancel()

                try:

                    await slot.task

                except asyncio.CancelledError:

                    pass

        await self.price_bus.stop()
        
        # v4: Close all live engines
        for engine in self._live_engines.values():
            if hasattr(engine, 'close'):
                await engine.close()
        self._live_engines.clear()

        await self.scanner.close()

        wallet_state = self.wallet_tracker.get_wallet_state()

        wallet_stats = self.wallet_tracker.get_stats()

        logger.info(

            f"🛑 Multi-Grid Manager v2 stopped | "

            f"wallet=${wallet_state.get('balance', 0):.2f} | "

            f"pnl={wallet_stats.get('pnl_pct', 0):.1f}% | "

            f"trades={wallet_stats.get('total_trades', 0)} | "

            f"win_rate={wallet_stats.get('win_rate', 0):.0f}%"

        )



    # ── v2: Risk Monitor Loop ──────────────────────────────────



    async def _risk_monitor_loop(self):

        """Run portfolio risk checks every RISK_CHECK_INTERVAL_SECONDS."""

        while self._running:

            await asyncio.sleep(RISK_CHECK_INTERVAL_SECONDS)
            await self._sync_live_wallet()

            if not self.slots:

                continue

            await self._run_emergency_checks()



    async def _run_emergency_checks(self):

        """Check for emergency conditions and act on them."""

        wallet_balance = self.wallet_tracker.get_balance()

        emergency = self.risk_monitor.check_emergency(wallet_balance, self.slots)
        if isinstance(emergency, dict) and emergency.get("emergency"):
            logger.warning(f"🚨 RISK MONITOR: {emergency.get('message', 'emergency exposure condition')}")
            try:
                await self.alerter.send_message(f"🚨 RISK MONITOR: {emergency.get('message')}")
            except Exception:
                pass

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

                    leverage=slot.adjusted_leverage or resolve_profile_leverage(profile, fallback=DEFAULT_LEVERAGE),

                    unrealized_pnl=status.get("total_pnl", 0),

                    num_fills=status.get("fills", 0),

                )



    # ── Deployment Cycle ──────────────────────────────────────



    async def _deployment_cycle(self):

        """Check for free slots and deploy new grids if available."""

        await self._sync_live_wallet()

        free_slots = self.max_grids - len(self.slots)

        now = time.time()

        if now < getattr(self, "_deployment_paused_until", 0):

            remaining = self._deployment_paused_until - now

            cluster_active = len(self._cluster_close_ts) >= DRAWDOWN_CLUSTER_THRESHOLD
            label = "by drawdown cluster" if cluster_active else "by heartbeat"
            logger.warning(f"💓 Deployment paused {label} for {remaining:.0f}s")

            return

        if free_slots < MIN_FREE_SLOTS_TO_SCAN and len(self.slots) > 0:

            return



        logger.info(f"\n🔄 Deployment cycle | active={len(self.slots)}/{self.max_grids} | free={free_slots}")

        if not DRY_RUN:
            wallet_state = self.wallet_tracker.get_wallet_state()
            if wallet_state.get("available_margin", 0.0) < LIVE_MIN_AVAILABLE_MARGIN_TO_DEPLOY:
                logger.warning(
                    "⏸️ Live deployment paused: available margin too low "
                    f"(${wallet_state.get('available_margin', 0.0):.4f})"
                )
                return



        # Scan market

        scores = await self.scanner.scan()

        if not scores:

            logger.warning("⚠️ No suitable coins found")

            return



        # Filter via risk monitor (blacklist check) and recently-rejected cooldown

        active_symbols = [slot.symbol for slot in self.slots.values()]

        now_ts = time.time()

        # Expire old rejection entries
        self._recently_rejected = {
            sym: ts for sym, ts in self._recently_rejected.items()
            if now_ts - ts < self._rejection_cooldown
        }

        available = [

            s for s in scores

            if symbol_has_grid_capacity(active_symbols, s.symbol)

            and not self.risk_monitor.is_blacklisted(s.symbol)

            and s.symbol not in self._recently_rejected

        ]



        if not available:

            logger.info("📊 All candidate symbols are at per-symbol capacity or blacklisted")

            return

        wallet_balance = self.wallet_tracker.get_balance()
        token_profile_by_symbol = {
            coin.symbol: self.risk_monitor.get_token_profile(coin.symbol)
            for coin in available[:SCANNER_TOP_N]
        }
        available = prefilter_scanner_candidates_for_deploy(
            available,
            token_profile_by_symbol=token_profile_by_symbol,
            wallet_balance=wallet_balance,
            decision_supervisor=self.decision_supervisor,
            active_symbols=active_symbols,
            max_active_per_symbol=MAX_GRIDS_PER_SYMBOL,
        )

        if not available:

            logger.info("📊 All scanner candidates were prefiltered by supervisor rules before selection")

            return

        # Pick coins algorithmically using scanner scores + diversification

        num_to_pick = min(free_slots, len(available), MAX_DEPLOYMENTS_PER_CYCLE)

        picks = self._select_coins_algorithmically(available[:SCANNER_TOP_N], num_to_pick, wallet_balance)

        if not picks:

            logger.warning("📊 Algorithmic selector returned no picks")

            return



        # Deploy each pick as a concurrent grid

        wallet_balance = self.wallet_tracker.get_balance()



        for decision in picks:

            if len(self.slots) >= self.max_grids:

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

                self._recently_rejected[decision.symbol] = time.time()

                continue

            for warning in review.warnings:

                logger.warning(f"🧠 {decision.symbol} supervisor warning: {warning}")



            # v2: Risk check — approve or adjust the deployment

            risk_result = self.risk_monitor.check_deploy(

                symbol=decision.symbol,

                direction=decision.direction,

                leverage=decision.leverage,

                order_size_usdt=token_profile.get("order_size_usdt", BASE_ORDER_SIZE_USDT),

                wallet_balance=wallet_balance,

                active_grids=self.slots,
                num_grids=decision.num_grids,

            )



            if not risk_result["approved"]:

                logger.warning(f"🛡️ {decision.symbol} REJECTED by risk monitor: {risk_result['reasons']}")

                self._recently_rejected[decision.symbol] = time.time()

                continue



            # Apply risk-adjusted params

            decision.leverage = risk_result["adjusted_leverage"]

            adjusted_order_size = risk_result["adjusted_order_size"]



            # v2: Volatility-scaled sizing

            adjusted_order_size = calculate_volatility_scaled_size(

                base_size=adjusted_order_size,

                atr_pct=coin_score.atr_pct,

                wallet_balance=wallet_balance,

                max_wallet_exposure_pct=min(
                    float(token_profile.get("max_wallet_exposure_pct", MAX_TRADE_MARGIN_PCT)),
                    MAX_TRADE_MARGIN_PCT,
                ),

                leverage=decision.leverage,

                num_grids=decision.num_grids,

            )



            # Override grid params with agent + risk-adjusted decision

            coin_score.suggested_upper = decision.upper

            coin_score.suggested_lower = decision.lower

            coin_score.suggested_grids = decision.num_grids

            coin_score.suggested_leverage = decision.leverage



            # Deploy with adjusted params

            try:
                await self._deploy_grid(

                    coin_score, decision,

                    token_profile=token_profile,

                    adjusted_leverage=risk_result["adjusted_leverage"],

                    adjusted_order_size=adjusted_order_size,

                )

                active_symbols.append(decision.symbol)
            except Exception as exc:
                logger.warning(f"🛡️ {decision.symbol} DEPLOY FAILED: {exc}")
                self._recently_rejected[decision.symbol] = time.time()
                continue



            # Rate limit between deployments

            await asyncio.sleep(NEW_GRID_DEPLOY_DELAY)



    def _select_coins_algorithmically(self, available: list[CoinScore], num_picks: int, wallet_balance: float) -> list[PreTradeDecision]:
        """
        Select coins algorithmically using scanner scores + diversification.
        No LLM involved — pure rule-based selection with sector diversification.
        """
        if not available or num_picks <= 0:
            return []

        # Score each coin: base grid_score + volume bonus + diversification potential
        scored: list[tuple[float, CoinScore, str]] = []
        for coin in available:
            sector = _get_sector(coin.symbol)
            # Base score from scanner (range + ATR + volume + mean reversion)
            base = coin.grid_score
            # Volume bonus: higher volume = more liquid = better for grids
            vol_bonus = min(0.05, coin.volume_24h_usdt / 2e9)  # cap at 0.05
            # Mean reversion bonus: critical for grid success
            mr_bonus = coin.mean_reversion_score * 0.05
            # Penalize extreme volatility slightly
            vol_penalty = 0.0
            if coin.atr_pct > 3.0:
                vol_penalty = (coin.atr_pct - 3.0) * 0.02

            final_score = base + vol_bonus + mr_bonus - vol_penalty
            scored.append((final_score, coin, sector))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Greedy diversification: pick top coins but limit per sector
        # Allow max 2 coins from the same sector (or 40% of picks, whichever is larger)
        max_per_sector = max(2, int(num_picks * 0.4))
        sector_counts: dict[str, int] = {}
        picks: list[PreTradeDecision] = []

        for score, coin, sector in scored:
            if len(picks) >= num_picks:
                break

            current_count = sector_counts.get(sector, 0)
            if current_count >= max_per_sector and sector != "Other":
                continue

            # Get token profile for direction bias and params
            profile = self.risk_monitor.get_token_profile(coin.symbol)
            profile_order_size = profile.get("order_size_usdt", 5.0)

            decision = build_scanner_candidate_decision(
                coin,
                token_profile=profile,
                wallet_balance=wallet_balance,
            )
            decision.reasoning = (
                f"Algorithmic pick: score={coin.grid_score:.3f} mr={coin.mean_reversion_score:.2f} "
                f"range={coin.range_pct:.1f}% vol=${coin.volume_24h_usdt/1e6:.0f}M"
            )
            decision.narrative = (
                f"{sector} sector, {decision.market_regime} regime, {coin.atr_pct:.1f}% ATR"
            )
            picks.append(decision)
            sector_counts[sector] = current_count + 1

            logger.info(
                f"📊 ALGO PICK: {decision.symbol} | dir={decision.direction} | "
                f"sector={sector} | score={coin.grid_score:.3f} | "
                f"mr={coin.mean_reversion_score:.2f} | trend={coin.trend_direction} | "
                f"regime={decision.market_regime} | conf={decision.confidence:.2f} | "
                f"grid={decision.lower:.4f}-{decision.upper:.4f} | "
                f"grids={decision.num_grids} | lev={decision.leverage}x"
            )

        return picks

    def _agent_pick_portfolio(self, top_coins: list[dict], num_picks: int) -> list[PreTradeDecision]:

        """DEPRECATED: LLM-based portfolio selection removed. Use _select_coins_algorithmically()."""

        logger.warning("_agent_pick_portfolio called but is deprecated — use _select_coins_algorithmically")

        return []



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



        # v2: Create agent with shared client instead of new client per grid

        agent = RuleBasedAgent()



        # Deploy a symmetric two-sided grid using risk-adjusted order size so actual grid quantities

        # match the portfolio risk monitor and wallet tracker.

        final_order_size = adjusted_order_size or token_profile.get("order_size_usdt", BASE_ORDER_SIZE_USDT)

        from dry_run_engine import DryRunEngine
        from adaptive_grid import AdaptiveConfig, default_config
        
        # v3: Create adaptive config from token profile
        adaptive_cfg = default_config()
        # Override from token profile if specified
        if token_profile.get("spike_window_sec"):
            adaptive_cfg.spike_window_sec = token_profile["spike_window_sec"]
        if token_profile.get("spike_threshold_pct"):
            adaptive_cfg.spike_threshold_pct = token_profile["spike_threshold_pct"]
        if token_profile.get("max_same_side_fills"):
            adaptive_cfg.max_same_side_fills = token_profile["max_same_side_fills"]
        if token_profile.get("recenter_trigger_pct"):
            adaptive_cfg.recenter_trigger_pct = token_profile["recenter_trigger_pct"]
        if token_profile.get("exp_sizing_gamma"):
            adaptive_cfg.exp_sizing_gamma = token_profile["exp_sizing_gamma"]
        
        # v4: Use LiveEngine when DRY_RUN=false, DryRunEngine when true.
        # The live engine receives the manager's TelegramAlerter so it can
        # raise critical alerts (flatten failures, scale-out failures).
        if DRY_RUN:
            engine = DryRunEngine(adaptive_config=adaptive_cfg)
        else:
            from live_engine import LiveEngine
            engine = LiveEngine(adaptive_config=adaptive_cfg, alerter=self.alerter)
        
        state = await self._deploy_symmetric_grid(

            engine,

            coin_score,

            decision.direction,

            order_size_usdt=final_order_size,

        )
        
        # v4: Register LiveEngine for execution routing
        if not DRY_RUN and hasattr(engine, 'notify_fill'):
            self.register_live_engine(coin_score.symbol, engine)



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

            entry_shape_template=coin_score.entry_shape_template,

            entry_shape_spacing=coin_score.entry_shape_spacing,

            entry_shape_confidence=coin_score.entry_quality_score,

            entry_buy_density_bias=coin_score.entry_buy_density_bias,

            entry_sell_density_bias=coin_score.entry_sell_density_bias,

            entry_shape_notes=coin_score.entry_shape_notes,

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

            coin_score=coin_score,

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



    async def _deploy_symmetric_grid(

        self,

        engine,  # DryRunEngine or LiveEngine

        coin_score: CoinScore,

        direction: str,

        order_size_usdt: float = BASE_ORDER_SIZE_USDT,

    ):
        """Deploy a symmetric two-sided grid to an isolated engine."""

        grid = self.grid_calc.calculate_grid_levels(

            symbol=coin_score.symbol,

            upper=coin_score.suggested_upper,

            lower=coin_score.suggested_lower,

            num_grids=normalize_grid_density(coin_score.suggested_grids),

            current_price=coin_score.price,

            leverage=coin_score.suggested_leverage,

            order_size_usdt=order_size_usdt,

        )


        logger.info(
            f"📐 TWO-SIDED GRID ({direction} signal): {coin_score.symbol} | "
            f"preserving symmetric buy-below / sell-above ladder"
        )



        # v4: Create appropriate state based on engine type
        from live_engine import LiveEngine
        
        if isinstance(engine, LiveEngine):
            # LiveEngine — create LiveState and place real orders
            from live_engine import LiveState
            state = LiveState(
                grid=grid,
                started_at=time.time(),
                current_price=coin_score.price,
            )
            
            # Place real orders via GridEngine (async)
            grid_engine = self.grid_calc  # Reuse the GridEngine instance
            
            # Set leverage first
            try:
                await grid_engine.set_leverage(grid.symbol, grid.leverage)
            except Exception as e:
                logger.error(f"Failed to set leverage: {e}")
            
            # Place limit orders
            placed_levels = 0
            failed_levels = 0
            first_error: str | None = None
            for level in grid.grid_levels:
                try:
                    order = await grid_engine.exchange.create_limit_order(
                        symbol=grid.symbol,
                        side=level.side.lower(),
                        amount=level.qty,
                        price=level.price,
                    )
                    level.order_id = order["id"]
                    level.status = "placed"
                    placed_levels += 1
                    logger.info(f"  ✅ {level.side} {level.qty} @ {level.price:.4f} → {order['id']}")
                except Exception as e:
                    level.status = "failed"
                    failed_levels += 1
                    if first_error is None:
                        first_error = str(e)
                    logger.error(f"  ❌ {level.side} {level.qty} @ {level.price:.4f} → {e}")

            if failed_levels > 0 or placed_levels == 0:
                if placed_levels > 0:
                    try:
                        await grid_engine.cancel_grid(grid)
                    except Exception as cancel_exc:
                        logger.error(f"Failed to rollback partial live grid {grid.symbol}: {cancel_exc}")
                raise RuntimeError(
                    f"live order placement incomplete for {grid.symbol}: "
                    f"placed={placed_levels}/{len(grid.grid_levels)} "
                    f"failed={failed_levels} first_error={first_error or 'unknown'}"
                )

            # CRITICAL: hand the manager's GridEngine + Adaptive grid into the
            # LiveEngine so its _flatten_and_cancel / _reset_grid_for_next_cycle
            # / scale-out paths can call close_position / cancel_grid /
            # exchange.create_limit_order. Without these the LiveEngine crashes
            # on every TP close with `'NoneType' has no attribute close_position`
            # and the position is left orphaned on the exchange.
            engine._grid_engine = grid_engine
            engine.state = state
        else:
            # DryRunEngine — create DryRunState (simulated)
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

        if total_pnl < 0:

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
                    
                    # Track fills before update
                    fills_before = len(slot.state.fills) if hasattr(slot.state, 'fills') else 0

                    event = slot.engine.on_price_update(price)
                    if inspect.isawaitable(event):
                        event = await event
                    
                    # Record new fills to DB
                    fills_after = len(slot.state.fills) if hasattr(slot.state, 'fills') else 0
                    if fills_after > fills_before and hasattr(slot.state, 'fills'):
                        for fill in slot.state.fills[fills_before:]:
                            try:
                                self.journal.record_fill(
                                    grid_id=slot.state.grid.grid_id,
                                    symbol=slot.symbol,
                                    side=getattr(fill, 'side', 'unknown'),
                                    price=getattr(fill, 'price', 0),
                                    qty=getattr(fill, 'qty', 0),
                                    realized_pnl=getattr(fill, 'sim_pnl', 0),
                                    order_id=f"sim_{getattr(fill, 'level_index', 0)}",
                                )
                            except Exception as e:
                                logger.error(f"Failed to record fill: {e}")

                    # v3: Handle new adaptive grid events
                    if event in ENGINE_FINAL_CLOSE_EVENTS:
                        close_reason = event
                        break
                    elif event == "cycle_complete":
                        # v4: Multi-cycle — grid reset for next cycle, continue monitoring
                        logger.info(f"🔄 [#{slot.slot_id}] Cycle complete for {slot.symbol} — continuing")
                    elif event in {"recenter", "trail"}:
                        # Grid was recentered or trailed — log but continue
                        logger.info(f"🔄 [#{slot.slot_id}] {event} event for {slot.symbol}")
                    elif event is None and slot.engine._adaptive and slot.engine._adaptive.spike_detector.is_paused():
                        # Spike pause — skip status logging to reduce noise
                        continue

                except asyncio.TimeoutError:

                    # No tick within the timeout; keep periodic checks alive.

                    pass



                now = time.time()

                status = slot.engine.get_status()

                current_price = float(status.get("current_price") or 0.0)

                current_fills = int(status.get("fills") or 0)

                current_pnl = float(status.get("total_pnl") or 0.0)

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

                    # Don't realise a losing position via stagnation close.
                    # The hard floor / time-decay / recovery window in the
                    # smart-close engine already manage exits on losing
                    # trades — trigger only if we're break-even or positive,
                    # OR if we've blown through the hard cap.
                    status = slot.engine.get_status()
                    allocated = float(status.get("allocated_margin_usdt") or 0.0)
                    loss_pct_margin = (
                        (-current_pnl / allocated * 100) if allocated > 0 and current_pnl < 0 else 0.0
                    )
                    hard_max_min = float(os.getenv("GRID_HARD_MAX_MINUTES", "240"))
                    age_min = (now - start) / 60

                    if stale_reason is not None:
                        logger.warning(

                            f"🧹 [#{slot.slot_id}] Closing stagnant grid | reason={stale_reason} | "

                            f"symbol={slot.symbol} | age={age_min:.1f}m | "

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



                # LLM mid-trade check (skip if grid < 10min old — let it work)
                grid_age_seconds = now - slot.started_at
                if now - last_agent_check >= MID_TRADE_CHECK_INTERVAL and grid_age_seconds >= 600:

                    try:

                        # v2: Include portfolio context in mid-trade check

                        grid_status = slot.engine.get_status()

                        grid_status["portfolio_wallet"] = self.wallet_tracker.get_wallet_state()

                        grid_status["token_profile"] = slot.token_profile
                        grid_status["allocated_margin"] = slot.allocated_margin if hasattr(slot, 'allocated_margin') else 0
                        grid_status["age_seconds"] = now - slot.started_at

                        if slot.agent:
                            mid_decision = slot.agent.decide_mid_trade(grid_status)
                        else:
                            from trading_agent import MidTradeDecision
                            mid_decision = MidTradeDecision(action="hold", reasoning="no agent")

                        if mid_decision.action == "close":
                            logger.info(
                                f"🤖 [#{slot.slot_id}] Agent requested close but was ignored; "
                                f"smart-close / engine exit rules are the only close authority. "
                                f"{mid_decision.reasoning}"
                            )
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

                    # Don't realise a big loss just because the clock ran
                    # out. If the position is in significant loss, extend the
                    # deadline up to GRID_HARD_MAX_MINUTES (env-tunable) so the
                    # smart-close engine can manage exit (recovery window /
                    # hard floor / time-decay all use margin-% loss).
                    status = slot.engine.get_status()
                    total_pnl = float(status.get("total_pnl") or 0.0)
                    allocated = float(status.get("allocated_margin_usdt") or 0.0)
                    loss_pct_margin = (-total_pnl / allocated * 100) if allocated > 0 and total_pnl < 0 else 0.0
                    hard_max_min = float(os.getenv("GRID_HARD_MAX_MINUTES", "240"))
                    age_min = (now - start) / 60

                    if loss_pct_margin > 1.0 and age_min < hard_max_min:
                        # Any losing position (>1% margin) inside the hard cap
                        # is held for smart-close to manage. Avoids realising
                        # small losses on profile timeout that would likely
                        # have recovered with a few more minutes. Throttle the
                        # warning so we don't spam logs.
                        if int(age_min) % 5 == 0:
                            logger.warning(
                                f"⏰ [#{slot.slot_id}] Profile timeout ({profile_timeout}m) "
                                f"deferred — loss={loss_pct_margin:.1f}% margin (age={age_min:.0f}m, "
                                f"hard_max={hard_max_min:.0f}m). Smart close will manage exit."
                            )
                    else:
                        logger.warning(
                            f"⏰ [#{slot.slot_id}] Grid timeout ({profile_timeout}m) "
                            f"loss_margin={loss_pct_margin:.1f}% age={age_min:.0f}m"
                        )
                        close_reason = "timeout"
                        break



        except asyncio.CancelledError:

            close_reason = slot.close_reason or "cancelled"

            logger.info(f"🛑 [#{slot.slot_id}] Grid task cancelled | reason={close_reason}")

        except Exception as e:

            # Transient WebSocket errors should not kill the grid immediately.
            # The price_bus auto-reconnects, so we retry a few times before giving up.
            error_str = str(e).lower()
            is_transient = any(kw in error_str for kw in [
                "keepalive ping timeout", "1011", "connection closed",
                "no close frame", "ping failed", "recv error",
            ])

            if is_transient:
                logger.warning(f"⚠️ [#{slot.slot_id}] Transient price bus error (will retry): {e}")
                # Don't close — let the grid be re-deployed by the manager's next cycle
                close_reason = "transient_error"
            else:
                logger.error(f"❌ [#{slot.slot_id}] Price bus monitor error: {e}")
                close_reason = "price_bus_error"

        finally:

            if price_queue is not None:

                await self.price_bus.unsubscribe(slot.symbol, price_queue)



        # ── Grid Closed: Record Results ──────────────────────

        await self._on_grid_closed(slot, close_reason)



    async def _on_grid_closed(self, slot: GridSlot, close_reason: str):

        """Handle a grid closing — record results, update wallet, free the slot."""

        # Patch N: ensure the live position is actually flattened on the
        # exchange BEFORE we tear down local state. Previously, manager-
        # initiated close paths (timeout, transient_error, cancelled,
        # price_bus_error) skipped LiveEngine._flatten_and_cancel entirely
        # because that method is only called from the engine's own tick
        # loop. Result: bot freed the slot, reset local PnL, and left the
        # real position open on Bybit until the orphan reaper picked it
        # up 60-180s later — exactly the "trades on Bybit but not on UI"
        # symptom. Always call _flatten_and_cancel here in live mode.
        if not DRY_RUN and getattr(slot, "engine", None):
            try:
                # _flatten_and_cancel is idempotent; safe even if the
                # engine already flattened via _handle_close.
                await slot.engine._flatten_and_cancel(close_reason)
            except Exception as e:
                logger.error(
                    f"❌ [#{slot.slot_id}] _on_grid_closed: explicit flatten failed: "
                    f"{type(e).__name__}: {e}"
                )

        # v4: Unregister LiveEngine if it was live
        if not DRY_RUN:
            self.unregister_live_engine(slot.symbol)

        status = slot.engine.get_status()

        total_pnl = status.get("total_pnl", 0)

        realized = status.get("realized_pnl", 0)

        unrealized = status.get("unrealized_pnl", 0)

        fills = status.get("fills", 0)

        duration = time.time() - slot.started_at



        # Update slot

        slot.close_reason = close_reason

        # Feed price-driven closes to the cross-symbol cluster gate.
        self._record_cluster_close(close_reason)

        slot.total_pnl = total_pnl

        slot.realized_pnl = realized

        slot.unrealized_pnl = unrealized

        slot.fills = fills

        slot.duration = duration



        # Record to journal

        try:

            fill_danger = _fill_danger_from_slot(slot)

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

                fill_danger=fill_danger["fill_danger"],

                fill_danger_score=fill_danger["fill_danger_score"],

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



        # v2: Update wallet tracker — remove position, add total PnL.
        # `total_pnl` (= realized + unrealized at close) is the correct
        # number to bank: when a grid stops, any open unrealized value
        # represents the simulated exit at the closing price. Previously
        # only `realized` was banked, which dropped the unrealized portion
        # and produced a small drift between wallet.balance and stats.total_pnl.
        self.wallet_tracker.remove_position(slot.symbol, realized_pnl=total_pnl)



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



        # Post-trade learning (LLM)

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

            if slot.agent:
                learning = slot.agent.analyze_post_trade(cycle_result)
            else:
                learning = None

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

        # Patch B: remember this symbol so the orphan reaper doesn't

        # flatten the very position we're trying to close on the next tick

        # (reduce-only market orders can take a few seconds to settle).

        if not hasattr(self, "_recently_closed_symbols"):

            self._recently_closed_symbols = {}

        self._recently_closed_symbols[slot.symbol] = time.time()



        logger.info(f"🔓 Slot #{slot.slot_id} freed | Active grids: {len(self.slots)}/{self.max_grids}")



    # ── Admin Apply: watch for restart sentinel ──────────────────

    async def _restart_signal_watcher(self):
        """Poll /data/restart.signal; exit gracefully when admin clicks Apply.

        The signal file is written by admin.py's /api/admin/apply handler.
        We set _running=False, which causes the manager loop to drain and
        the process to exit. The container entrypoint detects the manager
        exit and brings the whole container down; docker-compose restart
        policy ('unless-stopped') brings it back up, at which point
        runtime_config.apply_overlay() at import time picks up the new
        values from runtime_config.json and runtime_secrets.bin.
        """
        from pathlib import Path
        sentinel = Path("/data/restart.signal")
        last_seen_mtime = sentinel.stat().st_mtime if sentinel.exists() else 0.0
        while self._running:
            try:
                await asyncio.sleep(5)
                if not sentinel.exists():
                    continue
                mtime = sentinel.stat().st_mtime
                if mtime > last_seen_mtime:
                    logger.warning(
                        "🔄 Admin Apply requested via UI — exiting cleanly so "
                        "docker-compose can restart with new runtime config."
                    )
                    # Consume the signal so we don't loop on restart
                    try:
                        sentinel.unlink()
                    except Exception:
                        pass
                    self._running = False
                    return
            except Exception as exc:
                logger.warning(f"restart_signal_watcher error: {exc}")

    def _manual_close_requests_file(self):
        from runtime_config import DATA_DIR
        return DATA_DIR / "manual_close_requests.jsonl"

    async def _manual_close_request_watcher(self):
        """Poll for manual close requests written by the API process."""
        requests_file = self._manual_close_requests_file()
        while self._running:
            try:
                await asyncio.sleep(2)
                if not requests_file.exists() or requests_file.stat().st_size <= 0:
                    continue
                processing = requests_file.with_suffix(f".processing.{os.getpid()}.jsonl")
                try:
                    requests_file.replace(processing)
                except FileNotFoundError:
                    continue
                except OSError:
                    continue

                try:
                    for raw in processing.read_text(encoding="utf-8").splitlines():
                        if not raw.strip():
                            continue
                        try:
                            payload = json.loads(raw)
                        except Exception as exc:
                            logger.warning(f"manual close watcher bad payload: {exc}")
                            continue
                        await self._handle_manual_close_request(payload)
                finally:
                    try:
                        processing.unlink()
                    except FileNotFoundError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"manual_close_request_watcher error: {exc}")

    async def _handle_manual_close_request(self, payload: dict):
        slot_id = int(payload.get("slot_id") or 0)
        reason = str(payload.get("reason") or "manual_close")
        slot = self.slots.get(slot_id)
        if not slot:
            logger.warning(f"manual close requested for missing slot #{slot_id}")
            return
        if slot.close_reason:
            logger.info(f"manual close ignored for slot #{slot_id}; already closing as {slot.close_reason}")
            return

        slot.close_reason = reason
        try:
            slot.state.is_active = False
        except Exception:
            pass

        logger.warning(f"🧨 Manual close requested for slot #{slot_id} {slot.symbol} | reason={reason}")
        self._push_api_state()

        if slot.task and not slot.task.done():
            slot.task.cancel()
            return

        await self._on_grid_closed(slot, reason)


    # ── Portfolio Status Broadcaster ─────────────────────────-



    async def _portfolio_status_loop(self):

        """Periodically broadcast portfolio-wide status with v2 wallet data."""

        while self._running:

            await asyncio.sleep(STATUS_BROADCAST_INTERVAL)

            if not self.slots:

                continue

            self._log_portfolio_status()

    # ── Patch B: orphan reaper ──────────────────────────────────────────
    # Periodically audit Bybit-side positions vs locally tracked slots
    # and reduce-only market-close any position the bot doesn't manage.
    # Catches WS-race orphans, partial-fill leftovers, and any position
    # that lingers past a slot's lifecycle.
    async def _orphan_reaper_loop(self):
        """Every ORPHAN_REAPER_INTERVAL seconds, flatten Bybit positions
        with no managing slot. Idempotent — silent when there are none."""
        interval = float(os.getenv("ORPHAN_REAPER_INTERVAL_SEC", "60"))
        # Grace period: don't reap a symbol whose slot was just closed
        # (the periodic scan can race with cleanup). Symbol → unix ts.
        recent_close_grace_sec = float(
            os.getenv("ORPHAN_REAPER_GRACE_SEC", "120")
        )
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self.grid_calc or not getattr(self.grid_calc, "exchange", None):
                    continue
                # Build set of symbols the bot currently manages.
                tracked = {slot.symbol for slot in self.slots.values()}
                # Also include symbols closed within the grace window —
                # their reduce-only close may not have settled yet.
                grace = getattr(self, "_recently_closed_symbols", {})
                now = time.time()
                tracked.update(
                    sym for sym, ts in grace.items()
                    if now - ts < recent_close_grace_sec
                )
                # Fetch all positions in one call (faster than per-symbol).
                try:
                    positions = await self.grid_calc.exchange.fetch_positions()
                except Exception as e:
                    logger.debug(f"orphan_reaper: fetch_positions failed: {e}")
                    continue
                orphans = []
                for p in positions:
                    sym = p.get("symbol")
                    qty_raw = p.get("contracts")
                    if not sym or qty_raw is None:
                        continue
                    try:
                        qty = abs(float(qty_raw))
                    except (TypeError, ValueError):
                        continue
                    if qty <= 0:
                        continue
                    if sym in tracked:
                        continue
                    orphans.append((sym, p.get("side"), qty))
                if not orphans:
                    continue
                logger.warning(
                    f"🧹 ORPHAN REAPER: {len(orphans)} unmanaged Bybit positions; flattening"
                )
                for sym, pside, qty in orphans:
                    market_side = "sell" if pside == "long" else "buy"
                    placed = await self._reap_one(sym, pside, qty, market_side)
                    if placed:
                        oid = placed.get("id") if isinstance(placed, dict) else "?"
                        logger.warning(
                            f"  🧹 reaped {sym} {pside} {qty} → {market_side.upper()} reduceOnly id={oid}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"orphan_reaper_loop: unexpected error: {e}")

    async def _reap_one(self, sym: str, pside: str, qty: float, market_side: str):
        """Try to reduce-only-close one orphan position. On Bybit's
        retCode 110007 ("ab not enough for new order") — which fires when
        free margin is too low to validate even a reduce-only order — we
        first cancel any open orders for that symbol to free margin, then
        retry. Returns the order dict on success, None on failure (logs
        the failure)."""
        ex = self.grid_calc.exchange
        params = {"reduceOnly": True, "category": "linear"}
        for attempt in range(3):
            try:
                return await ex.create_market_order(sym, market_side, qty, params=params)
            except Exception as e:
                msg = str(e)
                is_insufficient = "110007" in msg or "ab not enough" in msg.lower()
                if is_insufficient and attempt < 2:
                    # Cancel all open orders for this symbol — they hold
                    # margin/order-quota that prevents the close from
                    # being accepted. Then retry the reduce-only close.
                    try:
                        await ex.cancel_all_orders(sym, params={"category": "linear"})
                        logger.warning(
                            f"  🧹 reaper: cancelled open orders on {sym} to free "
                            f"margin (110007 attempt {attempt+1})"
                        )
                    except Exception as ce:
                        logger.warning(
                            f"  🧹 reaper: cancel_all_orders failed on {sym}: "
                            f"{type(ce).__name__}: {ce}"
                        )
                    # Try the next attempt with the same qty; if Bybit
                    # still refuses, fall back to halving on attempt 3.
                    if attempt == 1:
                        qty = round(qty * 0.5, 8) or qty
                    continue
                logger.error(
                    f"  ❌ orphan_reaper failed for {sym}: {type(e).__name__}: {e}"
                )
                return None
        logger.error(
            f"  ❌ orphan_reaper exhausted retries for {sym} qty={qty}"
        )
        return None



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
        grid_id = slot.state.grid.grid_id if slot.state else None
        entry_shape = _entry_shape_from_coin_score(getattr(slot, "coin_score", None))
        fill_danger = _fill_danger_from_slot(slot)
        result[str(slot_id)] = {
            "slot_id": slot.slot_id,
            "grid_id": grid_id,
            "trade_id": grid_id,
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
            "grid_id": grid_id,
            "upper_price": slot.state.grid.upper_price if slot.state else None,
            "lower_price": slot.state.grid.lower_price if slot.state else None,
            "num_grids": slot.state.grid.num_grids if slot.state else None,
            "fill_log": status.get("fill_log", []),
            "grid_levels": status.get("grid_levels", []),
            "allocated_margin_usdt": status.get("allocated_margin_usdt", 0),
            "target_pnl_low": status.get("target_pnl_low"),
            "target_pnl_high": status.get("target_pnl_high"),
            "target_pnl_pct_low": status.get("target_pnl_pct_low"),
            "target_pnl_pct_high": status.get("target_pnl_pct_high"),
            "max_drawdown_pct": status.get("max_drawdown_pct"),
            "duration_sec": status.get("duration_sec"),
            "filled_levels": status.get("filled_levels", 0),
            "position_qty": status.get("position_qty", 0),
            "position_side": status.get("position_side"),
            "entry_price": status.get("entry_price"),
            "imbalance_ratio": status.get("imbalance_ratio", 0),
            **entry_shape,
            **fill_danger,
        }
    return result


# Shared state/lock/DB paths — written by bot, read by grid_api.py.
# Keep env overrides so Docker can use mounted persistent volumes while the
# legacy VPS process can keep the old /tmp + project-root defaults.
BOT_STATE_FILE = os.getenv("GRID_TRADER_STATE_FILE", "/tmp/grid_trader_state.json")
MANAGER_LOCK_FILE = os.getenv("GRID_TRADER_MANAGER_LOCK_FILE", "/tmp/grid_trader_manager.lock")
TRADE_DB_FILE = os.getenv("GRID_TRADER_DB_FILE", os.path.join(os.path.dirname(__file__), "multi_grid_trades.db"))


def _load_closed_trade_source_of_truth() -> tuple[dict, list[dict]]:
    """Return DB-backed closed trade stats/history for dashboard continuity."""
    if not os.path.exists(TRADE_DB_FILE):
        return {}, []
    try:
        conn = sqlite3.connect(TRADE_DB_FILE)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Schema may pre-date the `adjusted_order_size` column on older DBs;
        # fall back to a literal so the query still succeeds.
        existing_cols = {r[1] for r in cur.execute("PRAGMA table_info(grid_cycles)").fetchall()}
        order_size_expr = (
            "adjusted_order_size" if "adjusted_order_size" in existing_cols
            else "0.0 AS adjusted_order_size"
        )
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
            f"""
            SELECT grid_id, symbol, started_at, closed_at, close_reason,
                   total_pnl, realized_pnl, fills_count, duration_seconds,
                   upper_price, lower_price, num_grids, leverage, was_profitable,
                   {order_size_expr}
            FROM grid_cycles
            WHERE closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT 50
            """
        ).fetchall():
            order_size = float(r["adjusted_order_size"] or 0.0)
            num_grids = int(r["num_grids"] or 0)
            allocated_margin = order_size * num_grids
            total_pnl = float(r["total_pnl"] or 0.0)
            profit_pct = (total_pnl / allocated_margin * 100) if allocated_margin > 0 else 0.0
            trades.append({
                "slot_id": r["grid_id"],
                "grid_id": r["grid_id"],
                "trade_id": r["grid_id"],
                "symbol": r["symbol"],
                "started_at": r["started_at"],
                "closed_at": r["closed_at"],
                "close_reason": r["close_reason"],
                "total_pnl": total_pnl,
                "realized_pnl": r["realized_pnl"],
                "fills_count": r["fills_count"],
                "duration_seconds": r["duration_seconds"],
                "upper_price": r["upper_price"],
                "lower_price": r["lower_price"],
                "num_grids": num_grids,
                "leverage": r["leverage"],
                "was_profitable": bool(r["was_profitable"]),
                "order_size": order_size,
                "allocated_margin": allocated_margin,
                "profit_percentage": round(profit_pct, 2),
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
        cluster_ts = getattr(manager, "_cluster_close_ts", None)
        cluster_active_count = 0
        if cluster_ts is not None:
            cutoff = time.time() - DRAWDOWN_CLUSTER_WINDOW_SEC
            cluster_active_count = sum(1 for t in cluster_ts if t >= cutoff)
        cluster_state = {
            "active_count": cluster_active_count,
            "threshold": DRAWDOWN_CLUSTER_THRESHOLD,
            "window_sec": DRAWDOWN_CLUSTER_WINDOW_SEC,
            "paused_until": manager._deployment_paused_until if cluster_active_count >= DRAWDOWN_CLUSTER_THRESHOLD else 0,
        }
        state = {
            "mode": "running" if manager._running else ("paused" if manager._deployment_paused_until > time.time() else "stopped"),
            "writer_pid": os.getpid(),
            "run_id": getattr(manager, "_run_id", None),
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
            "cluster_gate": cluster_state,
            "current_prices": {
                slot.symbol: slot.engine.get_status().get("current_price", 0)
                for slot in manager.slots.values()
            },
            "events": _drain_recent_events(50),
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

    lock_fh = open(MANAGER_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.seek(0)
        lock_fh.truncate()
        lock_fh.write(str(os.getpid()))
        lock_fh.flush()
    except BlockingIOError:
        logger.error(
            f"Another multi_grid_manager.py instance already holds {MANAGER_LOCK_FILE}; "
            "exiting to prevent conflicting state writers."
        )
        return

    manager = MultiGridManager(max_grids=MAX_CONCURRENT_GRIDS)

    try:

        await manager.start()

    finally:

        await manager.close()
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass





if __name__ == "__main__":

    asyncio.run(main())
