"""
lifecycle_routes.py
FastAPI router for Order Lifecycle endpoints.

All endpoints require admin token (X-Admin-Token header).

Mount in main.py with:
    from lifecycle_routes import lifecycle_router
    app.include_router(lifecycle_router)

Endpoints:
    POST /lifecycle/check-all         — Daily cron: evaluate all orders      [admin]
    POST /lifecycle/check/{order_id}  — Check single order                   [admin]
    POST /lifecycle/extend/{order_id} — Extend deadline (customer response)  [admin]
    POST /lifecycle/cancel/{order_id} — Cancel order                         [admin]
    GET  /lifecycle/summary           — Dashboard counts by status            [admin]
    GET  /lifecycle/orders            — List orders by lifecycle status       [admin]
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from auth import require_admin
from lifecycle_engine import (
    check_all_orders_lifecycle,
    check_pending_quote_reminders,
    process_order_lifecycle,
    extend_deadline,
    cancel_order,
    get_lifecycle_summary,
    STATUS_ACTIVE, STATUS_INACTIVE, STATUS_ARCHIVED, STATUS_CANCELED,
)
from db_helpers import get_db
from psycopg2.extras import RealDictCursor

lifecycle_router = APIRouter(prefix="/lifecycle", tags=["lifecycle"])


@lifecycle_router.post("/check-all")
async def check_all_lifecycle(_: bool = Depends(require_admin)):
    """
    CRON ENDPOINT: Evaluate all active orders against lifecycle timeline. [admin]

    Run daily via Render cron job or external scheduler.
    Also runs check_pending_quote_reminders() for unpaid quotes after 3 days.

    Returns summary of all actions taken.
    """
    try:
        result = check_all_orders_lifecycle()
        quote_result = check_pending_quote_reminders()
        return {"success": True, **result, "quote_reminders": quote_result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lifecycle engine error: {str(e)}")


@lifecycle_router.post("/check/{order_id}")
async def check_order_lifecycle(order_id: str, _: bool = Depends(require_admin)):
    """Check lifecycle status for a single order. [admin]"""
    try:
        result = process_order_lifecycle(order_id)
        if result.get("error"):
            raise HTTPException(status_code=404, detail=result["error"])
        return {"success": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking order {order_id}: {str(e)}")


@lifecycle_router.post("/extend/{order_id}")
async def extend_order_deadline(order_id: str, days: int = 7,
                                _: bool = Depends(require_admin)):
    """
    Extend lifecycle deadline for an order. [admin]

    Called when a customer responds to an email about their order.
    Resets lifecycle_status back to 'active' and clears sent reminders.
    """
    try:
        result = extend_deadline(order_id, days=days)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error", "Unknown error"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error extending deadline: {str(e)}")


@lifecycle_router.post("/cancel/{order_id}")
async def cancel_order_endpoint(order_id: str, reason: str = "manual",
                                _: bool = Depends(require_admin)):
    """
    Cancel an order via lifecycle system. [admin]

    Reasons: customer_request | lifecycle_auto_cancel | manual
    """
    try:
        result = cancel_order(order_id, reason=reason)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error", "Unknown error"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error canceling order: {str(e)}")


@lifecycle_router.get("/summary")
async def lifecycle_summary(_: bool = Depends(require_admin)):
    """Get dashboard summary counts by lifecycle status. [admin]"""
    try:
        summary = get_lifecycle_summary()
        return {"success": True, **summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching summary: {str(e)}")


@lifecycle_router.get("/orders")
async def list_lifecycle_orders(
    status: Optional[str] = Query(None, description="Filter: active, inactive, archived, canceled"),
    limit: int = Query(100, ge=1, le=500),
    _: bool = Depends(require_admin),
):
    """List orders filtered by lifecycle status. [admin]"""
    valid_statuses = [STATUS_ACTIVE, STATUS_INACTIVE, STATUS_ARCHIVED, STATUS_CANCELED]
    if status and status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {valid_statuses}"
        )

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = """
                    SELECT o.order_id, o.customer_name, o.company_name,
                           o.email, o.order_total, o.order_date,
                           o.last_customer_email_at,
                           COALESCE(o.lifecycle_status, 'active') as lifecycle_status,
                           o.lifecycle_deadline_at,
                           o.lifecycle_reminders_sent,
                           s.current_status,
                           EXTRACT(DAY FROM NOW() - COALESCE(o.last_customer_email_at, o.order_date))::INTEGER as days_inactive
                    FROM orders o
                    LEFT JOIN order_status s ON o.order_id = s.order_id
                    WHERE (o.is_complete = FALSE OR o.is_complete IS NULL)
                """
                params = []

                if status:
                    if status == STATUS_ACTIVE:
                        query += " AND (o.lifecycle_status IS NULL OR o.lifecycle_status = %s)"
                    else:
                        query += " AND o.lifecycle_status = %s"
                    params.append(status)

                query += " ORDER BY o.last_customer_email_at ASC NULLS FIRST LIMIT %s"
                params.append(limit)

                cur.execute(query, params)
                orders = cur.fetchall()

                for order in orders:
                    if order.get("order_total"):
                        order["order_total"] = float(order["order_total"])

                return {
                    "success": True,
                    "count": len(orders),
                    "status_filter": status,
                    "orders": orders,
                }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing orders: {str(e)}")
