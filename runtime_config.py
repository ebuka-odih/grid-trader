"""
Runtime config + secret overlay for grid-trader.

Bot processes (multi_grid_manager, grid_api) import this module *first*,
before any other module that reads `os.getenv` at import time. The overlay
loads three files from /data:

  runtime_config.json   — plaintext tunable overrides (thresholds, sizing)
  runtime_secrets.bin   — Fernet-encrypted API keys and tokens
  secrets.key           — random 32-byte master key (auto-generated)

Both data files are written by admin.py via authenticated UI requests. They
*overlay* values onto os.environ, so all downstream `os.getenv(...)` calls
transparently see the user-supplied values without code changes.

Threat model:
  - The data volume is on the host filesystem. Anyone with shell access to
    the VPS can read runtime_config.json. Treat tunables as non-sensitive.
  - runtime_secrets.bin is encrypted with Fernet (AES-128-CBC + HMAC-SHA256).
    The master key lives in /data/secrets.key (chmod 600), generated once
    on first boot. The admin login password is intentionally NOT involved
    in encryption: it lives in .env as a bcrypt hash for login auth only,
    so password rotation no longer locks the bot out of saved secrets.
  - If both /data/secrets.key and runtime_secrets.bin leak together, the
    saved API keys are exposed. Defense is filesystem isolation (chmod
    600, the secrets.key is not in .env or any backup of .env).
  - If only the .env file leaks, the bcrypt hash protects the login
    password from direct recovery (must be cracked offline).

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
# Master encryption key for runtime_secrets.bin. Decoupled from the admin
# login password so the password can be hashed at rest. Generated on first
# boot, persisted to disk with chmod 600. If this file is lost, saved
# secrets become unreadable — re-enter them through the UI.
SECRETS_KEY_PATH = DATA_DIR / "secrets.key"

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
        type="int", default=30, min=1, max=125,
        category="sizing", label="Default leverage",
        help="Per-symbol leverage when not overridden by token profile."),
    "MIN_SAFE_LEVERAGE": FieldSpec(
        type="int", default=30, min=1, max=125,
        category="sizing", label="Min safe leverage",
        help="Lower clamp for allowed leverage values."),
    "MAX_SAFE_LEVERAGE": FieldSpec(
        type="int", default=50, min=1, max=125,
        category="sizing", label="Max safe leverage",
        help="Upper clamp for allowed leverage values."),
    "MIN_DEPLOY_LEVERAGE": FieldSpec(
        type="int", default=30, min=1, max=125,
        category="sizing", label="Min deploy leverage (legacy alias)",
        help="Legacy alias for MIN_SAFE_LEVERAGE; kept for backward-compatible overlays."),
    "MAX_DEPLOY_LEVERAGE": FieldSpec(
        type="int", default=50, min=1, max=125,
        category="sizing", label="Max deploy leverage (legacy alias)",
        help="Legacy alias for MAX_SAFE_LEVERAGE; kept for backward-compatible overlays."),
    "MAX_SCANNER_LEVERAGE": FieldSpec(
        type="int", default=50, min=1, max=125,
        category="sizing", label="Scanner max leverage",
        help="Caps leverage suggested by scanner decisions."),
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


def _load_master_key() -> Optional[bytes]:
    """Read /data/secrets.key, generating it once on first call if missing.

    Returns the 32-byte urlsafe-b64-encoded key suitable for Fernet, or
    None on any I/O failure (logs but does not raise — the bot must still
    boot even if the secrets infra is broken).
    """
    try:
        if SECRETS_KEY_PATH.exists():
            return SECRETS_KEY_PATH.read_bytes().strip()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = base64.urlsafe_b64encode(os.urandom(32))
        SECRETS_KEY_PATH.write_bytes(key)
        try:
            os.chmod(SECRETS_KEY_PATH, 0o600)
        except Exception:
            pass
        logger.info(f"Generated new secrets master key at {SECRETS_KEY_PATH}")
        return key
    except Exception as exc:
        logger.warning(f"Could not access master key at {SECRETS_KEY_PATH}: {exc}")
        return None


def _decrypt_secrets() -> dict[str, str]:
    """Decrypt runtime_secrets.bin using the master key. Returns {} on any failure."""
    if not RUNTIME_SECRETS_PATH.exists():
        return {}
    key = _load_master_key()
    if not key:
        return {}
    try:
        from cryptography.fernet import Fernet
        token = RUNTIME_SECRETS_PATH.read_bytes()
        plaintext = Fernet(key).decrypt(token)
        data = json.loads(plaintext.decode("utf-8"))
        return data.get("values", {}) if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning(f"Could not decrypt secrets: {type(exc).__name__}")
        return {}


def encrypt_secrets(values: dict[str, str]) -> bytes:
    """Encrypt a dict of secrets for write to disk using the master key."""
    key = _load_master_key()
    if not key:
        raise RuntimeError("Master key unavailable; cannot encrypt secrets")
    from cryptography.fernet import Fernet
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

    # 2. Secrets (encrypted with master key — independent of admin password)
    secrets = _decrypt_secrets()
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

    # Startup safety: if the user toggled DRY_RUN=false but no API keys are
    # configured, force back to dry-run with a loud warning. Prevents the
    # crash-loop where the live engine fails at startup, the manager exits,
    # docker-compose restarts, and the same crash repeats every minute.
    dry_run = (os.environ.get("DRY_RUN", "true").strip().lower()
               in {"1", "true", "yes", "on"})
    if not dry_run:
        bybit_key = os.environ.get("BYBIT_API_KEY", "").strip()
        bybit_secret = os.environ.get("BYBIT_API_SECRET", "").strip()
        if not bybit_key or not bybit_secret:
            logger.warning(
                "⚠️  DRY_RUN=false but BYBIT_API_KEY/SECRET are not set. "
                "Forcing DRY_RUN=true to avoid live-engine startup failure. "
                "Set keys via the admin UI (Settings → API Keys), then retry."
            )
            os.environ["DRY_RUN"] = "true"
            applied["DRY_RUN"] = "fallback"

    return applied


# Apply immediately on import. Importers must put this at the very top,
# before any module that reads os.getenv at import time.
apply_overlay()
