"""
Admin + dashboard access API for grid-trader.

Authenticates against bcrypt hashes in env, issues bearer tokens with TTL,
exposes admin endpoints to read/write runtime config + encrypted secrets, and
exposes viewer access endpoints for tenant-scoped dashboard sessions.

Environment variables:
  - ADMIN_PASSWORD_HASH : bcrypt hash for admin login
  - VIEWER_PIN_HASH     : bcrypt hash for read-only dashboard viewer login
  - DASHBOARD_TENANT_ID : single allowed tenant id for dashboard sessions

To bootstrap a password or viewer PIN, run:
    python admin.py hash-password
and paste the printed hash into .env.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
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
    _load_runtime_config,
)

logger = logging.getLogger("admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])
access_router = APIRouter(prefix="/api/access", tags=["access"])

TOKEN_TTL_SECONDS = 8 * 3600
ROLE_RANK = {"viewer": 1, "admin": 2}
_tokens: dict[str, dict[str, Any]] = {}


# ── Auth helpers ─────────────────────────────────────────────────────────

def _admin_password_hash() -> str:
    return os.environ.get("ADMIN_PASSWORD_HASH", "") or ""


def _viewer_pin_hash() -> str:
    return os.environ.get("VIEWER_PIN_HASH", "") or ""


def dashboard_tenant() -> str:
    tenant = (os.environ.get("DASHBOARD_TENANT_ID", "default") or "default").strip()
    return tenant or "default"


def auth_required() -> bool:
    """Whether read-only dashboard endpoints require a viewer session."""
    return bool(_viewer_pin_hash())


def _verify_hash_value(provided: str, expected_hash: str) -> bool:
    if not expected_hash or not provided:
        return False
    try:
        return bcrypt.checkpw(provided.encode("utf-8"), expected_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _verify_password(provided: str) -> bool:
    return _verify_hash_value(provided, _admin_password_hash())


def _verify_viewer_pin(provided: str) -> bool:
    return _verify_hash_value(provided, _viewer_pin_hash())


def _normalize_tenant(requested: Optional[str]) -> str:
    requested_value = (requested or dashboard_tenant()).strip() or dashboard_tenant()
    configured = dashboard_tenant()
    if requested_value != configured:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Tenant mismatch: dashboard is scoped to {configured}",
        )
    return configured


def _issue_token(role: str, tenant: Optional[str] = None) -> tuple[str, float]:
    tenant_id = _normalize_tenant(tenant)
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + TOKEN_TTL_SECONDS
    _tokens[token] = {
        "role": role,
        "tenant": tenant_id,
        "issued_at": time.time(),
        "expires_at": expires_at,
    }
    now = time.time()
    for t, session in list(_tokens.items()):
        if float(session.get("expires_at", 0)) < now:
            _tokens.pop(t, None)
    return token, expires_at


def _extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    return token


def _resolve_session(token: str) -> dict[str, Any]:
    session = _tokens.get(token)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if float(session.get("expires_at", 0)) < time.time():
        _tokens.pop(token, None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    return session


def require_access(
    authorization: Optional[str],
    *,
    required_role: str = "viewer",
    tenant: Optional[str] = None,
    allow_public_if_disabled: bool = True,
) -> dict[str, Any]:
    requested_tenant = _normalize_tenant(tenant)
    if allow_public_if_disabled and not auth_required():
        return {
            "role": "public",
            "tenant": requested_tenant,
            "issued_at": None,
            "expires_at": None,
        }

    token = _extract_bearer_token(authorization)
    session = _resolve_session(token)
    session_role = str(session.get("role") or "viewer")
    if ROLE_RANK.get(session_role, 0) < ROLE_RANK.get(required_role, 0):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient access level")
    if session.get("tenant") != requested_tenant:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token not valid for requested tenant")
    return session


def require_viewer_access(authorization: Optional[str], tenant: Optional[str] = None) -> dict[str, Any]:
    return require_access(authorization, required_role="viewer", tenant=tenant, allow_public_if_disabled=True)


def require_admin_access(authorization: Optional[str], tenant: Optional[str] = None) -> dict[str, Any]:
    if not _admin_password_hash():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Admin access is disabled")
    return require_access(authorization, required_role="admin", tenant=tenant, allow_public_if_disabled=False)


def build_access_context(session: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    effective = session or {
        "role": "public" if not auth_required() else None,
        "tenant": dashboard_tenant(),
        "expires_at": None,
    }
    return {
        "auth_required": auth_required(),
        "dashboard_mode": "tenant_scoped" if auth_required() else "public",
        "role": effective.get("role"),
        "tenant": effective.get("tenant", dashboard_tenant()),
        "expires_at": effective.get("expires_at"),
    }


# ── Models ───────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str
    tenant: Optional[str] = None


class ViewerLoginRequest(BaseModel):
    pin: str
    tenant: Optional[str] = None


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class SecretsUpdate(BaseModel):
    values: dict[str, str]


# ── Status / login ───────────────────────────────────────────────────────

@router.get("/status")
def status_endpoint() -> dict:
    has_hash = bool(_admin_password_hash())
    secret_keys_set = []
    for key in SECRET_FIELDS:
        if os.environ.get(key):
            secret_keys_set.append(key)
    return {
        "has_admin_password": has_hash,
        "has_viewer_pin": bool(_viewer_pin_hash()),
        "auth_required": auth_required(),
        "dashboard_tenant": dashboard_tenant(),
        "secret_keys_set": secret_keys_set,
    }


@access_router.get("/status")
def access_status_endpoint() -> dict:
    return {
        "auth_required": auth_required(),
        "has_admin_password": bool(_admin_password_hash()),
        "has_viewer_pin": bool(_viewer_pin_hash()),
        "dashboard_tenant": dashboard_tenant(),
        "dashboard_mode": "tenant_scoped" if auth_required() else "public",
    }


@router.post("/login")
def login_endpoint(req: LoginRequest) -> dict:
    if not _verify_password(req.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication failed")
    token, expires_at = _issue_token("admin", tenant=req.tenant)
    return {
        "token": token,
        "expires_at": expires_at,
        "role": "admin",
        "tenant": dashboard_tenant(),
    }


@access_router.post("/viewer-login")
def viewer_login_endpoint(req: ViewerLoginRequest) -> dict:
    if not _viewer_pin_hash():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Viewer access is disabled")
    if not _verify_viewer_pin(req.pin):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication failed")
    token, expires_at = _issue_token("viewer", tenant=req.tenant)
    return {
        "token": token,
        "expires_at": expires_at,
        "role": "viewer",
        "tenant": dashboard_tenant(),
    }


@access_router.get("/session")
def access_session_endpoint(authorization: Optional[str] = Header(None)) -> dict:
    session = require_viewer_access(authorization)
    return build_access_context(session)


# ── Settings (tunables) ──────────────────────────────────────────────────

def _typed_value(raw: Any, key: str, default: Any) -> Any:
    spec = TUNABLE_FIELDS[key]
    if raw is None:
        return default
    if spec.type == "int":
        try:
            return int(float(raw))
        except Exception:
            return default
    if spec.type == "float":
        try:
            return float(raw)
        except Exception:
            return default
    if spec.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return str(raw)


@router.get("/settings")
def get_settings(authorization: Optional[str] = Header(None)) -> dict:
    require_admin_access(authorization)
    schema = {key: spec.as_dict() for key, spec in TUNABLE_FIELDS.items()}
    saved_overrides = _load_runtime_config()
    values: dict[str, Any] = {}
    for key, spec in TUNABLE_FIELDS.items():
        if key in saved_overrides:
            values[key] = _typed_value(saved_overrides.get(key), key, spec.default)
            continue
        values[key] = _typed_value(os.environ.get(key), key, spec.default)

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
        "access": build_access_context({"role": "admin", "tenant": dashboard_tenant(), "expires_at": None}),
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
    require_admin_access(authorization)
    cleaned: dict[str, Any] = {}
    for key, value in req.values.items():
        cleaned[key] = _validate_value(key, value)

    merged = dict(_load_runtime_config())
    merged.update(cleaned)
    min_safe = int(merged.get("MIN_SAFE_LEVERAGE", merged.get("MIN_DEPLOY_LEVERAGE", TUNABLE_FIELDS["MIN_SAFE_LEVERAGE"].default)))
    max_safe = int(merged.get("MAX_SAFE_LEVERAGE", merged.get("MAX_DEPLOY_LEVERAGE", TUNABLE_FIELDS["MAX_SAFE_LEVERAGE"].default)))
    min_deploy = int(merged.get("MIN_DEPLOY_LEVERAGE", min_safe))
    max_deploy = int(merged.get("MAX_DEPLOY_LEVERAGE", max_safe))
    if max_safe < min_safe:
        raise HTTPException(status_code=400, detail="MAX_SAFE_LEVERAGE must be >= MIN_SAFE_LEVERAGE")
    if max_deploy < min_deploy:
        raise HTTPException(status_code=400, detail="MAX_DEPLOY_LEVERAGE must be >= MIN_DEPLOY_LEVERAGE")
    if max_deploy > max_safe:
        raise HTTPException(status_code=400, detail="MAX_DEPLOY_LEVERAGE must be <= MAX_SAFE_LEVERAGE")
    if min_deploy < min_safe:
        raise HTTPException(status_code=400, detail="MIN_DEPLOY_LEVERAGE must be >= MIN_SAFE_LEVERAGE")

    merged["MIN_SAFE_LEVERAGE"] = min_safe
    merged["MAX_SAFE_LEVERAGE"] = max_safe
    merged["MIN_DEPLOY_LEVERAGE"] = min_safe
    merged["MAX_DEPLOY_LEVERAGE"] = max_safe

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = RUNTIME_CONFIG_PATH.with_suffix(".tmp")
    payload = {"version": 1, "updated_at": time.time(), "values": merged}
    tmp_path.write_text(json.dumps(payload, indent=2))
    os.replace(tmp_path, RUNTIME_CONFIG_PATH)
    logger.info(f"runtime_config updated: {sorted(cleaned.keys())}")
    return {"ok": True, "applied_keys": sorted(cleaned.keys())}


# ── Secrets (encrypted) ──────────────────────────────────────────────────

@router.put("/secrets")
def put_secrets(req: SecretsUpdate, authorization: Optional[str] = Header(None)) -> dict:
    require_admin_access(authorization)
    existing = _decrypt_secrets()
    merged = dict(existing)
    for key, value in req.values.items():
        if key not in SECRET_FIELDS:
            raise HTTPException(status_code=400, detail=f"Unknown secret: {key}")
        if value is None or value == "":
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
    require_admin_access(authorization)
    sentinel = DATA_DIR / "restart.signal"
    sentinel.write_text(str(time.time()))
    logger.warning(f"apply requested — restart sentinel written: {sentinel}")
    return {"ok": True, "sentinel": str(sentinel)}


# ── CLI helper: bcrypt hash a password for .env ─────────────────────────

def _hash_password_cli(password: Optional[str] = None, confirm: Optional[str] = None) -> int:
    import sys
    import getpass

    pw = password
    pw2 = confirm

    if pw is None:
        if sys.stdin.isatty():
            pw = getpass.getpass("Password/PIN: ")
            pw2 = getpass.getpass("Confirm: ")
        else:
            piped = sys.stdin.read().splitlines()
            if len(piped) >= 2:
                pw, pw2 = piped[0], piped[1]
            elif len(piped) == 1:
                pw, pw2 = piped[0], piped[0]
            else:
                env_pw = os.environ.get("ADMIN_PASSWORD")
                if env_pw is None:
                    print("Non-interactive mode detected but no password input found.")
                    print("Use one of:")
                    print("  1) printf '%s\n%s\n' 'your-pass' 'your-pass' | docker exec -i <container> python admin.py hash-password")
                    print("  2) docker exec -e ADMIN_PASSWORD='your-pass' <container> python admin.py hash-password")
                    print("  3) docker exec <container> python admin.py hash-password --password 'your-pass'")
                    return 2
                pw = env_pw
                pw2 = os.environ.get("ADMIN_PASSWORD_CONFIRM", env_pw)
    elif pw2 is None:
        pw2 = pw

    if not pw:
        print("Empty password, aborting.")
        return 1
    if pw != pw2:
        print("Passwords don't match, aborting.")
        return 1
    h = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    print()
    print("Add the following line to .env:")
    print()
    print(f"ADMIN_PASSWORD_HASH={h}")
    print("# or VIEWER_PIN_HASH=<same hash if you want viewer PIN auth>")
    print()
    print("Then recreate/restart the service so the new hash is loaded.")
    return 0


if __name__ == "__main__":
    import argparse
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("hash-password", "hash"):
        parser = argparse.ArgumentParser(
            prog=f"python {sys.argv[0]} hash-password",
            description="Generate bcrypt hash for ADMIN_PASSWORD_HASH or VIEWER_PIN_HASH",
        )
        parser.add_argument("--password", help="Password/PIN (for non-interactive usage)")
        parser.add_argument("--confirm", help="Confirmation (optional; defaults to --password)")
        args = parser.parse_args(sys.argv[2:])
        sys.exit(_hash_password_cli(password=args.password, confirm=args.confirm))
    print(f"Usage: python {sys.argv[0]} hash-password")
    sys.exit(2)
