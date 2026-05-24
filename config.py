# ============================================================
# Bybit Agentic Grid Trader — v2 Cross-Margin Config
# ============================================================

import os
from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


# --- Bybit API ---
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TRADING_MODE = os.getenv("TRADING_MODE", "testnet")

# --- Cross-Margin Mode ---
# v2: All positions share one wallet — cross margin
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "cross")  # "cross" or "isolated"
# --- Aggressive dry-run config (override with env vars for live) ---
# $100 wallet, $10/order, 30-50x leverage, 10 concurrent grids
INITIAL_WALLET_BALANCE = _env_float("INITIAL_WALLET_BALANCE", 100.0)
BASE_ORDER_SIZE_USDT = _env_float("BASE_ORDER_SIZE_USDT", 10.0)
# Allow aggressive portfolio exposure for dry-run testing
MAX_TOTAL_WALLET_EXPOSURE_PCT = _env_float("MAX_TOTAL_WALLET_EXPOSURE_PCT", 60)
TOKEN_PROFILES_PATH = os.getenv("TOKEN_PROFILES_PATH", "token_profiles.json")

# --- Grid Strategy (defaults, overridden by token_profiles.json) ---
# High-frequency cross-margin policy: keep leverage at extreme for dry-run
# speed testing. Wallet risk is managed through reserved margin size, not leverage.
# 100x is Bybit's max — the exchange caps per symbol below that if unsupported.
#
# Single source of truth for leverage policy:
# - Global operating band comes from MIN_SAFE_LEVERAGE / MAX_SAFE_LEVERAGE.
# - Legacy MIN/MAX_DEPLOY_LEVERAGE env vars are accepted as aliases so older
#   runtime_config.json overlays still work, but the rest of the code should
#   read the canonical values/helpers below instead of re-reading env vars.
_RAW_MIN_SAFE_LEVERAGE = _env_int("MIN_SAFE_LEVERAGE", _env_int("MIN_DEPLOY_LEVERAGE", 30))
_RAW_MAX_SAFE_LEVERAGE = _env_int("MAX_SAFE_LEVERAGE", _env_int("MAX_DEPLOY_LEVERAGE", 100))
if _RAW_MAX_SAFE_LEVERAGE < _RAW_MIN_SAFE_LEVERAGE:
    _RAW_MAX_SAFE_LEVERAGE = _RAW_MIN_SAFE_LEVERAGE

MIN_SAFE_LEVERAGE = _RAW_MIN_SAFE_LEVERAGE
MAX_SAFE_LEVERAGE = _RAW_MAX_SAFE_LEVERAGE

# Backward-compatible aliases: other modules should prefer MIN/MAX_SAFE_LEVERAGE.
MIN_DEPLOY_LEVERAGE = MIN_SAFE_LEVERAGE
MAX_DEPLOY_LEVERAGE = MAX_SAFE_LEVERAGE


def clamp_leverage(value: int | float | str | None, *, minimum: int | None = None, maximum: int | None = None) -> int:
    min_allowed = MIN_SAFE_LEVERAGE if minimum is None else int(minimum)
    max_allowed = MAX_SAFE_LEVERAGE if maximum is None else int(maximum)
    if max_allowed < min_allowed:
        max_allowed = min_allowed
    try:
        raw = int(float(value))
    except (TypeError, ValueError):
        raw = DEFAULT_LEVERAGE if "DEFAULT_LEVERAGE" in globals() else min_allowed
    return max(min_allowed, min(max_allowed, raw))


DEFAULT_LEVERAGE = clamp_leverage(_env_int("DEFAULT_LEVERAGE", 100))
DEFAULT_NUM_GRIDS = _env_int("DEFAULT_NUM_GRIDS", 10)
MAX_TRADE_WALLET_EXPOSURE_PCT = _env_float("MAX_TRADE_WALLET_EXPOSURE_PCT", 10.0)
MIN_ORDER_SIZE_USDT = _env_float("MIN_ORDER_SIZE_USDT", 0.1)
# Static dollar targets are retained only as a safety fallback.
# Fast-grid exits should use percentage targets against margin allocated.
TARGET_PNL_LOW = _env_float("TARGET_PNL_LOW", 1.0)
TARGET_PNL_HIGH = _env_float("TARGET_PNL_HIGH", 2.0)
TARGET_PNL_PCT_LOW = _env_float("TARGET_PNL_PCT_LOW", 2.0)
TARGET_PNL_PCT_HIGH = _env_float("TARGET_PNL_PCT_HIGH", 4.0)
MAX_DRAWDOWN_PCT = _env_float("MAX_DRAWDOWN_PCT", 5.0)
BASE_ORDER_SIZE_USDT = _env_float("BASE_ORDER_SIZE_USDT", 10.0)
MAX_SCANNER_LEVERAGE = clamp_leverage(_env_int("MAX_SCANNER_LEVERAGE", MAX_SAFE_LEVERAGE))


def resolve_profile_max_leverage(profile: dict | None = None) -> int:
    profile = profile or {}
    return clamp_leverage(profile.get("max_leverage", MAX_SAFE_LEVERAGE))


def resolve_profile_leverage(profile: dict | None = None, fallback: int | None = None) -> int:
    profile = profile or {}
    preferred = profile.get("leverage", DEFAULT_LEVERAGE if fallback is None else fallback)
    return clamp_leverage(preferred, maximum=resolve_profile_max_leverage(profile))


# --- Volatility-Scaled Sizing ---
# Scale order size based on ATR: higher ATR = smaller orders
VOLATILITY_SCALE_ENABLED = os.getenv("VOLATILITY_SCALE_ENABLED", "true").lower() == "true"
VOLATILITY_SCALE_BASE_ATR = _env_float("VOLATILITY_SCALE_BASE_ATR", 1.5)  # 1.5% ATR = baseline
VOLATILITY_SCALE_MIN_FACTOR = _env_float("VOLATILITY_SCALE_MIN_FACTOR", 0.3)  # min 30% of base size
VOLATILITY_SCALE_MAX_FACTOR = _env_float("VOLATILITY_SCALE_MAX_FACTOR", 2.0)  # max 200% of base size

# --- Coin Scanner ---
SCAN_INTERVAL_SECONDS = _env_int("SCAN_INTERVAL_SECONDS", 300)
MIN_24H_VOLUME_USDT = _env_float("MIN_24H_VOLUME_USDT", 2000000)
SCAN_TOP_N_COINS = _env_int("SCAN_TOP_N_COINS", 80)
SCANNER_SYMBOL_DELAY_MS = _env_int("SCANNER_SYMBOL_DELAY_MS", 250)
SCANNER_RATE_LIMIT_RETRIES = _env_int("SCANNER_RATE_LIMIT_RETRIES", 3)
SCANNER_RATE_LIMIT_BACKOFF_SECONDS = _env_float("SCANNER_RATE_LIMIT_BACKOFF_SECONDS", 2.0)

# --- Safety Filters (global, can be tightened per-token in profiles) ---
MAX_24H_RANGE_PCT = _env_float("MAX_24H_RANGE_PCT", 25.0)
MIN_24H_RANGE_PCT = _env_float("MIN_24H_RANGE_PCT", 1.0)
MAX_ATR_PCT = _env_float("MAX_ATR_PCT", 3.0)
MIN_MEAN_REVERSION = _env_float("MIN_MEAN_REVERSION", 0.3)

# --- Blacklist (now loaded from token_profiles.json, this is fallback) ---
COIN_BLACKLIST = os.getenv("COIN_BLACKLIST", "")  # No blacklist — all coins tradeable

# --- Portfolio Risk (cross-margin) ---
MAX_TOTAL_WALLET_EXPOSURE_PCT = _env_float("MAX_TOTAL_WALLET_EXPOSURE_PCT", 60)
MAX_SINGLE_DIRECTION_EXPOSURE_PCT = _env_float("MAX_SINGLE_DIRECTION_EXPOSURE_PCT", 50)
MAX_TRADE_WALLET_EXPOSURE_PCT = _env_float("MAX_TRADE_WALLET_EXPOSURE_PCT", 5.0)
PORTFOLIO_RESERVE_PCT = _env_float("PORTFOLIO_RESERVE_PCT", 20)
EMERGENCY_LIQUIDATION_BUFFER_PCT = _env_float("EMERGENCY_LIQUIDATION_BUFFER_PCT", 10)
RISK_CHECK_INTERVAL_SECONDS = _env_int("RISK_CHECK_INTERVAL_SECONDS", 30)

# --- Dry Run ---
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# --- Trading Agent (LLM) ---
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
TRADING_AGENT_MODEL = os.getenv("TRADING_AGENT_MODEL", "meta/llama-3.3-70b-instruct")
TRADING_AGENT_FALLBACK_MODEL = os.getenv("TRADING_AGENT_FALLBACK_MODEL", "meta/llama-3.1-8b-instruct")
AGENT_MID_TRADE_INTERVAL = _env_int("AGENT_MID_TRADE_INTERVAL", 120)

# --- Multi-Grid Mode ---
MAX_CONCURRENT_GRIDS = _env_int("MAX_CONCURRENT_GRIDS", 50)
MID_TRADE_CHECK_INTERVAL = _env_int("MID_TRADE_CHECK_INTERVAL", 120)
GRID_MONITOR_TIMEOUT = _env_int("GRID_MONITOR_TIMEOUT", 1800)
SCANNER_TOP_N_PORTFOLIO = _env_int("SCANNER_TOP_N_PORTFOLIO", 30)
MIN_FREE_SLOTS_TO_SCAN = _env_int("MIN_FREE_SLOTS_TO_SCAN", 3)
MAX_DEPLOYMENTS_PER_CYCLE = _env_int("MAX_DEPLOYMENTS_PER_CYCLE", 15)

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Bybit URLs ---
if TRADING_MODE == "testnet":
    BYBIT_REST_URL = "https://api-testnet.bybit.com"
    BYBIT_WS_PUBLIC = "wss://stream-testnet.bybit.com/v5/public/linear"
    BYBIT_WS_PRIVATE = "wss://stream-testnet.bybit.com/v5/private"
else:
    BYBIT_REST_URL = "https://api.bybit.com"
    BYBIT_WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"
    BYBIT_WS_PRIVATE = "wss://stream.bybit.com/v5/private"
