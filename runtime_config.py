"""
Runtime config + secret overlay for grid-trader.

Bot processes (multi_grid_manager, grid_api) import this module *first*,
before any other module that reads `os.getenv` at import time. The overlay
loads two files from /data:

  runtime_config.json   — plaintext tunable overrides (thresholds, sizing)
  runtime_secrets.bin   — Fernet-encrypted API keys and tokens

Both files are written by admin.py via authenticated UI requests. They
*overlay* values onto os.environ, so all downstream `os.getenv(...)` calls
transparently see the user-supplied values without code changes.

Threat model:
  - The data volume is on the host filesystem. Anyone with shell access to
    the VPS can read runtime_config.json. Treat tunables as non-sensitive.
  - runtime_secrets.bin is encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
    using a key derived from ADMIN_PASSWORD via PBKDF2-HMAC-SHA256 with a
    randomly generated, persisted salt and 480_000 iterations. An attacker
    with the file but not the password cannot recover the plaintext keys.
  - If ADMIN_PASSWORD is rotated, existing secrets become unreadable. The
    user re-enters them through the UI, which is acceptable for a personal
    deployment.

Fields exposed:
  CONFIG_SCHEMA below is the source of truth. UI reads it to render the
  form; admin.py validates writes against it. To add a new tunable, add
  it here AND make sure the consuming module reads it via os.getenv.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("runtime_config")

DATA_DIR = Path(os.getenv("GRID_TRADER_DATA_DIR", "/data"))
RUNTIME_CONFIG_PATH = DATA_DIR / "runtime_config.json"
RUNTIME_SECRETS_PATH = DATA_DIR / "runtime_secrets.bin"
RUNTIME_SALT_PATH = DATA_DIR / "runtime_secrets.salt"

# ── Schema ──────────────────────────────────────────────────────────────
# Each entry: env-var name -> {type, default, min?, max?, category, label, help}.
# `default` is informational (the env-var default in code is authoritative);
# we surface it so the UI can show the baseline.

@dataclass
class FieldSpec:
    type: str             # "int" | "float" | "bool" | "str"
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None
    category: str = "general"
    label: str = ""
    help: str = ""
    secret: bool = False  # if true, never exposed in /api/admin/settings

    def as_dict(self) -> dict:
        d = {"type": self.type, "default": self.default, "category": self.category,
             "label": self.label, "help": self.help, "secret": self.secret}
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        return d


CONFIG_SCHEMA: dict[str, FieldSpec] = {
    # ── Sizing ──────────────────────────────────────────────────────────
    "BASE_ORDER_SIZE_USDT": FieldSpec(
        type="float", default=10.0, min=0.1, max=1000.0,
        category="sizing", label="Base order size (USDT)",
        help="Notional per grid level. With leverage=50 and size=$10, margin per fill is $0.20."),
    "MAX_CONCURRENT_GRIDS": FieldSpec(
        type="int", default=50, min=1, max=200,
        category="sizing", label="Max concurrent grids",
        help="Cap on simultaneously active grid slots."),
    "DEFAULT_LEVERAGE": FieldSpec(
        type="int", default=25, min=1, max=125,
        category="sizing", label="Default leverage",
        help="Per-symbol leverage when not overridden by token profile."),
    "TARGET_WALLET_EXPOSURE_PCT": FieldSpec(
        type="float", default=80.0, min=10.0, max=100.0,
        category="sizing", label="Target wallet exposure (%)",
        help="Soft cap on combined position margin as % of wallet."),

    # ── Hard loss floor ──────────────────────────────────────────────────
    "HARD_FLOOR_BASE_PCT": FieldSpec(
        type="float", default=15.0, min=5.0, max=50.0,
        category="floor", label="Hard floor base (%)",
        help="Margin-loss % at which scale-out fires (ATR-bucketed around this)."),
    "HARD_FLOOR_MIN_PCT": FieldSpec(
        type="float", default=12.0, min=5.0, max=50.0,
        category="floor", label="Hard floor min (%)",
        help="Lower bucket clamp for low-ATR symbols."),
    "HARD_FLOOR_MAX_PCT": FieldSpec(
        type="float", default=22.0, min=5.0, max=50.0,
        category="floor", label="Hard floor max (%)",
        help="Upper bucket clamp for high-ATR symbols."),
    "SCALE_OUT_FRACTION": FieldSpec(
        type="float", default=0.5, min=0.1, max=1.0,
        category="floor", label="Scale-out fraction",
        help="Fraction of position closed on first floor breach. 0.5 = halve."),

    # ── Imbalance bypass guards ──────────────────────────────────────────
    "IMBALANCE_EMERGENCY_RATIO": FieldSpec(
        type="float", default=4.0, min=1.5, max=20.0,
        category="imbalance", label="Bypass: ratio threshold",
        help="One-side:other-side fill ratio that arms the bypass."),
    "IMBALANCE_EMERGENCY_MIN_FILLS": FieldSpec(
        type="int", default=4, min=2, max=20,
        category="imbalance", label="Bypass: min fills",
        help="Minimum fills before bypass can fire."),
    "IMBALANCE_EMERGENCY_MIN_LOSS_PCT": FieldSpec(
        type="float", default=8.0, min=1.0, max=30.0,
        category="imbalance", label="Bypass: min loss (% margin)",
        help="Below this, bypass defers to freeze + recovery window."),
    "IMBALANCE_EMERGENCY_MIN_AGE_SEC": FieldSpec(
        type="float", default=60.0, min=0.0, max=600.0,
        category="imbalance", label="Bypass: min age (s)",
        help="Position must be at least this old; suppresses candle-event triggers."),

    # ── Soft blacklist ───────────────────────────────────────────────────
    "IMBALANCE_SOFT_BLACKLIST_THRESHOLD": FieldSpec(
        type="int", default=2, min=1, max=10,
        category="blacklist", label="Imbalance closes to cool",
        help="N grid_imbalance closes within window triggers cooldown."),
    "IMBALANCE_SOFT_BLACKLIST_WINDOW_SEC": FieldSpec(
        type="int", default=3600, min=300, max=86400,
        category="blacklist", label="Window (s)",
        help="Rolling window over which imbalance closes are counted."),
    "IMBALANCE_SOFT_BLACKLIST_COOLDOWN_SEC": FieldSpec(
        type="int", default=7200, min=300, max=86400,
        category="blacklist", label="Cooldown (s)",
        help="How long the symbol is sidelined after the threshold trips."),

    # ── Cluster gate ─────────────────────────────────────────────────────
    "DRAWDOWN_CLUSTER_WINDOW_SEC": FieldSpec(
        type="int", default=300, min=60, max=3600,
        category="cluster", label="Cluster window (s)"),
    "DRAWDOWN_CLUSTER_THRESHOLD": FieldSpec(
        type="int", default=3, min=2, max=20,
        category="cluster", label="Cluster threshold"),
    "DRAWDOWN_CLUSTER_PAUSE_SEC": FieldSpec(
        type="int", default=600, min=60, max=7200,
        category="cluster", label="Cluster pause (s)"),

    # ── Spike detection ──────────────────────────────────────────────────
    "SPIKE_WINDOW_SEC": FieldSpec(
        type="float", default=10.0, min=1.0, max=120.0,
        category="spike", label="Spike window (s)"),
    "SPIKE_THRESHOLD_PCT": FieldSpec(
        type="float", default=0.5, min=0.1, max=5.0,
        category="spike", label="Spike threshold (% move)"),
    "SPIKE_COOLDOWN_SEC": FieldSpec(
        type="float", default=60.0, min=5.0, max=600.0,
        category="spike", label="Spike cooldown (s)"),

    # ── Mode toggle (high-impact, restart required) ──────────────────────
    "DRY_RUN": FieldSpec(
        type="bool", default=True,
        category="mode", label="Dry-run mode",
        help="When false, the engine places real orders via CCXT. Restart required."),

    # ── Secrets (write-only via admin/secrets endpoint) ─────────────────
    "BYBIT_API_KEY": FieldSpec(
        type="str", default="", category="secrets",
        label="Bybit API key", secret=True),
    "BYBIT_API_SECRET": FieldSpec(
        type="str", default="", category="secrets",
        label="Bybit API secret", secret=True),
    "TELEGRAM_BOT_TOKEN": FieldSpec(
        type="str", default="", category="secrets",
        label="Telegram bot token", secret=True),
    "TELEGRAM_CHAT_ID": FieldSpec(
        type="str", default="", category="secrets",
        label="Telegram chat ID", secret=True),
}

# Public constant: tunables (non-secret) and secrets.
TUNABLE_FIELDS = {k: v for k, v in CONFIG_SCHEMA.items() if not v.secret}
SECRET_FIELDS = {k: v for k, v in CONFIG_SCHEMA.items() if v.secret}


# ── File I/O ─────────────────────────────────────────────────────────────

def _load_runtime_config() -> dict[str, Any]:
    if not RUNTIME_CONFIG_PATH.exists():
        return {}
    try:
        raw = json.loads(RUNTIME_CONFIG_PATH.read_text())
        return raw.get("values", {}) if isinstance(raw, dict) else {}
    except Exception as exc:
        logger.warning(f"Could not read {RUNTIME_CONFIG_PATH}: {exc}")
        return {}


def _coerce(value: Any, spec: FieldSpec) -> str:
    """Coerce a JSON value to the str form expected by os.environ."""
    if spec.type == "bool":
        # os.getenv("FOO", "false") style — accept many truthy spellings
        return "true" if (value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}) else "false"
    return str(value)


def _derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256, 480_000 iters, 32-byte output → urlsafe-b64 for Fernet."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _get_or_make_salt() -> bytes:
    if RUNTIME_SALT_PATH.exists():
        try:
            return RUNTIME_SALT_PATH.read_bytes()
        except Exception as exc:
            logger.warning(f"Could not read {RUNTIME_SALT_PATH}: {exc}")
    salt = os.urandom(16)
    try:
        RUNTIME_SALT_PATH.write_bytes(salt)
        os.chmod(RUNTIME_SALT_PATH, 0o600)
    except Exception as exc:
        logger.warning(f"Could not write salt: {exc}")
    return salt


def _decrypt_secrets(password: str) -> dict[str, str]:
    """Decrypt runtime_secrets.bin with the password-derived key. Returns {} on any failure."""
    if not RUNTIME_SECRETS_PATH.exists():
        return {}
    if not password:
        logger.warning("Cannot decrypt secrets: ADMIN_PASSWORD not set")
        return {}
    try:
        from cryptography.fernet import Fernet, InvalidToken
        salt = _get_or_make_salt()
        key = _derive_key(password, salt)
        token = RUNTIME_SECRETS_PATH.read_bytes()
        plaintext = Fernet(key).decrypt(token)
        data = json.loads(plaintext.decode("utf-8"))
        return data.get("values", {}) if isinstance(data, dict) else {}
    except Exception as exc:
        # Catch InvalidToken plus anything else — never leak crypto details.
        logger.warning(f"Could not decrypt secrets: {type(exc).__name__}")
        return {}


def encrypt_secrets(values: dict[str, str], password: str) -> bytes:
    """Encrypt a dict of secrets for write to disk. Caller persists the bytes."""
    if not password:
        raise ValueError("ADMIN_PASSWORD not set")
    from cryptography.fernet import Fernet
    salt = _get_or_make_salt()
    key = _derive_key(password, salt)
    payload = json.dumps({"values": values}).encode("utf-8")
    return Fernet(key).encrypt(payload)


# ── The overlay ──────────────────────────────────────────────────────────

def apply_overlay() -> dict[str, str]:
    """
    Overlay runtime_config + decrypted secrets onto os.environ.
    Logs which keys were applied (without values).
    Returns the effective overlay (keys only) for diagnostics.
    """
    applied: dict[str, str] = {}

    # 1. Tunables (plaintext)
    overrides = _load_runtime_config()
    for key, value in overrides.items():
        spec = CONFIG_SCHEMA.get(key)
        if not spec or spec.secret:
            continue
        try:
            os.environ[key] = _coerce(value, spec)
            applied[key] = "tunable"
        except Exception as exc:
            logger.warning(f"Skipping {key}: {exc}")

    # 2. Secrets (encrypted)
    password = os.environ.get("ADMIN_PASSWORD", "")
    if password:
        secrets = _decrypt_secrets(password)
        for key, value in secrets.items():
            if not value:
                continue
            if CONFIG_SCHEMA.get(key) and CONFIG_SCHEMA[key].secret:
                os.environ[key] = str(value)
                applied[key] = "secret"

    if applied:
        logger.info(f"Runtime overlay applied: {len(applied)} keys "
                    f"({sum(1 for v in applied.values() if v == 'tunable')} tunables, "
                    f"{sum(1 for v in applied.values() if v == 'secret')} secrets)")
    return applied


# Apply immediately on import. Importers must put this at the very top,
# before any module that reads os.getenv at import time.
apply_overlay()
