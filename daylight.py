"""
daylight.py
Daylight Transport (dylt.com) carrier API integration — Phase 1 (read-only).

Daylight is a west-coast LTL carrier, cheaper than R+L but with limited lane
coverage. This module is the OAuth2 handshake plus the endpoints that need ONLY
the app credentials (no MyDaylight customer login):
  - fuelSurcharge            GET /fuelSurcharge
  - transitTimes             GET /transitTimes/{origZip}/{destZip}
  - externalTrace            GET /externalTrace/{probill}
  - externalTrace/booking    GET /externalTrace/booking/{bookingNumber}

Phase 2 (rateQuote / pickup / image-bol) additionally needs MyDaylight
username+password and is intentionally NOT built here.

Env:
  DAYLIGHT_CLIENT_ID       (required) Apigee app consumer key
  DAYLIGHT_CLIENT_SECRET   (required) Apigee app consumer secret
  DAYLIGHT_BASE_URL        (default https://test-api.dylt.com  — TEST server; docs say test here)
  DAYLIGHT_TOKEN_URL       (default https://api.dylt.com/oauth/client_credential/accesstoken)

Auth flow (Apigee OAuth2 client-credentials):
  POST client_id+client_secret to the token endpoint -> access_token (~15 min).
  Cache in-process until ~60s before expiry; send as 'Authorization: Bearer <token>'.
Follows the urllib style of b2bwave_api.py (no external HTTP dependency).
"""

import os
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Dict

DAYLIGHT_CLIENT_ID = os.environ.get("DAYLIGHT_CLIENT_ID", "").strip()
DAYLIGHT_CLIENT_SECRET = os.environ.get("DAYLIGHT_CLIENT_SECRET", "").strip()
DAYLIGHT_BASE_URL = os.environ.get(
    "DAYLIGHT_BASE_URL", "https://test-api.dylt.com"
).strip().rstrip("/")
DAYLIGHT_TOKEN_URL = os.environ.get(
    "DAYLIGHT_TOKEN_URL",
    "https://api.dylt.com/oauth/client_credential/accesstoken",
).strip()


class DaylightAPIError(Exception):
    """Custom exception for Daylight API errors."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Daylight API Error ({status_code}): {message}")


def is_configured() -> bool:
    return bool(DAYLIGHT_CLIENT_ID and DAYLIGHT_CLIENT_SECRET)


# In-process token cache: {"token": <str|None>, "expires_at": <epoch float>}
_token_cache: Dict[str, object] = {"token": None, "expires_at": 0.0}


def _fetch_token() -> str:
    """
    POST client_credentials -> access_token. Cached until ~60s before expiry.
    The token value is never logged or returned to callers.
    """
    now = time.time()
    cached = _token_cache.get("token")
    if cached and now < float(_token_cache.get("expires_at", 0)):
        return str(cached)

    if not is_configured():
        raise DaylightAPIError(
            500, "Daylight not configured (set DAYLIGHT_CLIENT_ID / DAYLIGHT_CLIENT_SECRET)"
        )

    # Apigee accesstoken endpoint: grant_type in the query, creds in the form body.
    sep = "&" if "?" in DAYLIGHT_TOKEN_URL else "?"
    url = DAYLIGHT_TOKEN_URL
    if "grant_type" not in url:
        url = f"{url}{sep}grant_type=client_credentials"

    body = urllib.parse.urlencode({
        "client_id": DAYLIGHT_CLIENT_ID,
        "client_secret": DAYLIGHT_CLIENT_SECRET,
    }).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        raise DaylightAPIError(e.code, f"token request failed: {e.reason} {detail}")
    except urllib.error.URLError as e:
        raise DaylightAPIError(500, f"token connection error: {e}")

    token = data.get("access_token") or data.get("accessToken")
    if not token:
        raise DaylightAPIError(500, f"no access_token in token response (keys={list(data.keys())})")

    try:
        expires_in = int(float(data.get("expires_in", 900)))
    except (TypeError, ValueError):
        expires_in = 900

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(60, expires_in - 60)
    return str(token)


def _get(path: str) -> Dict:
    """Authenticated GET against the Daylight base URL. `path` begins with '/'."""
    token = _fetch_token()
    url = f"{DAYLIGHT_BASE_URL}{path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        raise DaylightAPIError(e.code, f"GET {path} failed: {e.reason} {detail}")
    except urllib.error.URLError as e:
        raise DaylightAPIError(500, f"GET {path} connection error: {e}")


def token_check() -> Dict:
    """Prove the OAuth handshake works WITHOUT ever exposing the token value."""
    token = _fetch_token()
    remaining = int(float(_token_cache.get("expires_at", 0)) - time.time())
    return {
        "ok": True,
        "token_acquired": bool(token),
        "token_length": len(token),
        "expires_in_seconds": remaining,
        "base_url": DAYLIGHT_BASE_URL,
        "token_url": DAYLIGHT_TOKEN_URL,
    }


def get_fuel_surcharge() -> Dict:
    """Current fuel surcharge rate + discount percentage."""
    return _get("/fuelSurcharge")


def get_transit_times(orig_zip: str, dest_zip: str) -> Dict:
    """Freight transit time between an origin and destination zip."""
    o = urllib.parse.quote(str(orig_zip).strip())
    d = urllib.parse.quote(str(dest_zip).strip())
    return _get(f"/transitTimes/{o}/{d}")


def trace(probill: str) -> Dict:
    """Shipment tracing for a Daylight probill (PRO) number."""
    return _get(f"/externalTrace/{urllib.parse.quote(str(probill).strip())}")


def trace_booking(booking_number: str) -> Dict:
    """Shipment tracing for a Daylight booking number (from pickup requests)."""
    return _get(f"/externalTrace/booking/{urllib.parse.quote(str(booking_number).strip())}")
