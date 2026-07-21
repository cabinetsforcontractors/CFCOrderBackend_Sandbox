"""
daylight_routes.py
FastAPI router for the Daylight Transport carrier module.

All endpoints require admin token (X-Admin-Token header).

Phase 1 (OAuth only): config-check, token-check, fuel-surcharge, transit, trace.
Phase 2 (OAuth + MyDaylight): rate-quote, bol, pickup  (raw pass-through test endpoints;
each takes the inner request fields as the JSON body -- account auth is merged server-side).

Mount in main.py:
    from daylight_routes import daylight_router
    app.include_router(daylight_router)
"""

import base64
from fastapi import APIRouter, Depends, HTTPException, Body
from auth import require_admin
import daylight

daylight_router = APIRouter(prefix="/daylight", tags=["daylight"])


def _handle(fn, *args):
    try:
        return fn(*args)
    except daylight.DaylightAPIError as e:
        raise HTTPException(status_code=502, detail=f"Daylight API error ({e.status_code}): {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Daylight error: {e}")


@daylight_router.get("/config-check")
async def daylight_config_check(_: bool = Depends(require_admin)):
    """Credential presence + lengths (never the values). [admin]"""
    cid = daylight.DAYLIGHT_CLIENT_ID
    csec = daylight.DAYLIGHT_CLIENT_SECRET
    return {
        "client_id_set": bool(cid),
        "client_id_length": len(cid),
        "client_id_preview": (cid[:4] + "...") if cid else "",
        "client_secret_set": bool(csec),
        "client_secret_length": len(csec),
        "mydaylight_account_set": bool(daylight.DAYLIGHT_ACCOUNT_NUMBER),
        "mydaylight_account_length": len(daylight.DAYLIGHT_ACCOUNT_NUMBER),
        "mydaylight_user_set": bool(daylight.DAYLIGHT_MYDAYLIGHT_USER),
        "mydaylight_password_set": bool(daylight.DAYLIGHT_MYDAYLIGHT_PASSWORD),
        "mydaylight_ready": daylight.mydaylight_configured(),
        "base_url": daylight.DAYLIGHT_BASE_URL,
        "token_url": daylight.DAYLIGHT_TOKEN_URL,
    }


@daylight_router.get("/token-check")
async def daylight_token_check(_: bool = Depends(require_admin)):
    """Prove the OAuth handshake works. Never returns the token itself. [admin]"""
    return _handle(daylight.token_check)


# ---------- Phase 1 (read-only) ----------

@daylight_router.get("/fuel-surcharge")
async def daylight_fuel_surcharge(_: bool = Depends(require_admin)):
    """Live fuel surcharge rate + discount. [admin]"""
    return _handle(daylight.get_fuel_surcharge)


@daylight_router.get("/transit/{orig_zip}/{dest_zip}")
async def daylight_transit(orig_zip: str, dest_zip: str, _: bool = Depends(require_admin)):
    """Transit time between an origin and destination zip. [admin]"""
    return _handle(daylight.get_transit_times, orig_zip, dest_zip)


@daylight_router.get("/trace/booking/{booking_number}")
async def daylight_trace_booking(booking_number: str, _: bool = Depends(require_admin)):
    """Shipment tracing for a Daylight booking number. [admin]"""
    return _handle(daylight.trace_booking, booking_number)


@daylight_router.get("/trace/{probill}")
async def daylight_trace(probill: str, _: bool = Depends(require_admin)):
    """Shipment tracing for a Daylight probill (PRO, 8-10 digits). [admin]"""
    return _handle(daylight.trace, probill)


# ---------- Phase 2 (MyDaylight-authenticated) ----------

@daylight_router.post("/rate-quote")
async def daylight_rate_quote(payload: dict = Body(...), _: bool = Depends(require_admin)):
    """
    Raw rateQuote. Body = the dyltRateQuoteReq fields (WITHOUT accountNumber/userName/password;
    those are merged server-side). Returns Daylight's dyltRateQuoteResp. [admin]
    """
    return _handle(daylight.rate_quote, payload)


@daylight_router.post("/pickup")
async def daylight_pickup(payload: dict = Body(...), _: bool = Depends(require_admin)):
    """Raw pickup request. Body = dyltPickupReq fields (auth merged server-side). [admin]"""
    return _handle(daylight.schedule_pickup, payload)


@daylight_router.post("/bol")
async def daylight_bol(payload: dict = Body(...), _: bool = Depends(require_admin)):
    """
    Raw image/bol. Body = dyltImageReq fields (auth merged server-side). On success Daylight
    returns a PDF; this endpoint returns its size + a base64 copy (or the JSON error). [admin]
    """
    result = _handle(daylight.create_bol, payload)
    if isinstance(result, dict) and result.get("pdf_bytes") is not None:
        raw = result["pdf_bytes"]
        return {
            "is_pdf": raw[:4] == b"%PDF",
            "size": len(raw),
            "content_type": result.get("content_type"),
            "pdf_base64": base64.b64encode(raw).decode(),
        }
    return result
