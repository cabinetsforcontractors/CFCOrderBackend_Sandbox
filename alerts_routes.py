"""
alerts_routes.py
FastAPI router for AlertsEngine endpoints.

Mount in main.py with:
    from alerts_routes import alerts_router
    app.include_router(alerts_router)
"""

from fastapi import APIRouter, HTTPException
from alerts_engine import check_all_orders, check_order_alerts, get_alert_summary
from db_helpers import get_order_alerts, resolve_alert

alerts_router = APIRouter(prefix="/alerts", tags=["alerts"])


@alerts_router.post("/check-all")
async def check_all_alerts():
    """
    CRON ENDPOINT: Check all active orders for alert conditions.
    
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
async def check_order(order_id: str):
    """Check alert rules for a single order."""
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
async def alert_summary():
    """Get summary of all unresolved alerts grouped by type."""
    try:
        summary = get_alert_summary()
        return {
            "success": True,
            **summary,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching summary: {str(e)}")


@alerts_router.get("/")
async def list_alerts(order_id: str = None, include_resolved: bool = False):
    """List alerts, optionally filtered by order."""
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
async def resolve_alert_endpoint(alert_id: int):
    """Manually resolve an alert."""
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
