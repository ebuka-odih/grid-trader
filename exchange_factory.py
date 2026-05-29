# ============================================================
# Exchange Factory — unified ccxt adapter for Bybit + Binance
# ============================================================
"""Single entry point for creating exchange instances.

Import this instead of hardcoding ccxt.bybit() / ccxt.binance().
Reads EXCHANGE, TRADING_MODE, and the appropriate API keys from env.
"""

import os
import logging
import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)

# Keys — loaded from env after runtime overlay has applied
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TRADING_MODE = os.getenv("TRADING_MODE", "testnet")
EXCHANGE = os.getenv("EXCHANGE", "bybit")  # "bybit" | "binance"

# ── Bybit URLs ──────────────────────────────────────────────
BYBIT_TESTNET_REST = "https://api-testnet.bybit.com"
BYBIT_MAINNET_REST = "https://api.bybit.com"

# ── Binance URLs ────────────────────────────────────────────
BINANCE_TESTNET_REST = "https://testnet.binancefuture.com"
BINANCE_MAINNET_REST = "https://api.binance.com"


def _create_bybit() -> ccxt.Exchange:
    """Create a Bybit linear perpetuals exchange."""
    opts = {
        "apiKey": BYBIT_API_KEY,
        "secret": BYBIT_API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "linear"},
    }
    if TRADING_MODE == "testnet":
        opts["urls"] = {
            "api": {
                "public": BYBIT_TESTNET_REST,
                "private": BYBIT_TESTNET_REST,
            }
        }
    logger.info(f"Bybit exchange created (mode={TRADING_MODE})")
    return ccxt.bybit(opts)


def _create_binance() -> ccxt.Exchange:
    """Create a Binance Futures (USDⓈ-M) exchange."""
    opts = {
        "apiKey": BINANCE_API_KEY,
        "secret": BINANCE_API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    }
    if TRADING_MODE == "testnet":
        opts["urls"] = {
            "api": {
                "public": BINANCE_TESTNET_REST,
                "private": BINANCE_TESTNET_REST,
            }
        }
    logger.info(f"Binance Futures exchange created (mode={TRADING_MODE})")
    return ccxt.binance(opts)


_factories = {
    "bybit": _create_bybit,
    "binance": _create_binance,
}


def create_exchange() -> ccxt.Exchange:
    """Create a ccxt exchange instance based on the EXCHANGE env var.

    Returns:
        A configured ccxt.async_support exchange instance.

    Raises:
        ValueError: if EXCHANGE is not a known exchange.
    """
    exchange_name = EXCHANGE.lower()
    factory = _factories.get(exchange_name)
    if not factory:
        supported = ", ".join(sorted(_factories))
        raise ValueError(
            f"Unknown exchange '{EXCHANGE}'. "
            f"Set EXCHANGE to one of: {supported}"
        )
    return factory()


def get_exchange_name() -> str:
    """Return the active exchange name (for logging/UI)."""
    return EXCHANGE.lower()


def has_api_keys() -> bool:
    """Check if API keys are configured for the active exchange."""
    if EXCHANGE.lower() == "binance":
        key = os.getenv("BINANCE_API_KEY", "").strip()
        secret = os.getenv("BINANCE_API_SECRET", "").strip()
        return bool(key and key != "your_api_key_here" and secret and secret != "your_secret_here")
    else:
        key = os.getenv("BYBIT_API_KEY", "").strip()
        secret = os.getenv("BYBIT_API_SECRET", "").strip()
        return bool(key and key != "your_api_key_here" and secret and secret != "your_secret_here")


def get_exchange_label() -> str:
    """Return a human-readable label: 'Bybit Linear' or 'Binance Futures'."""
    labels = {"bybit": "Bybit Linear Testnet" if TRADING_MODE == "testnet" else "Bybit Linear",
              "binance": "Binance Futures Testnet" if TRADING_MODE == "testnet" else "Binance Futures"}
    return labels.get(EXCHANGE.lower(), EXCHANGE.upper())
