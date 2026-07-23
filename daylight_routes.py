"""
daylight_routes.py
FastAPI router for the Daylight Transport carrier module.

All endpoints require admin token (X-Admin-Token header).

Phase 1 (OAuth only): config-check, token-check, fuel-surcharge, transit, trace.
Phase 2 (OAuth + MyDaylight): rate-quote, bol, pickup  (raw pass-through test endpoints;
each takes the inner request fields as the JSON body -- account auth is merged server-side).
Order integration (2026-07-23): order-quote, order-bol -- auto-assemble the Phase-2
bodies straight from an order id (engine in daylight_order.py).
Tracking (step 2, 2026-07-23): probill registry + externalTrace delivery poller
(engine in daylight_tracking.py; the poller also rides every progress sweep).

Mount in main.py:
    from daylight_routes import daylight_router
    app.include_router(daylight_router)
"""

import base64
from typing import Optional

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


# ---------- Order integration (daylight_order.py, 2026-07-23) ----------

@daylight_router.get("/order-quote/{order_id}")
async def daylight_order_quote(
    order_id: str,
    residential: Optional[bool] = None,
    liftgate: bool = False,
    warehouse: Optional[str] = None,
    pickup_date: Optional[str] = None,
    assemble_only: bool = False,
    _: bool = Depends(require_admin),
):
    """
    Auto-build a Daylight rateQuote per eligible leg of an order and (unless
    assemble_only=true) fire it. Omit `residential` to auto-detect via Smarty.
    Non-CA legs are refused with a note. Nothing is committed anywhere. [admin]
    """
    from daylight_order import order_quote
    return _handle(lambda: order_quote(
        order_id, residential=residential, liftgate=liftgate, warehouse=warehouse,
        pickup_date=pickup_date, execute=not assemble_only))


@daylight_router.post("/order-bol/{order_id}")
async def daylight_order_bol(
    order_id: str,
    warehouse: Optional[str] = None,
    bol_date: Optional[str] = None,
    bill_terms: str = "Collect",
    residential: Optional[bool] = None,
    liftgate: bool = False,
    assemble_only: bool = False,
    _: bool = Depends(require_admin),
):
    """
    Auto-build the Daylight BOL (dyltImageReq) for ONE Daylight-eligible leg of an
    order and (unless assemble_only=true) fire it. Pass ?warehouse= when the order
    has several eligible legs. Returns the assembled fields + the PDF (base64) in
    the same shape as the raw /daylight/bol endpoint. Hits whatever base URL
    daylight.py is configured with (TEST until the prod flip). [admin]
    """
    from daylight_order import order_bol
    result = _handle(lambda: order_bol(
        order_id, warehouse=warehouse, bol_date=bol_date, bill_terms=bill_terms,
        residential=residential, liftgate=liftgate, execute=not assemble_only))
    pdf = result.pop("pdf", None) if isinstance(result, dict) else None
    if isinstance(pdf, dict) and pdf.get("pdf_bytes") is not None:
        raw = pdf["pdf_bytes"]
        result["is_pdf"] = raw[:4] == b"%PDF"
        result["size"] = len(raw)
        result["content_type"] = pdf.get("content_type")
        result["pdf_base64"] = base64.b64encode(raw).decode()
    elif pdf is not None:
        result["bol_response"] = pdf  # JSON error body from Daylight
    return result


# ---------- Tracking / delivery poller (daylight_tracking.py, step 2) ----------

@daylight_router.post("/probill/{order_id}")
async def daylight_register_probill(
    order_id: str,
    probill: str,
    warehouse: Optional[str] = None,
    stamp_tracking: bool = True,
    force_stamp: bool = False,
    _: bool = Depends(require_admin),
):
    """
    Register a Daylight PRO (8-10 digits — NOT the BOL number) for an order.
    Stamps orders.tracking only-if-empty, which arms the existing progress sweep
    to draft the customer tracking email (draft-first). stamp_tracking=false
    registers for polling without touching tracking. [admin]
    """
    from daylight_tracking import register_probill
    return _handle(lambda: register_probill(
        order_id, probill, warehouse=warehouse, stamp_tracking=stamp_tracking,
        force_stamp=force_stamp))


@daylight_router.get("/shipments")
async def daylight_shipments(_: bool = Depends(require_admin)):
    """Daylight shipment registry: status, poll dates, delivered flags. [admin]"""
    from daylight_tracking import list_shipments
    return _handle(list_shipments)


@daylight_router.delete("/shipments/{probill}")
async def daylight_remove_shipment(probill: str, _: bool = Depends(require_admin)):
    """Remove a registry row (drill cleanup / mis-entered PRO). Does not touch
    orders.tracking. [admin]"""
    from daylight_tracking import remove_shipment
    return _handle(lambda: remove_shipment(probill))


@daylight_router.post("/poll")
async def daylight_poll(force: bool = False, _: bool = Depends(require_admin)):
    """Run the Daylight delivery poller now. force=true ignores the morning /
    once-a-day gates (drills). Customer touches are DRAFTS only. [admin]"""
    from daylight_tracking import poll_daylight_shipments
    return _handle(lambda: poll_daylight_shipments(force=force))
