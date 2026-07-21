"""
daylight.py
Daylight Transport (dylt.com) carrier API integration.

Phase 1 (OAuth-only, read): fuelSurcharge, transitTimes, externalTrace.
Phase 2 (OAuth + MyDaylight account creds): rateQuote, pickup, image/bol.

Env:
  DAYLIGHT_CLIENT_ID          (Phase 1) Apigee app consumer key
  DAYLIGHT_CLIENT_SECRET      (Phase 1) Apigee app consumer secret
  DAYLIGHT_BASE_URL           default https://test-api.dylt.com  (TEST server)
  DAYLIGHT_TOKEN_URL          default https://api.dylt.com/oauth/client_credential/accesstoken
  DAYLIGHT_ACCOUNT_NUMBER     (Phase 2) Daylight account number (<=10 digits)
  DAYLIGHT_MYDAYLIGHT_USER    (Phase 2) MyDaylight username
  DAYLIGHT_MYDAYLIGHT_PASSWORD(Phase 2) MyDaylight password

Auth: Apigee OAuth2 client-credentials -> access_token (~15 min, cached). Phase-2 calls ALSO
carry accountNumber/userName/password in the request body (MyDaylight account auth). urllib only.
Request-body shapes follow the public XSDs at {base}/rateQuote/schema, /pickup/schema, /image/schema.
"""

import os
import json
import time
import base64
import urllib.request
import urllib.error
import urllib.parse
from typing import Dict, List

DAYLIGHT_CLIENT_ID = os.environ.get("DAYLIGHT_CLIENT_ID", "").strip()
DAYLIGHT_CLIENT_SECRET = os.environ.get("DAYLIGHT_CLIENT_SECRET", "").strip()
DAYLIGHT_BASE_URL = os.environ.get(
    "DAYLIGHT_BASE_URL", "https://test-api.dylt.com"
).strip().rstrip("/")
DAYLIGHT_TOKEN_URL = os.environ.get(
    "DAYLIGHT_TOKEN_URL",
    "https://api.dylt.com/oauth/client_credential/accesstoken",
).strip()

# Phase 2 -- MyDaylight account credentials (carried in the request body).
DAYLIGHT_ACCOUNT_NUMBER = os.environ.get("DAYLIGHT_ACCOUNT_NUMBER", "").strip()
DAYLIGHT_MYDAYLIGHT_USER = os.environ.get("DAYLIGHT_MYDAYLIGHT_USER", "").strip()
DAYLIGHT_MYDAYLIGHT_PASSWORD = os.environ.get("DAYLIGHT_MYDAYLIGHT_PASSWORD", "").strip()


class DaylightAPIError(Exception):
    """Custom exception for Daylight API errors."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Daylight API Error ({status_code}): {message}")


def is_configured() -> bool:
    return bool(DAYLIGHT_CLIENT_ID and DAYLIGHT_CLIENT_SECRET)


def mydaylight_configured() -> bool:
    return bool(DAYLIGHT_ACCOUNT_NUMBER and DAYLIGHT_MYDAYLIGHT_USER and DAYLIGHT_MYDAYLIGHT_PASSWORD)


# In-process token cache: {"token": <str|None>, "expires_at": <epoch float>}
_token_cache: Dict[str, object] = {"token": None, "expires_at": 0.0}
# Records which token URL last succeeded (diagnostic; no secret).
_last_token_url: str = ""


def _with_grant(url: str) -> str:
    if "grant_type" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}grant_type=client_credentials"
    return url


def _token_url_candidates() -> List[str]:
    """Configured token URL first, then the token endpoint on the same host as the
    data base URL (covers test-env apps whose token lives on test-api)."""
    cands = [_with_grant(DAYLIGHT_TOKEN_URL)]
    same_host = _with_grant(f"{DAYLIGHT_BASE_URL}/oauth/client_credential/accesstoken")
    out: List[str] = []
    for c in (cands + [same_host]):
        if c not in out:
            out.append(c)
    return out


def _request_token(url: str, use_basic: bool) -> dict:
    """One token attempt against `url`. use_basic=True -> Authorization: Basic header;
    else creds in the form body. Raises urllib errors to the caller."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if use_basic:
        creds = base64.b64encode(
            f"{DAYLIGHT_CLIENT_ID}:{DAYLIGHT_CLIENT_SECRET}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {creds}"
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    else:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": DAYLIGHT_CLIENT_ID,
            "client_secret": DAYLIGHT_CLIENT_SECRET,
        }).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _fetch_token() -> str:
    """
    Obtain (and cache) an access_token, trying each (token-URL x transport) combo.
    The token value is never logged or returned to callers.
    """
    global _last_token_url
    now = time.time()
    cached = _token_cache.get("token")
    if cached and now < float(_token_cache.get("expires_at", 0)):
        return str(cached)

    if not is_configured():
        raise DaylightAPIError(
            500, "Daylight not configured (set DAYLIGHT_CLIENT_ID / DAYLIGHT_CLIENT_SECRET)"
        )

    data = None
    used_url = ""
    errors = []
    for url in _token_url_candidates():
        host = urllib.parse.urlparse(url).netloc
        for use_basic in (True, False):
            label = f"{host}/{'basic' if use_basic else 'form'}"
            try:
                data = _request_token(url, use_basic)
                used_url = url
                break
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode()[:160]
                except Exception:
                    pass
                errors.append(f"{label}: {e.code} {detail}")
            except urllib.error.URLError as e:
                errors.append(f"{label}: connection error {e}")
        if data is not None:
            break

    if data is None:
        raise DaylightAPIError(401, "token request failed [" + " | ".join(errors) + "]")

    token = data.get("access_token") or data.get("accessToken")
    if not token:
        raise DaylightAPIError(500, f"no access_token in token response (keys={list(data.keys())})")

    try:
        expires_in = int(float(data.get("expires_in", 900)))
    except (TypeError, ValueError):
        expires_in = 900

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(60, expires_in - 60)
    _last_token_url = used_url
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


def _post(path: str, body: Dict, expect: str = "json") -> Dict:
    """Authenticated POST (OAuth Bearer). expect='pdf' returns {'pdf_bytes','content_type'}."""
    token = _fetch_token()
    url = f"{DAYLIGHT_BASE_URL}{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/pdf" if expect == "pdf" else "application/json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "") or ""
            if expect == "pdf" or "pdf" in ctype.lower():
                # Could still be a JSON error even when we asked for pdf.
                if "json" in ctype.lower():
                    try:
                        return json.loads(raw.decode())
                    except Exception:
                        pass
                return {"pdf_bytes": raw, "content_type": ctype, "size": len(raw)}
            txt = raw.decode()
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                return {"raw": txt}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:400]
        except Exception:
            pass
        raise DaylightAPIError(e.code, f"POST {path} failed: {e.reason} {detail}")
    except urllib.error.URLError as e:
        raise DaylightAPIError(500, f"POST {path} connection error: {e}")


def _mydaylight_auth() -> Dict:
    """The three account-auth fields every Phase-2 request body carries."""
    if not mydaylight_configured():
        raise DaylightAPIError(
            500,
            "MyDaylight not configured (set DAYLIGHT_ACCOUNT_NUMBER / "
            "DAYLIGHT_MYDAYLIGHT_USER / DAYLIGHT_MYDAYLIGHT_PASSWORD)",
        )
    return {
        "accountNumber": DAYLIGHT_ACCOUNT_NUMBER,
        "userName": DAYLIGHT_MYDAYLIGHT_USER,
        "password": DAYLIGHT_MYDAYLIGHT_PASSWORD,
    }


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
        "token_url_used": _last_token_url,
        "mydaylight_configured": mydaylight_configured(),
    }


# =============================================================================
# PHASE 1 -- read-only (OAuth only)
# =============================================================================

def get_fuel_surcharge() -> Dict:
    """Current fuel surcharge rate + discount percentage."""
    return _get("/fuelSurcharge")


def get_transit_times(orig_zip: str, dest_zip: str) -> Dict:
    """Freight transit time between an origin and destination zip."""
    o = urllib.parse.quote(str(orig_zip).strip())
    d = urllib.parse.quote(str(dest_zip).strip())
    return _get(f"/transitTimes/{o}/{d}")


def trace(probill: str) -> Dict:
    """Shipment tracing for a Daylight probill (PRO) number (8-10 digits)."""
    return _get(f"/externalTrace/{urllib.parse.quote(str(probill).strip())}")


def trace_booking(booking_number: str) -> Dict:
    """Shipment tracing for a Daylight booking number (from pickup requests)."""
    return _get(f"/externalTrace/booking/{urllib.parse.quote(str(booking_number).strip())}")


# =============================================================================
# PHASE 2 -- MyDaylight-authenticated (rateQuote / pickup / image-BOL)
# Body shapes mirror the public XSDs. `fields` is the inner object; account auth is merged in.
# =============================================================================

def rate_quote(fields: Dict) -> Dict:
    """
    POST /rateQuote -> dyltRateQuoteResp.
    `fields` = dyltRateQuoteReq minus auth, e.g.:
      {"billTerms":"Collect","serviceType":"LTL","pickupDate":"2026-07-22",
       "shipperInfo":{"customerAddress":{"zip":"90660","city":"Pico Rivera","state":"CA"}},
       "consigneeInfo":{"customerAddress":{"zip":"24112","city":"Martinsville","state":"VA"}},
       "items":{"item":[{"pcs":2,"pallets":2,"weight":2300,"actualClass":"85","description":"Cabinets"}]},
       "accessorials":{"accessorial":[{"accName":"Delivery","accId":"Overlength 8 ft but less than 12 ft"}]}}
    """
    return _post("/rateQuote", {"dyltRateQuoteReq": {**_mydaylight_auth(), **fields}})


def create_bol(fields: Dict) -> Dict:
    """
    POST /image/bol -> populated BOL (PDF bytes) or a JSON imageResponse error.
    `fields` = dyltImageReq minus auth (shipper*/consignee*/billTo*/items/accessorials/bolDate/billTerms).
    """
    return _post("/image/bol", {"dyltImageReq": {**_mydaylight_auth(), **fields}}, expect="pdf")


def schedule_pickup(fields: Dict) -> Dict:
    """
    POST /pickup -> dyltPickupResps.
    `fields` = dyltPickupReq minus auth (shipper*/consignee*/pickupStart|End Date/Time/items/...).
    """
    return _post("/pickup", {"dyltPickupReqs": {"dyltPickupReq": {**_mydaylight_auth(), **fields}}})
