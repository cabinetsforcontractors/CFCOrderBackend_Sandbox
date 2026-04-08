"""
alerts_routes.py
FastAPI router for AlertsEngine endpoints.

All POST endpoints (cron triggers, resolve) require admin token.
GET endpoints (summary, list) are also admin-protected.

Mount in main.py with:
    from alerts_routes import alerts_router
    app.include_router(alerts_router)
"""

from fastapi import APIRouter, Depends, HTTPException
from auth import require_admin
from alerts_engine import check_all_orders, check_order_alerts, get_alert_summary
from db_helpers import get_order_alerts, resolve_alert

alerts_router = APIRouter(prefix="/alerts", tags=["alerts"])


@alerts_router.post("/check-all")
async def check_all_alerts(_: bool = Depends(require_admin)):
    """
    CRON ENDPOINT: Check all active orders for alert conditions. [admin]

    Run daily via Render cron job or external scheduler.
    Evaluates all 8 ORD-A1 rules against every active order.
    Creates new alerts when thresholds exceeded.
    Auto-resolves alerts when conditions no longer apply.

    Returns summary of actions taken.
    """
    try:
        result = check_all_orders()
        return {
            "success": True,
            **result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AlertsEngine error: {str(e)}")


@alerts_router.post("/check/{order_id}")
async def check_order(order_id: str, _: bool = Depends(require_admin)):
    """Check alert rules for a single order. [admin]"""
    try:
        alerts = check_order_alerts(order_id)
        return {
            "success": True,
            "order_id": order_id,
            "alerts_created": [a for a in alerts if a.get("action") == "created"],
            "alerts_resolved": [a for a in alerts if a.get("action") == "resolved"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking order {order_id}: {str(e)}")


@alerts_router.get("/summary")
async def alert_summary(_: bool = Depends(require_admin)):
    """Get summary of all unresolved alerts grouped by type. [admin]"""
    try:
        summary = get_alert_summary()
        return {
            "success": True,
            **summary,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching summary: {str(e)}")


@alerts_router.get("/")
async def list_alerts(order_id: str = None, include_resolved: bool = False,
                      _: bool = Depends(require_admin)):
    """List alerts, optionally filtered by order. [admin]"""
    try:
        alerts = get_order_alerts(order_id=order_id, include_resolved=include_resolved)
        return {
            "success": True,
            "count": len(alerts),
            "alerts": alerts,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching alerts: {str(e)}")


@alerts_router.post("/{alert_id}/resolve")
async def resolve_alert_endpoint(alert_id: int, _: bool = Depends(require_admin)):
    """Manually resolve an alert. [admin]"""
    try:
        resolved = resolve_alert(alert_id)
        if not resolved:
            raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found or already resolved")
        return {
            "success": True,
            "alert_id": alert_id,
            "status": "resolved",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error resolving alert: {str(e)}")


# =============================================================================
# TRACKING CHECK — cron endpoint
# =============================================================================

@alerts_router.post("/tracking/check-all")
async def check_tracking(_: bool = Depends(require_admin)):
    """
    CRON ENDPOINT: Poll R+L ShipmentTracing for all BOL'd orders. [admin]

    Sends customer tracking email when first scan detected (freight moving).
    Run every 2-4 hours. Safe to run frequently — skips already-sent orders.

    Returns summary: checked, tracking_emails_sent, not_yet_moving, errors.
    """
    try:
        from supplier_polling_engine import check_tracking_updates
        result = check_tracking_updates()
        return {
            "success": True,
            **result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tracking check error: {str(e)}")


# =============================================================================
# PICKUP CONFIRMATION CHECK — cron endpoint
# =============================================================================

@alerts_router.post("/pickup/check-confirmations")
async def check_pickup_confirmations(_: bool = Depends(require_admin)):
    """
    CRON ENDPOINT: Ask supplier 'Has the customer picked up?' [admin]

    Fires daily after pickup_ready_date has passed.
    Safe to run frequently — skips shipments where poll already sent.

    Returns summary: checked, polls_sent, errors.
    """
    try:
        from pickup_polling_engine import check_pickup_confirmations as _check
        result = _check()
        return {
            "success": True,
            **result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pickup confirmation check error: {str(e)}")
