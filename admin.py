"""
Admin API for grid-trader.

Authenticates against ADMIN_PASSWORD_HASH (bcrypt hash in .env), issues
bearer tokens with TTL, exposes endpoints to read tunables/schema and
write tunables + encrypted secrets, and supports an Apply (graceful
container exit so docker-compose restarts with the new config).

To bootstrap a password, run:
    python admin.py hash-password
and paste the printed line into .env, then restart the container.

Routes (all under /api/admin):
  POST /login            { password } -> { token, expires_at }
  GET  /settings         -> { schema, values, secret_status }
  PUT  /settings         { values } -> { ok, applied_keys }
  PUT  /secrets          { values } -> { ok, masked }
  POST /apply            -> kicks the manager exit; api stays up briefly
  GET  /status           -> { authed, has_admin_password, secret_keys_set }

Auth is a bearer token in the Authorization header. Tokens are stored in
process memory only (lost on restart, which is fine — the user logs in
again from the UI).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

import bcrypt

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from runtime_config import (
    CONFIG_SCHEMA,
    DATA_DIR,
    RUNTIME_CONFIG_PATH,
    RUNTIME_SECRETS_PATH,
    SECRET_FIELDS,
    TUNABLE_FIELDS,
    encrypt_secrets,
    _decrypt_secrets,
)

logger = logging.getLogger("admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Token lifetime: 8h. Stored in-memory; lost on restart by design.
TOKEN_TTL_SECONDS = 8 * 3600
_tokens: dict[str, float] = {}  # token -> expires_at


# ── Auth helpers ─────────────────────────────────────────────────────────

def _admin_password_hash() -> str:
    """Return the bcrypt hash from env. Empty string disables admin entirely."""
    return os.environ.get("ADMIN_PASSWORD_HASH", "") or ""


def _verify_password(provided: str) -> bool:
    """Verify a plaintext password against the stored bcrypt hash.

    bcrypt.checkpw is constant-time relative to the hash, defeating
    timing side-channels. Returns False if no hash is configured (admin
    disabled) or the hash is malformed.
    """
    expected = _admin_password_hash()
    if not expected or not provided:
        return False
    try:
        return bcrypt.checkpw(provided.encode("utf-8"), expected.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash (e.g. user pasted the plaintext password by mistake)
        return False


def _issue_token() -> tuple[str, float]:
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + TOKEN_TTL_SECONDS
    _tokens[token] = expires_at
    # Sweep expired tokens opportunistically
    now = time.time()
    for t, exp in list(_tokens.items()):
        if exp < now:
            _tokens.pop(t, None)
    return token, expires_at


def _require_token(authorization: Optional[str]) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization[len("Bearer "):].strip()
    expires_at = _tokens.get(token)
    if expires_at is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if expires_at < time.time():
        _tokens.pop(token, None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")


# ── Models ───────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class SecretsUpdate(BaseModel):
    values: dict[str, str]


# ── Status / login ───────────────────────────────────────────────────────

@router.get("/status")
def status_endpoint() -> dict:
    """Pre-login status. Tells the UI whether admin is even available
    (ADMIN_PASSWORD_HASH set?) and which secrets are currently configured."""
    has_hash = bool(_admin_password_hash())
    # Probe which secrets currently have values in env (post-overlay).
    secret_keys_set = []
    for key in SECRET_FIELDS:
        if os.environ.get(key):
            secret_keys_set.append(key)
    return {
        "has_admin_password": has_hash,  # name kept for UI compat
        "secret_keys_set": secret_keys_set,
    }


@router.post("/login")
def login_endpoint(req: LoginRequest) -> dict:
    if not _verify_password(req.password):
        # Generic message — don't disclose whether the password env var is set
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication failed")
    token, expires_at = _issue_token()
    return {"token": token, "expires_at": expires_at}


# ── Settings (tunables) ──────────────────────────────────────────────────

@router.get("/settings")
def get_settings(authorization: Optional[str] = Header(None)) -> dict:
    _require_token(authorization)
    schema = {key: spec.as_dict() for key, spec in TUNABLE_FIELDS.items()}
    # Effective values come from env (which has had the overlay applied).
    values: dict[str, Any] = {}
    for key, spec in TUNABLE_FIELDS.items():
        raw = os.environ.get(key)
        if raw is None:
            values[key] = spec.default
            continue
        if spec.type == "int":
            try: values[key] = int(float(raw))
            except: values[key] = spec.default
        elif spec.type == "float":
            try: values[key] = float(raw)
            except: values[key] = spec.default
        elif spec.type == "bool":
            values[key] = str(raw).strip().lower() in {"1", "true", "yes", "on"}
        else:
            values[key] = raw

    # Secret display: mask all but last 4 chars.
    secret_status: dict[str, dict] = {}
    for key, spec in SECRET_FIELDS.items():
        raw = os.environ.get(key, "")
        if raw and len(raw) > 4:
            secret_status[key] = {"set": True, "preview": "•" * 8 + raw[-4:]}
        elif raw:
            secret_status[key] = {"set": True, "preview": "••••"}
        else:
            secret_status[key] = {"set": False, "preview": ""}

    return {
        "schema": schema,
        "values": values,
        "secret_schema": {key: spec.as_dict() for key, spec in SECRET_FIELDS.items()},
        "secret_status": secret_status,
    }


def _validate_value(key: str, value: Any) -> Any:
    spec = TUNABLE_FIELDS.get(key)
    if not spec:
        raise HTTPException(status_code=400, detail=f"Unknown setting: {key}")
    if spec.type == "int":
        try:
            v = int(value)
        except Exception:
            raise HTTPException(status_code=400, detail=f"{key}: must be int")
        if spec.min is not None and v < spec.min:
            raise HTTPException(status_code=400, detail=f"{key}: must be >= {spec.min}")
        if spec.max is not None and v > spec.max:
            raise HTTPException(status_code=400, detail=f"{key}: must be <= {spec.max}")
        return v
    if spec.type == "float":
        try:
            v = float(value)
        except Exception:
            raise HTTPException(status_code=400, detail=f"{key}: must be number")
        if spec.min is not None and v < spec.min:
            raise HTTPException(status_code=400, detail=f"{key}: must be >= {spec.min}")
        if spec.max is not None and v > spec.max:
            raise HTTPException(status_code=400, detail=f"{key}: must be <= {spec.max}")
        return v
    if spec.type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    return str(value)


@router.put("/settings")
def put_settings(req: SettingsUpdate, authorization: Optional[str] = Header(None)) -> dict:
    _require_token(authorization)
    # Validate every key first; persist atomically (write tmp, then rename).
    cleaned: dict[str, Any] = {}
    for key, value in req.values.items():
        cleaned[key] = _validate_value(key, value)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = RUNTIME_CONFIG_PATH.with_suffix(".tmp")
    payload = {"version": 1, "updated_at": time.time(), "values": cleaned}
    tmp_path.write_text(json.dumps(payload, indent=2))
    os.replace(tmp_path, RUNTIME_CONFIG_PATH)
    logger.info(f"runtime_config updated: {sorted(cleaned.keys())}")
    return {"ok": True, "applied_keys": sorted(cleaned.keys())}


# ── Secrets (encrypted) ──────────────────────────────────────────────────

@router.put("/secrets")
def put_secrets(req: SecretsUpdate, authorization: Optional[str] = Header(None)) -> dict:
    _require_token(authorization)

    # Merge with existing decrypted secrets so the caller can update one
    # field without losing the others. Encryption uses /data/secrets.key,
    # not the admin password — see runtime_config.py.
    existing = _decrypt_secrets()
    merged = dict(existing)
    for key, value in req.values.items():
        if key not in SECRET_FIELDS:
            raise HTTPException(status_code=400, detail=f"Unknown secret: {key}")
        if value is None or value == "":
            # Treat empty string as "unset"
            merged.pop(key, None)
        else:
            merged[key] = str(value)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        blob = encrypt_secrets(merged)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    tmp_path = RUNTIME_SECRETS_PATH.with_suffix(".tmp")
    tmp_path.write_bytes(blob)
    try:
        os.chmod(tmp_path, 0o600)
    except Exception:
        pass
    os.replace(tmp_path, RUNTIME_SECRETS_PATH)

    masked: dict[str, dict] = {}
    for key in SECRET_FIELDS:
        v = merged.get(key, "")
        if v:
            masked[key] = {"set": True, "preview": "•" * 8 + v[-4:] if len(v) > 4 else "••••"}
        else:
            masked[key] = {"set": False, "preview": ""}

    logger.info(f"runtime_secrets updated: keys_set={[k for k, v in merged.items() if v]}")
    return {"ok": True, "masked": masked}


# ── Apply (graceful exit so docker-compose restarts with new config) ────

@router.post("/apply")
def apply_endpoint(authorization: Optional[str] = Header(None)) -> dict:
    _require_token(authorization)
    """
    Trigger a graceful container exit. Docker compose's restart=unless-stopped
    brings the container back up, at which point runtime_config + secrets are
    re-applied via the runtime_config.apply_overlay() call at module-import
    time.

    Implementation: we don't kill the api process itself (the response would
    never be returned). Instead we touch a sentinel file that the manager
    process polls; multi_grid_manager exits on detection, which the
    entrypoint's child-watch translates into a container exit.
    """
    sentinel = DATA_DIR / "restart.signal"
    sentinel.write_text(str(time.time()))
    logger.warning(f"apply requested — restart sentinel written: {sentinel}")
    return {"ok": True, "sentinel": str(sentinel)}


# ── CLI helper: bcrypt hash a password for .env ─────────────────────────

def _hash_password_cli() -> int:
    """Read a password from stdin (no echo), print the bcrypt hash line."""
    import getpass
    pw = getpass.getpass("Password: ")
    if not pw:
        print("Empty password, aborting.")
        return 1
    pw2 = getpass.getpass("Confirm: ")
    if pw != pw2:
        print("Passwords don't match, aborting.")
        return 1
    h = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    print()
    print("Add the following line to .env (and remove any old ADMIN_PASSWORD line):")
    print()
    print(f"ADMIN_PASSWORD_HASH={h}")
    print()
    print("Then restart the container: docker compose up -d --force-recreate")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("hash-password", "hash"):
        sys.exit(_hash_password_cli())
    print(f"Usage: python {sys.argv[0]} hash-password")
    sys.exit(2)
