"""
lifecycle_routes.py
FastAPI router for Order Lifecycle endpoints.

Mount in main.py with:
    from lifecycle_routes import lifecycle_router
    app.include_router(lifecycle_router)

Endpoints:
    POST /lifecycle/check-all         — Daily cron: evaluate all orders
    POST /lifecycle/check/{order_id}  — Check single order
    POST /lifecycle/extend/{order_id} — Extend deadline (customer response)
    POST /lifecycle/cancel/{order_id} — Cancel order
    GET  /lifecycle/summary           — Dashboard counts by status
    GET  /lifecycle/orders            — List orders by lifecycle status
"""

from fastapi import APIRouter, HTTPException, Query
from typing import Optional
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
async def check_all_lifecycle():
    """
    CRON ENDPOINT: Evaluate all active orders against lifecycle timeline.
    
    Run daily via Render cron job or external scheduler.
    Checks each order's last_customer_email_at against the 7/30/45 day
    thresholds and updates lifecycle_status accordingly.
    
    Also queues reminder emails (actual sending is Phase 4).
    
    Returns summary of all actions taken.
    """
    try:
        result = check_all_orders_lifecycle()
        quote_result = check_pending_quote_reminders()
        return {"success": True, **result, "quote_reminders": quote_result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lifecycle engine error: {str(e)}")


@lifecycle_router.post("/check/{order_id}")
async def check_order_lifecycle(order_id: str):
    """Check lifecycle status for a single order."""
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
async def extend_order_deadline(order_id: str, days: int = 7):
    """
    Extend lifecycle deadline for an order.
    
    Called when a customer responds to an email about their order.
    Per William's rules: customer response adds +7 days to all timers.
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
async def cancel_order_endpoint(order_id: str, reason: str = "manual"):
    """
    Cancel an order via lifecycle system.
    
    Reasons:
      - 'customer_request': Customer said "cancel" in email
      - 'lifecycle_auto_cancel': Day 45 auto-cancellation
      - 'manual': Admin manually canceled
    
    Sets lifecycle_status to 'canceled'. Phase 4 will add B2BWave API cancel
    and confirmation email sending.
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
async def lifecycle_summary():
    """
    Get dashboard summary counts by lifecycle status.
    
    Returns counts for: active, inactive, archived, canceled.
    Used by the frontend tab badges.
    """
    try:
        summary = get_lifecycle_summary()
        return {"success": True, **summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching summary: {str(e)}")


@lifecycle_router.get("/orders")
async def list_lifecycle_orders(
    status: Optional[str] = Query(None, description="Filter by lifecycle status: active, inactive, archived, canceled"),
    limit: int = Query(100, ge=1, le=500),
):
    """
    List orders filtered by lifecycle status.
    
    Used by the Inactive and Archived tabs in the frontend.
    Includes days_inactive, next_deadline, and reminder status.
    """
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
                
                # Convert decimals for JSON
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
