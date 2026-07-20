"""
daylight_routes.py
FastAPI router for the Daylight Transport carrier module — Phase 1 (read-only).

All endpoints require admin token (X-Admin-Token header).

Mount in main.py with:
    from daylight_routes import daylight_router
    app.include_router(daylight_router)

Endpoints:
    GET /daylight/token-check                     — prove OAuth handshake (no token leaked) [admin]
    GET /daylight/fuel-surcharge                  — current fuel surcharge rate             [admin]
    GET /daylight/transit/{orig_zip}/{dest_zip}   — transit time between zips               [admin]
    GET /daylight/trace/booking/{booking_number}  — trace by booking number                 [admin]
    GET /daylight/trace/{probill}                 — trace by probill (PRO)                   [admin]
"""

from fastapi import APIRouter, Depends, HTTPException
from auth import require_admin
import daylight

daylight_router = APIRouter(prefix="/daylight", tags=["daylight"])


def _handle(fn, *args):
    try:
        return fn(*args)
    except daylight.DaylightAPIError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Daylight API error ({e.status_code}): {e.message}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Daylight error: {e}")


@daylight_router.get("/token-check")
async def daylight_token_check(_: bool = Depends(require_admin)):
    """Prove the OAuth handshake works. Never returns the token itself. [admin]"""
    return _handle(daylight.token_check)


@daylight_router.get("/fuel-surcharge")
async def daylight_fuel_surcharge(_: bool = Depends(require_admin)):
    """Live fuel surcharge rate + discount (proves a real API call works). [admin]"""
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
    """Shipment tracing for a Daylight probill (PRO). [admin]"""
    return _handle(daylight.trace, probill)
