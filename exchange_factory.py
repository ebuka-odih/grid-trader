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

# ── Bybit URLs ──────────────────────────────────────────────
BYBIT_TESTNET_REST = "https://api-testnet.bybit.com"
BYBIT_MAINNET_REST = "https://api.bybit.com"

# ── Binance URLs ────────────────────────────────────────────
BINANCE_TESTNET_REST = "https://testnet.binancefuture.com"
BINANCE_MAINNET_REST = "https://api.binance.com"


def _create_bybit() -> ccxt.Exchange:
    """Create a Bybit linear perpetuals exchange."""
    key = os.getenv("BYBIT_API_KEY", "")
    secret = os.getenv("BYBIT_API_SECRET", "")
    trading_mode = os.getenv("TRADING_MODE", "testnet")
    opts: dict = {
        "apiKey": key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "linear"},
    }
    if trading_mode == "testnet":
        opts["urls"] = {
            "api": {
                "public": BYBIT_TESTNET_REST,
                "private": BYBIT_TESTNET_REST,
            }
        }
    logger.info(f"Bybit exchange created (mode={trading_mode})")
    return ccxt.bybit(opts)


def _create_binance() -> ccxt.Exchange:
    """Create a Binance Futures (USDⓈ-M) exchange."""
    key = os.getenv("BINANCE_API_KEY", "")
    secret = os.getenv("BINANCE_API_SECRET", "")
    trading_mode = os.getenv("TRADING_MODE", "testnet")
    opts: dict = {
        "apiKey": key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    }
    if trading_mode == "testnet":
        opts["urls"] = {
            "api": {
                "public": BINANCE_TESTNET_REST,
                "private": BINANCE_TESTNET_REST,
            }
        }
    logger.info(f"Binance Futures exchange created (mode={trading_mode})")
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
    exchange_name = os.getenv("EXCHANGE", "bybit").lower()
    factory = _factories.get(exchange_name)
    if not factory:
        supported = ", ".join(sorted(_factories))
        raise ValueError(
            f"Unknown exchange '{exchange_name}'. "
            f"Set EXCHANGE to one of: {supported}"
        )
    return factory()


def get_exchange_name() -> str:
    """Return the active exchange name (for logging/UI)."""
    return os.getenv("EXCHANGE", "bybit").lower()


def has_api_keys() -> bool:
    """Check if API keys are configured for the active exchange."""
    exchange = os.getenv("EXCHANGE", "bybit").lower()
    if exchange == "binance":
        key = os.getenv("BINANCE_API_KEY", "").strip()
        secret = os.getenv("BINANCE_API_SECRET", "").strip()
        return bool(key and key != "your_api_key_here" and secret and secret != "your_secret_here")
    else:
        key = os.getenv("BYBIT_API_KEY", "").strip()
        secret = os.getenv("BYBIT_API_SECRET", "").strip()
        return bool(key and key != "your_api_key_here" and secret and secret != "your_secret_here")


def get_exchange_label() -> str:
    """Return a human-readable label: 'Bybit Linear' or 'Binance Futures'."""
    trading_mode = os.getenv("TRADING_MODE", "testnet")
    exchange = os.getenv("EXCHANGE", "bybit").lower()
    labels = {"bybit": "Bybit Linear Testnet" if trading_mode == "testnet" else "Bybit Linear",
              "binance": "Binance Futures Testnet" if trading_mode == "testnet" else "Binance Futures"}
    return labels.get(exchange, exchange.upper())
