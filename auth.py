"""
auth.py - CFC Orders Admin Authentication
Phase 5 Backend Hardening

Provides a FastAPI Depends() dependency for admin-protected endpoints.

Current mode:  API key check (ADMIN_API_KEY env var, default CFC2025)
Upgrade path:  Set ADMIN_JWT_SECRET env var to also accept signed HS256 JWTs

Usage:
    from auth import require_admin
    from fastapi import Depends

    @router.delete("/orders/{order_id}")
    def delete_order(order_id: str, _: bool = Depends(require_admin)):
        ...

Generating a JWT token (after setting ADMIN_JWT_SECRET):
    python -c "from auth import create_admin_token; print(create_admin_token())"

Environment variables:
    ADMIN_API_KEY           Raw API key accepted as-is (default: CFC2025)
    ADMIN_JWT_SECRET        If set, HS256 JWTs are also accepted
    ADMIN_JWT_EXPIRY_HOURS  JWT lifetime in hours (default: 24)
"""

import os
import time
import json
import base64
import hashlib
import hmac as _hmac_module
from typing import Optional
from fastapi import HTTPException, Header


# =============================================================================
# CONFIG
# =============================================================================

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "CFC2025")
JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "")
JWT_EXPIRY_HOURS = int(os.environ.get("ADMIN_JWT_EXPIRY_HOURS", "24"))


# =============================================================================
# DEPENDENCY
# =============================================================================

def require_admin(
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    authorization: Optional[str] = Header(None),
) -> bool:
    """
    FastAPI dependency — validates admin access.

    Accepts token via:
      - X-Admin-Token: <token>   header  (preferred)
      - Authorization: Bearer <token>    header  (fallback)

    Token may be:
      - The raw ADMIN_API_KEY value (default: CFC2025)
      - A signed HS256 JWT (if ADMIN_JWT_SECRET is configured)

    Returns True on success, raises HTTP 401 on failure.
    """
    token = _extract_token(x_admin_token, authorization)

    if not token:
        raise HTTPException(
            status_code=401,
            detail=(
                "Admin token required. "
                "Send X-Admin-Token header or Authorization: Bearer <token>"
            ),
        )

    # Try JWT first when secret is configured
    if JWT_SECRET:
        try:
            if _verify_jwt(token):
                return True
        except Exception:
            pass  # Fall through to raw key check

    # Raw API key check (constant-time compare)
    if _hmac_module.compare_digest(token, ADMIN_API_KEY):
        return True

    raise HTTPException(status_code=401, detail="Invalid admin token")


def _extract_token(
    x_admin_token: Optional[str],
    authorization: Optional[str],
) -> Optional[str]:
    """Pull token string from whichever header was provided."""
    if x_admin_token:
        return x_admin_token.strip()
    if authorization:
        auth = authorization.strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
    return None


# =============================================================================
# JWT HELPERS  (HS256, stdlib only — no python-jose dependency)
# =============================================================================

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def create_admin_token(expiry_hours: int = JWT_EXPIRY_HOURS) -> str:
    """
    Create a signed HS256 JWT with admin role.
    Requires ADMIN_JWT_SECRET env var to be set.

    Example:
        export ADMIN_JWT_SECRET=<random-64-char-string>
        python -c "from auth import create_admin_token; print(create_admin_token())"
    """
    if not JWT_SECRET:
        raise ValueError(
            "ADMIN_JWT_SECRET is not set. "
            "Add it as a Render environment variable before generating JWT tokens."
        )

    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": "admin",
        "role": "admin",
        "iat": now,
        "exp": now + expiry_hours * 3600,
    }

    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}"

    sig = _hmac_module.new(
        JWT_SECRET.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    return f"{signing_input}.{_b64url_encode(sig)}"


def _verify_jwt(token: str) -> bool:
    """
    Verify an HS256 JWT.  Returns True if valid, raises ValueError if not.
    Callers should catch Exception broadly.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed JWT: expected 3 parts")

    h_b64, p_b64, sig_b64 = parts
    signing_input = f"{h_b64}.{p_b64}"

    # Signature check (constant-time)
    expected = _hmac_module.new(
        JWT_SECRET.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_b64 = _b64url_encode(expected)

    if not _hmac_module.compare_digest(sig_b64, expected_b64):
        raise ValueError("JWT signature mismatch")

    # Payload checks
    payload = json.loads(_b64url_decode(p_b64))

    exp = payload.get("exp", 0)
    if exp and time.time() > exp:
        raise ValueError("JWT token expired")

    if payload.get("role") != "admin":
        raise ValueError("JWT does not carry admin role")

    return True
