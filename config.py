# ============================================================
# Agentic Grid Trader — v2 Cross-Margin Config (Multi-Exchange)
# ============================================================

import os
from dotenv import load_dotenv

load_dotenv()


def _env_int(name, default):
    return int(os.getenv(name, str(default)))


def _env_float(name, default):
    return float(os.getenv(name, str(default)))


# --- Exchange Configuration ---
EXCHANGE = os.getenv("EXCHANGE", "bybit").lower()
TRADING_MODE = os.getenv("TRADING_MODE", "testnet")

# --- API Keys ---
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# --- Cross-Margin Mode ---
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "cross")

# --- Wallet Configuration ---
INITIAL_WALLET_BALANCE = _env_float("INITIAL_WALLET_BALANCE", 50.0)
BASE_ORDER_SIZE_USDT = _env_float("BASE_ORDER_SIZE_USDT", 0.5)
MAX_TOTAL_WALLET_EXPOSURE_PCT = _env_float("MAX_TOTAL_WALLET_EXPOSURE_PCT", 25)
TOKEN_PROFILES_PATH = os.getenv("TOKEN_PROFILES_PATH", "token_profiles.json")

# --- Grid Strategy ---
_RAW_MIN_SAFE_LEVERAGE = _env_int("MIN_SAFE_LEVERAGE", 10)
_RAW_MAX_SAFE_LEVERAGE = _env_int("MAX_SAFE_LEVERAGE", 35)
if _RAW_MAX_SAFE_LEVERAGE < _RAW_MIN_SAFE_LEVERAGE:
    _RAW_MAX_SAFE_LEVERAGE = _RAW_MIN_SAFE_LEVERAGE

MIN_SAFE_LEVERAGE = _RAW_MIN_SAFE_LEVERAGE
MAX_SAFE_LEVERAGE = _RAW_MAX_SAFE_LEVERAGE
MIN_DEPLOY_LEVERAGE = MIN_SAFE_LEVERAGE
MAX_DEPLOY_LEVERAGE = MAX_SAFE_LEVERAGE


def clamp_leverage(value, *, minimum=None, maximum=None):
    min_allowed = MIN_SAFE_LEVERAGE if minimum is None else int(minimum)
    max_allowed = MAX_SAFE_LEVERAGE if maximum is None else int(maximum)
    if max_allowed < min_allowed:
        max_allowed = min_allowed
    try:
        raw = int(float(value))
    except (TypeError, ValueError):
        raw = DEFAULT_LEVERAGE if "DEFAULT_LEVERAGE" in globals() else min_allowed
    return max(min_allowed, min(max_allowed, raw))


DEFAULT_LEVERAGE = clamp_leverage(_env_int("DEFAULT_LEVERAGE", 35))
DEFAULT_NUM_GRIDS = _env_int("DEFAULT_NUM_GRIDS", 8)
MAX_TRADE_WALLET_EXPOSURE_PCT = _env_float("MAX_TRADE_WALLET_EXPOSURE_PCT", 15.0)
MIN_ORDER_SIZE_USDT = _env_float("MIN_ORDER_SIZE_USDT", 0.15)

# --- PnL Targets (percentage of allocated margin) ---
TARGET_PNL_LOW = _env_float("TARGET_PNL_LOW", 0.5)
TARGET_PNL_HIGH = _env_float("TARGET_PNL_HIGH", 1.5)
TARGET_PNL_PCT_LOW = _env_float("TARGET_PNL_PCT_LOW", 1.0)
TARGET_PNL_PCT_HIGH = _env_float("TARGET_PNL_PCT_HIGH", 2.0)
MAX_DRAWDOWN_PCT = _env_float("MAX_DRAWDOWN_PCT", 12.0)
MAX_SCANNER_LEVERAGE = clamp_leverage(_env_int("MAX_SCANNER_LEVERAGE", MAX_SAFE_LEVERAGE))


def resolve_profile_max_leverage(profile=None):
    profile = profile or {}
    return clamp_leverage(profile.get("max_leverage", MAX_SAFE_LEVERAGE))


def resolve_profile_leverage(profile=None, fallback=None):
    profile = profile or {}
    preferred = profile.get("leverage", DEFAULT_LEVERAGE if fallback is None else fallback)
    return clamp_leverage(preferred, maximum=resolve_profile_max_leverage(profile))


# --- Volatility-Scaled Sizing ---
VOLATILITY_SCALE_ENABLED = os.getenv("VOLATILITY_SCALE_ENABLED", "true").lower() == "true"
VOLATILITY_SCALE_BASE_ATR = _env_float("VOLATILITY_SCALE_BASE_ATR", 1.5)
VOLATILITY_SCALE_MIN_FACTOR = _env_float("VOLATILITY_SCALE_MIN_FACTOR", 0.3)
VOLATILITY_SCALE_MAX_FACTOR = _env_float("VOLATILITY_SCALE_MAX_FACTOR", 2.0)

# --- Progressive (Martingale) Sizing ---
PROGRESSIVE_SIZING_ENABLED = os.getenv("PROGRESSIVE_SIZING_ENABLED", "true").lower() == "true"
PROGRESSIVE_MIN_FACTOR = _env_float("PROGRESSIVE_MIN_FACTOR", 0.5)
PROGRESSIVE_MAX_FACTOR = _env_float("PROGRESSIVE_MAX_FACTOR", 1.5)
PROGRESSIVE_CURVE_POWER = _env_float("PROGRESSIVE_CURVE_POWER", 1.5)

# --- Double-Down / DCA Re-Entry (Passivbot-style) ---
DOUBLE_DOWN_ENABLED = os.getenv("DOUBLE_DOWN_ENABLED", "true").lower() == "true"
DOUBLE_DOWN_FACTOR = _env_float("DOUBLE_DOWN_FACTOR", 1.8)
DOUBLE_DOWN_MAX_ENTRIES = _env_int("DOUBLE_DOWN_MAX_ENTRIES", 3)
DOUBLE_DOWN_SPACING_PCT = _env_float("DOUBLE_DOWN_SPACING_PCT", 1.2)
DOUBLE_DOWN_MIN_LOSS_PCT = _env_float("DOUBLE_DOWN_MIN_LOSS_PCT", 1.5)
DOUBLE_DOWN_MAX_LOSS_PCT = _env_float("DOUBLE_DOWN_MAX_LOSS_PCT", 8.0)

# --- Trailing Take-Profit (Profit Lock-In) ---
TRAILING_PROFIT_ENABLED = os.getenv("TRAILING_PROFIT_ENABLED", "true").lower() == "true"
TRAILING_PROFIT_THRESHOLD_PCT = _env_float("TRAILING_PROFIT_THRESHOLD_PCT", 0.5)
TRAILING_PROFIT_RETRACEMENT_PCT = _env_float("TRAILING_PROFIT_RETRACEMENT_PCT", 0.2)

# --- Unstuck Mechanism (Gradual Loss Realization) ---
UNSTUCK_ENABLED = os.getenv("UNSTUCK_ENABLED", "true").lower() == "true"
UNSTUCK_LOSS_ALLOWANCE_PCT = _env_float("UNSTUCK_LOSS_ALLOWANCE_PCT", 4.0)
UNSTUCK_MIN_LOSS_PCT = _env_float("UNSTUCK_MIN_LOSS_PCT", 2.0)
UNSTUCK_MIN_AGE_MINUTES = _env_int("UNSTUCK_MIN_AGE_MINUTES", 10)
UNSTUCK_CLOSE_FRACTION = _env_float("UNSTUCK_CLOSE_FRACTION", 0.08)
UNSTUCK_COOLDOWN_MINUTES = _env_int("UNSTUCK_COOLDOWN_MINUTES", 5)

# --- Coin Scanner ---
SCAN_INTERVAL_SECONDS = _env_int("SCAN_INTERVAL_SECONDS", 60)
MIN_24H_VOLUME_USDT = _env_float("MIN_24H_VOLUME_USDT", 500000)
SCAN_TOP_N_COINS = _env_int("SCAN_TOP_N_COINS", 80)
SCANNER_SYMBOL_DELAY_MS = _env_int("SCANNER_SYMBOL_DELAY_MS", 50)
SCANNER_RATE_LIMIT_RETRIES = _env_int("SCANNER_RATE_LIMIT_RETRIES", 3)
SCANNER_RATE_LIMIT_BACKOFF_SECONDS = _env_float("SCANNER_RATE_LIMIT_BACKOFF_SECONDS", 1.0)

# --- Safety Filters ---
MAX_24H_RANGE_PCT = _env_float("MAX_24H_RANGE_PCT", 80.0)
MIN_24H_RANGE_PCT = _env_float("MIN_24H_RANGE_PCT", 0.5)
MAX_ATR_PCT = _env_float("MAX_ATR_PCT", 8.0)
MIN_MEAN_REVERSION = _env_float("MIN_MEAN_REVERSION", 0.35)
MIN_ENTRY_QUALITY = _env_float("MIN_ENTRY_QUALITY", 0.0)
MAX_GRID_WIDTH_PCT = _env_float("MAX_GRID_WIDTH_PCT", 15.0)

# --- Blacklist ---
COIN_BLACKLIST = os.getenv("COIN_BLACKLIST", "BTC,RAVE,GALA,PEOPLE,BLUR,ACE,NIL")

# --- Portfolio Risk ---
MAX_SINGLE_DIRECTION_EXPOSURE_PCT = _env_float("MAX_SINGLE_DIRECTION_EXPOSURE_PCT", 50)
PORTFOLIO_RESERVE_PCT = _env_float("PORTFOLIO_RESERVE_PCT", 20)
EMERGENCY_LIQUIDATION_BUFFER_PCT = _env_float("EMERGENCY_LIQUIDATION_BUFFER_PCT", 10)
RISK_CHECK_INTERVAL_SECONDS = _env_int("RISK_CHECK_INTERVAL_SECONDS", 30)

# --- Dry Run ---
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# --- Trading Agent (LLM) ---
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
TRADING_AGENT_MODEL = os.getenv("TRADING_AGENT_MODEL", "meta/llama-3.3-70b-instruct")
TRADING_AGENT_FALLBACK_MODEL = os.getenv("TRADING_AGENT_FALLBACK_MODEL", "meta/llama-3.1-8b-instruct")
AGENT_MID_TRADE_INTERVAL = _env_int("AGENT_MID_TRADE_INTERVAL", 120)

# --- Multi-Grid Mode ---
MAX_CONCURRENT_GRIDS = _env_int("MAX_CONCURRENT_GRIDS", 3)
MID_TRADE_CHECK_INTERVAL = _env_int("MID_TRADE_CHECK_INTERVAL", 120)
GRID_MONITOR_TIMEOUT = _env_int("GRID_MONITOR_TIMEOUT", 1800)
SCANNER_TOP_N_PORTFOLIO = _env_int("SCANNER_TOP_N_PORTFOLIO", 30)
MIN_FREE_SLOTS_TO_SCAN = _env_int("MIN_FREE_SLOTS_TO_SCAN", 1)
MAX_DEPLOYMENTS_PER_CYCLE = _env_int("MAX_DEPLOYMENTS_PER_CYCLE", 1)
MAX_GRIDS_PER_SYMBOL = _env_int("MAX_GRIDS_PER_SYMBOL", 2)

# --- Grid Timeouts ---
NO_FILL_GRID_TIMEOUT_SECONDS = _env_int("NO_FILL_GRID_TIMEOUT_SECONDS", 900)
STAGNANT_GRID_TIMEOUT_SECONDS = _env_int("STAGNANT_GRID_TIMEOUT_SECONDS", 3600)
LOSING_STAGNANT_TIMEOUT_SECONDS = _env_int("LOSING_STAGNANT_TIMEOUT_SECONDS", 7200)

# --- Hard Floor (ATR-bucketed stop loss) ---
HARD_FLOOR_BASE_PCT = _env_float("HARD_FLOOR_BASE_PCT", 25.0)
HARD_FLOOR_MIN_PCT = _env_float("HARD_FLOOR_MIN_PCT", 20.0)
HARD_FLOOR_MAX_PCT = _env_float("HARD_FLOOR_MAX_PCT", 35.0)

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Exchange URLs ---
if EXCHANGE == "bybit":
    if TRADING_MODE == "testnet":
        BYBIT_REST_URL = "https://api-testnet.bybit.com"
        BYBIT_WS_PUBLIC = "wss://stream-testnet.bybit.com/v5/public/linear"
        BYBIT_WS_PRIVATE = "wss://stream-testnet.bybit.com/v5/private"
    else:
        BYBIT_REST_URL = "https://api.bybit.com"
        BYBIT_WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"
        BYBIT_WS_PRIVATE = "wss://stream.bybit.com/v5/private"
elif EXCHANGE == "binance":
    if TRADING_MODE == "testnet":
        BINANCE_REST_URL = "https://testnet.binancefuture.com"
        BYBIT_WS_PUBLIC = "wss://stream.binancefuture.com/ws"
        BYBIT_WS_PRIVATE = "wss://stream.binancefuture.com/ws"
    else:
        BINANCE_REST_URL = "https://fapi.binance.com"
        BYBIT_WS_PUBLIC = "wss://fstream.binance.com/ws"
        BYBIT_WS_PRIVATE = "wss://fstream.binance.com/ws"
