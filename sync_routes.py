"""
sync_routes.py
FastAPI router for B2BWave, Gmail, and Square sync endpoints.

Phase 5B: Extracted from main.py

All endpoints require admin token (X-Admin-Token: <token> header),
except GET /square/status which is public (no sensitive data).

Mount in main.py with:
    from sync_routes import sync_router
    app.include_router(sync_router)

Endpoints:
    GET  /b2bwave/test             — test B2BWave API connection
    POST /b2bwave/sync             — pull orders from B2BWave (last N days)
    GET  /b2bwave/order/{order_id} — sync a specific order from B2BWave
    POST /gmail/sync               — pull order updates from Gmail
    POST /square/sync              — pull payments from Square
    GET  /square/status            — check Square config (public)
    POST /orders/regenerate-summaries — regenerate AI 6-bullet summaries for active orders
"""

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Depends

from auth import require_admin
from config import B2BWAVE_URL, B2BWAVE_USERNAME, B2BWAVE_API_KEY
from db_helpers import get_db

try:
    from sync_service import b2bwave_api_request, sync_order_from_b2bwave, refresh_ai_summaries_for_active_orders
    SYNC_SERVICE_LOADED = True
except ImportError:
    SYNC_SERVICE_LOADED = False
    def refresh_ai_summaries_for_active_orders():
        pass

try:
    from gmail_sync import run_gmail_sync, gmail_configured
except ImportError:
    def run_gmail_sync(conn, hours_back=2):
        return {"status": "disabled", "reason": "module_not_found"}

    def gmail_configured():
        return False


try:
    from square_sync import run_square_sync, square_configured
except ImportError:
    def run_square_sync(conn, hours_back=24):
        return {"status": "disabled", "reason": "module_not_found"}

    def square_configured():
        return False


sync_router = APIRouter(tags=["sync"])


# =============================================================================
# B2BWAVE
# =============================================================================

@sync_router.get("/b2bwave/test")
def test_b2bwave(_: bool = Depends(require_admin)):
    """Test B2BWave API connection."""
    if not B2BWAVE_URL or not B2BWAVE_USERNAME or not B2BWAVE_API_KEY:
        return {
            "status": "error",
            "message": "B2BWave API not configured",
            "config": {
                "url_set": bool(B2BWAVE_URL),
                "username_set": bool(B2BWAVE_USERNAME),
                "api_key_set": bool(B2BWAVE_API_KEY),
            },
        }
    if not SYNC_SERVICE_LOADED:
        return {"status": "error", "message": "sync_service module not loaded"}
    try:
        data = b2bwave_api_request("orders", {"submitted_at_gteq": "2024-01-01"})
        order_count = len(data) if isinstance(data, list) else 1
        return {
            "status": "ok",
            "message": f"B2BWave API connected. Found {order_count} orders.",
            "url": B2BWAVE_URL,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@sync_router.post("/b2bwave/sync")
def sync_from_b2bwave(days_back: int = 14, _: bool = Depends(require_admin)):
    """Sync orders from B2BWave API (last 14 days default)."""
    if not SYNC_SERVICE_LOADED:
        raise HTTPException(status_code=503, detail="sync_service module not loaded")

    since_date = (
        datetime.now(timezone.utc) - timedelta(days=days_back)
    ).strftime("%Y-%m-%d")

    try:
        data = b2bwave_api_request("orders", {"submitted_at_gteq": since_date})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"B2BWave API error: {str(e)}")

    orders_list = data if isinstance(data, list) else [data]
    synced = []
    errors = []

    for order_data in orders_list:
        try:
            result = sync_order_from_b2bwave(order_data)
            synced.append(result)
        except Exception as e:
            order_id = order_data.get("order", order_data).get("id", "unknown")
            errors.append({"order_id": order_id, "error": str(e)})

    return {
        "status": "ok",
        "synced_count": len(synced),
        "error_count": len(errors),
        "synced_orders": synced,
        "errors": errors if errors else None,
    }


@sync_router.get("/b2bwave/order/{order_id}")
def get_b2bwave_order(order_id: str, _: bool = Depends(require_admin)):
    """Fetch a specific order from B2BWave and sync it."""
    if not SYNC_SERVICE_LOADED:
        raise HTTPException(status_code=503, detail="sync_service module not loaded")
    try:
        data = b2bwave_api_request("orders", {"id_eq": order_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"B2BWave API error: {str(e)}")

    if not data:
        raise HTTPException(status_code=404, detail="Order not found in B2BWave")

    order_data = data[0] if isinstance(data, list) else data
    result = sync_order_from_b2bwave(order_data)

    return {
        "status": "ok",
        "message": f"Order {order_id} synced from B2BWave",
        "order": result,
    }


# =============================================================================
# GMAIL / SQUARE
# =============================================================================

@sync_router.post("/gmail/sync")
def sync_from_gmail(hours_back: int = 2, _: bool = Depends(require_admin)):
    """Sync order status updates from Gmail (last 2 hours default)."""
    if not gmail_configured():
        raise HTTPException(
            status_code=400,
            detail="Gmail not configured. Set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN.",
        )
    try:
        with get_db() as conn:
            results = run_gmail_sync(conn, hours_back=hours_back)
        return {"status": "ok", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gmail sync error: {str(e)}")


@sync_router.post("/square/sync")
def sync_from_square(hours_back: int = 24, _: bool = Depends(require_admin)):
    """Sync payments from Square API (last 24 hours default)."""
    if not square_configured():
        raise HTTPException(
            status_code=400,
            detail="Square not configured. Set SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID.",
        )
    try:
        with get_db() as conn:
            results = run_square_sync(conn, hours_back=hours_back)
        return {"status": "ok", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Square sync error: {str(e)}")


@sync_router.get("/square/status")
def square_status():
    """Check Square API configuration status (public — no sensitive data)."""
    return {
        "configured": square_configured(),
        "message": (
            "Square API configured"
            if square_configured()
            else "Set SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID environment variables"
        ),
    }


# =============================================================================
# AI SUMMARIES
# =============================================================================

@sync_router.post("/orders/regenerate-summaries")
def regenerate_ai_summaries(_: bool = Depends(require_admin)):
    """
    Trigger immediate AI summary regeneration for all active orders.
    Same logic as the auto-sync refresh but on-demand. [admin]
    Runs synchronously — may take 30-60s for many orders.
    """
    try:
        refresh_ai_summaries_for_active_orders()
        return {"status": "ok", "message": "AI summaries regenerated for active orders"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI summary refresh error: {str(e)}")
