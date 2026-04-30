# ============================================================
# Bybit Agentic Grid Trader — v2 Cross-Margin Config
# ============================================================

import os
from dotenv import load_dotenv

load_dotenv()

# --- Bybit API ---
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TRADING_MODE = os.getenv("TRADING_MODE", "testnet")

# --- Cross-Margin Mode ---
# v2: All positions share one wallet — cross margin
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "cross")  # "cross" or "isolated"
# --- Aggressive dry-run config (override with env vars for live) ---
# $100 wallet, $10/order, 50x leverage, 10 concurrent grids
INITIAL_WALLET_BALANCE = float(os.getenv("INITIAL_WALLET_BALANCE", "100.0"))
BASE_ORDER_SIZE_USDT = float(os.getenv("BASE_ORDER_SIZE_USDT", "10.0"))
MAX_CONCURRENT_GRIDS = int(os.getenv("MAX_CONCURRENT_GRIDS", "10"))
# Allow aggressive portfolio exposure for dry-run testing
MAX_TOTAL_WALLET_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_WALLET_EXPOSURE_PCT", "95"))
TOKEN_PROFILES_PATH = os.getenv("TOKEN_PROFILES_PATH", "token_profiles.json")

# --- Grid Strategy (defaults, overridden by token_profiles.json) ---
# High-frequency cross-margin policy: keep leverage high for fast movement while
# limiting wallet risk through reserved margin size, not low leverage.
MIN_SAFE_LEVERAGE = int(os.getenv("MIN_SAFE_LEVERAGE", "15"))
MAX_SAFE_LEVERAGE = int(os.getenv("MAX_SAFE_LEVERAGE", "100"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "50"))
DEFAULT_LEVERAGE = max(MIN_SAFE_LEVERAGE, min(MAX_SAFE_LEVERAGE, DEFAULT_LEVERAGE))
DEFAULT_NUM_GRIDS = int(os.getenv("DEFAULT_NUM_GRIDS", "10"))
MAX_TRADE_WALLET_EXPOSURE_PCT = float(os.getenv("MAX_TRADE_WALLET_EXPOSURE_PCT", "2.0"))
MIN_ORDER_SIZE_USDT = float(os.getenv("MIN_ORDER_SIZE_USDT", "0.1"))
# Static dollar targets are retained only as a safety fallback.
# Fast-grid exits should use percentage targets against margin allocated.
TARGET_PNL_LOW = float(os.getenv("TARGET_PNL_LOW", "1.0"))
TARGET_PNL_HIGH = float(os.getenv("TARGET_PNL_HIGH", "2.0"))
TARGET_PNL_PCT_LOW = float(os.getenv("TARGET_PNL_PCT_LOW", "2.0"))
TARGET_PNL_PCT_HIGH = float(os.getenv("TARGET_PNL_PCT_HIGH", "4.0"))
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "5.0"))
BASE_ORDER_SIZE_USDT = float(os.getenv("BASE_ORDER_SIZE_USDT", "10.0"))
MAX_SCANNER_LEVERAGE = int(os.getenv("MAX_SCANNER_LEVERAGE", str(MAX_SAFE_LEVERAGE)))

# --- Volatility-Scaled Sizing ---
# Scale order size based on ATR: higher ATR = smaller orders
VOLATILITY_SCALE_ENABLED = os.getenv("VOLATILITY_SCALE_ENABLED", "true").lower() == "true"
VOLATILITY_SCALE_BASE_ATR = float(os.getenv("VOLATILITY_SCALE_BASE_ATR", "1.5"))  # 1.5% ATR = baseline
VOLATILITY_SCALE_MIN_FACTOR = float(os.getenv("VOLATILITY_SCALE_MIN_FACTOR", "0.3"))  # min 30% of base size
VOLATILITY_SCALE_MAX_FACTOR = float(os.getenv("VOLATILITY_SCALE_MAX_FACTOR", "2.0"))  # max 200% of base size

# --- Coin Scanner ---
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
MIN_24H_VOLUME_USDT = float(os.getenv("MIN_24H_VOLUME_USDT", "2000000"))
SCAN_TOP_N_COINS = int(os.getenv("SCAN_TOP_N_COINS", "80"))

# --- Safety Filters (global, can be tightened per-token in profiles) ---
MAX_24H_RANGE_PCT = float(os.getenv("MAX_24H_RANGE_PCT", "25.0"))
MIN_24H_RANGE_PCT = float(os.getenv("MIN_24H_RANGE_PCT", "1.0"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "3.0"))
MIN_MEAN_REVERSION = float(os.getenv("MIN_MEAN_REVERSION", "0.3"))

# --- Blacklist (now loaded from token_profiles.json, this is fallback) ---
COIN_BLACKLIST = os.getenv("COIN_BLACKLIST", "BTC,ETH,SOL,BNB,XRP,ADA,AVAX,LINK,DOT,LTC,BCH,TRX,TON,RAVE,GALA,PEOPLE,BLUR,ACE,NIL")

# --- Portfolio Risk (cross-margin) ---
MAX_TOTAL_WALLET_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_WALLET_EXPOSURE_PCT", "80"))
MAX_SINGLE_DIRECTION_EXPOSURE_PCT = float(os.getenv("MAX_SINGLE_DIRECTION_EXPOSURE_PCT", "50"))
MAX_TRADE_WALLET_EXPOSURE_PCT = float(os.getenv("MAX_TRADE_WALLET_EXPOSURE_PCT", str(MAX_TRADE_WALLET_EXPOSURE_PCT)))
PORTFOLIO_RESERVE_PCT = float(os.getenv("PORTFOLIO_RESERVE_PCT", "20"))
EMERGENCY_LIQUIDATION_BUFFER_PCT = float(os.getenv("EMERGENCY_LIQUIDATION_BUFFER_PCT", "10"))
RISK_CHECK_INTERVAL_SECONDS = int(os.getenv("RISK_CHECK_INTERVAL_SECONDS", "30"))

# --- Dry Run ---
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# --- Trading Agent (LLM) ---
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
TRADING_AGENT_MODEL = os.getenv("TRADING_AGENT_MODEL", "meta/llama-3.3-70b-instruct")
TRADING_AGENT_FALLBACK_MODEL = os.getenv("TRADING_AGENT_FALLBACK_MODEL", "meta/llama-3.1-8b-instruct")
AGENT_MID_TRADE_INTERVAL = int(os.getenv("AGENT_MID_TRADE_INTERVAL", "120"))

# --- Multi-Grid Mode ---
MAX_CONCURRENT_GRIDS = int(os.getenv("MAX_CONCURRENT_GRIDS", "45"))
MID_TRADE_CHECK_INTERVAL = int(os.getenv("MID_TRADE_CHECK_INTERVAL", "120"))
GRID_MONITOR_TIMEOUT = int(os.getenv("GRID_MONITOR_TIMEOUT", "1800"))
SCANNER_TOP_N_PORTFOLIO = int(os.getenv("SCANNER_TOP_N_PORTFOLIO", "30"))
MIN_FREE_SLOTS_TO_SCAN = int(os.getenv("MIN_FREE_SLOTS_TO_SCAN", "3"))
MAX_DEPLOYMENTS_PER_CYCLE = int(os.getenv("MAX_DEPLOYMENTS_PER_CYCLE", "15"))

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
