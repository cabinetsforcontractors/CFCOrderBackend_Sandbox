"""
orders_routes.py
FastAPI router for Order CRUD, Shipment Management, Warehouse Mapping,
Trusted Customers, and the is_trusted_customer helper.

Phase 5: Extracted from main.py (was ~900 lines inline)
Phase 5C: require_admin wired to all write/delete endpoints
Phase 5C: run-check + reactivate endpoints added

Mount in main.py with:
    from orders_routes import orders_router, is_trusted_customer
    app.include_router(orders_router)

Endpoints:
    GET    /orders                                  — list orders
    GET    /orders/status/summary                   — counts by status
    GET    /orders/{order_id}                       — single order
    POST   /orders/{order_id}/generate-summary      — short AI summary
    POST   /orders/{order_id}/comprehensive-summary — full AI summary
    POST   /orders/{order_id}/add-email-snippet     — store email snippet
    GET    /orders/{order_id}/supplier-sheet-data   — per-warehouse sheet data
    PATCH  /orders/{order_id}                       — update order fields          [admin]
    PATCH  /orders/{order_id}/checkpoint            — mark checkpoint done         [admin]
    PATCH  /orders/{order_id}/set-status            — set full workflow status     [admin]
    GET    /orders/{order_id}/shipments             — per-order shipments
    GET    /orders/{order_id}/events                — event history
    DELETE /orders/{order_id}                       — delete order                 [admin]
    POST   /orders/{order_id}/run-check             — trigger lifecycle check      [admin]
    POST   /orders/{order_id}/reactivate            — reactivate inactive order    [admin]

    GET    /shipments                               — all shipments (with order info)
    PATCH  /shipments/{shipment_id}                 — update shipment fields       [admin]
    GET    /shipments/{shipment_id}/rl-quote-data   — pre-fill RL quote data

    GET    /warehouse-mapping                       — all SKU→warehouse mappings
    POST   /warehouse-mapping                       — add/update mapping           [admin]

    GET    /trusted-customers                       — list trusted customers
    POST   /trusted-customers                       — add trusted customer         [admin]
    DELETE /trusted-customers/{customer_id}         — remove trusted customer      [admin]
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from psycopg2.extras import RealDictCursor

from auth import require_admin
from db_helpers import get_db
from config import SUPPLIER_INFO, WAREHOUSE_ZIPS, OVERSIZED_KEYWORDS

try:
    from ai_summary import generate_order_summary, generate_comprehensive_summary
    AI_SUMMARY_LOADED = True
except ImportError:
    AI_SUMMARY_LOADED = False

    def generate_order_summary(order_id):
        return "AI summary not available (ai_summary module not loaded)"

    def generate_comprehensive_summary(order_id):
        return "AI summary not available (ai_summary module not loaded)"


# =============================================================================
# ROUTER
# =============================================================================

orders_router = APIRouter(tags=["orders"])


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class OrderUpdate(BaseModel):
    customer_name: Optional[str] = None
    company_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    order_total: Optional[float] = None
    comments: Optional[str] = None
    notes: Optional[str] = None
    tracking: Optional[str] = None
    supplier_order_no: Optional[str] = None
    warehouse_1: Optional[str] = None
    warehouse_2: Optional[str] = None


class CheckpointUpdate(BaseModel):
    checkpoint: str  # payment_link_sent | payment_received | sent_to_warehouse | warehouse_confirmed | bol_sent | is_complete
    source: Optional[str] = "api"
    payment_amount: Optional[float] = None


class WarehouseMappingUpdate(BaseModel):
    sku_prefix: str
    warehouse_name: str
    warehouse_code: Optional[str] = None


# =============================================================================
# HELPER
# =============================================================================

def is_trusted_customer(conn, customer_name: str, company_name: str = None) -> bool:
    """Check if customer is in the trusted_customers table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM trusted_customers
            WHERE LOWER(customer_name) = LOWER(%s)
               OR (company_name IS NOT NULL AND LOWER(company_name) = LOWER(%s))
            """,
            (customer_name, company_name or ""),
        )
        return cur.fetchone() is not None


# =============================================================================
# ORDER CRUD
# =============================================================================

@orders_router.get("/orders")
def list_orders(
    status: Optional[str] = None,
    include_complete: bool = False,
    limit: int = 200,
):
    """List orders with optional filters, including shipments."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT o.*, s.current_status, s.days_open
                FROM orders o
                JOIN order_status s ON o.order_id = s.order_id
                WHERE 1=1
            """
            params = []

            if not include_complete:
                query += " AND NOT o.is_complete"

            if status:
                query += " AND s.current_status = %s"
                params.append(status)

            query += " ORDER BY o.order_date DESC LIMIT %s"
            params.append(limit)

            cur.execute(query, params)
            orders = cur.fetchall()

            order_ids = [o["order_id"] for o in orders]
            shipments_by_order = {}
            if order_ids:
                cur.execute(
                    """
                    SELECT * FROM order_shipments
                    WHERE order_id = ANY(%s)
                    ORDER BY warehouse
                    """,
                    (order_ids,),
                )
                for ship in cur.fetchall():
                    oid = ship["order_id"]
                    if oid not in shipments_by_order:
                        shipments_by_order[oid] = []
                    if ship.get("weight"):
                        ship["weight"] = float(ship["weight"])
                    shipments_by_order[oid].append(dict(ship))

            for order in orders:
                for key in ["order_total", "payment_amount", "shipping_cost"]:
                    if order.get(key):
                        order[key] = float(order[key])
                order["shipments"] = shipments_by_order.get(order["order_id"], [])

            return {"status": "ok", "count": len(orders), "orders": orders}


@orders_router.get("/orders/status/summary")
def status_summary():
    """Get count of orders by status."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT current_status, COUNT(*) as count
                FROM order_status
                GROUP BY current_status
                ORDER BY
                    CASE current_status
                        WHEN 'needs_payment_link' THEN 1
                        WHEN 'awaiting_payment' THEN 2
                        WHEN 'needs_warehouse_order' THEN 3
                        WHEN 'awaiting_warehouse' THEN 4
                        WHEN 'needs_bol' THEN 5
                        WHEN 'awaiting_shipment' THEN 6
                        WHEN 'complete' THEN 7
                    END
                """
            )
            summary = cur.fetchall()
            return {"status": "ok", "summary": summary}


@orders_router.get("/orders/{order_id}")
def get_order(order_id: str):
    """Get single order details."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT o.*, s.current_status, s.days_open
                FROM orders o
                JOIN order_status s ON o.order_id = s.order_id
                WHERE o.order_id = %s
                """,
                (order_id,),
            )
            order = cur.fetchone()

            if not order:
                raise HTTPException(status_code=404, detail="Order not found")

            for key in ["order_total", "payment_amount", "shipping_cost"]:
                if order.get(key):
                    order[key] = float(order[key])

            return {"status": "ok", "order": order}


@orders_router.post("/orders/{order_id}/generate-summary")
def generate_summary_endpoint(order_id: str, force: bool = False):
    """
    Generate SHORT AI summary for order card display.
    If force=False and summary exists and is less than 1 hour old, returns cached.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT ai_summary, ai_summary_updated_at FROM orders WHERE order_id = %s",
                (order_id,),
            )
            order = cur.fetchone()

            if not order:
                raise HTTPException(status_code=404, detail="Order not found")

            if (
                not force
                and order.get("ai_summary")
                and order.get("ai_summary_updated_at")
            ):
                age = datetime.now(timezone.utc) - order["ai_summary_updated_at"]
                if age < timedelta(hours=1):
                    return {
                        "status": "ok",
                        "summary": order["ai_summary"],
                        "cached": True,
                        "updated_at": order["ai_summary_updated_at"].isoformat(),
                    }

    summary = generate_order_summary(order_id)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE orders
                SET ai_summary = %s, ai_summary_updated_at = NOW(), updated_at = NOW()
                WHERE order_id = %s
                """,
                (summary, order_id),
            )

    return {
        "status": "ok",
        "summary": summary,
        "cached": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@orders_router.post("/orders/{order_id}/comprehensive-summary")
def generate_comprehensive_summary_endpoint(order_id: str, force: bool = False):
    """Generate COMPREHENSIVE AI summary for order popup — full history analysis."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT order_id FROM orders WHERE order_id = %s", (order_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Order not found")

    summary = generate_comprehensive_summary(order_id)

    return {
        "status": "ok",
        "summary": summary,
        "cached": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@orders_router.post("/orders/{order_id}/add-email-snippet")
def add_email_snippet(
    order_id: str,
    email_from: str,
    email_subject: str,
    email_snippet: str,
    email_date: Optional[str] = None,
    snippet_type: str = "general",
):
    """Add an email snippet for an order (called by Google Script)."""
    with get_db() as conn:
        with conn.cursor() as cur:
            parsed_date = None
            if email_date:
                try:
                    parsed_date = datetime.fromisoformat(email_date.replace("Z", "+00:00"))
                except Exception:
                    parsed_date = datetime.now(timezone.utc)
            else:
                parsed_date = datetime.now(timezone.utc)

            cur.execute(
                """
                INSERT INTO order_email_snippets
                (order_id, email_from, email_subject, email_snippet, email_date, snippet_type)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    order_id,
                    email_from,
                    email_subject,
                    email_snippet[:1000],
                    parsed_date,
                    snippet_type,
                ),
            )

    return {"status": "ok", "message": "Email snippet added"}


@orders_router.get("/orders/{order_id}/supplier-sheet-data")
def get_supplier_sheet_data(order_id: str):
    """Get order data organized by warehouse for supplier sheet generation."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()

            if not order:
                raise HTTPException(status_code=404, detail="Order not found")

            cur.execute(
                "SELECT * FROM order_line_items WHERE order_id = %s", (order_id,)
            )
            line_items = cur.fetchall()

    customer_name = order.get("customer_name") or ""
    company_name = order.get("company_name") or ""
    customer_display = company_name if company_name else customer_name
    if company_name and customer_name:
        customer_display = f"{company_name} ({customer_name})"

    street = order.get("street") or ""
    street2 = order.get("street2") or ""
    city = order.get("city") or ""
    state = order.get("state") or ""
    zip_code = order.get("zip_code") or ""
    phone = order.get("phone") or ""
    email = order.get("email") or ""

    address_parts = [street]
    if street2:
        address_parts.append(street2)
    address_parts.append(f"{city}, {state} {zip_code}")
    customer_address = ", ".join(filter(None, address_parts))

    comments = order.get("comments") or ""

    warehouses = {}
    for item in line_items:
        wh = item.get("warehouse") or "Unknown"
        if wh not in warehouses:
            supplier_info = SUPPLIER_INFO.get(
                wh, {"name": wh, "address": "", "contact": "", "email": ""}
            )
            warehouses[wh] = {
                "supplier_name": supplier_info["name"],
                "supplier_address": supplier_info["address"],
                "supplier_contact": supplier_info["contact"],
                "supplier_email": supplier_info["email"],
                "items": [],
            }
        warehouses[wh]["items"].append(
            {
                "quantity": item.get("quantity") or 1,
                "product_code": item.get("sku") or "",
                "product_name": item.get("product_name") or "",
            }
        )

    return {
        "status": "ok",
        "order_id": order_id,
        "customer_name": customer_display,
        "customer_address": customer_address,
        "customer_phone": phone,
        "customer_email": email,
        "comments": comments,
        "warehouses": warehouses,
    }


@orders_router.patch("/orders/{order_id}")
def update_order(order_id: str, update: OrderUpdate, _: bool = Depends(require_admin)):
    """Update order fields. [admin]"""
    with get_db() as conn:
        with conn.cursor() as cur:
            fields = []
            values = []

            for field, value in update.dict(exclude_unset=True).items():
                if value is not None:
                    fields.append(f"{field} = %s")
                    values.append(value)

            if not fields:
                raise HTTPException(status_code=400, detail="No fields to update")

            fields.append("updated_at = NOW()")
            values.append(order_id)

            query = f"UPDATE orders SET {', '.join(fields)} WHERE order_id = %s"
            cur.execute(query, values)

            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Order not found")

            return {"status": "ok", "message": "Order updated"}


@orders_router.patch("/orders/{order_id}/checkpoint")
def update_checkpoint(order_id: str, update: CheckpointUpdate, _: bool = Depends(require_admin)):
    """Update order checkpoint. [admin]"""
    valid_checkpoints = [
        "payment_link_sent",
        "payment_received",
        "sent_to_warehouse",
        "warehouse_confirmed",
        "bol_sent",
        "is_complete",
    ]

    if update.checkpoint not in valid_checkpoints:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid checkpoint. Must be one of: {valid_checkpoints}",
        )

    with get_db() as conn:
        with conn.cursor() as cur:
            timestamp_field = (
                f"{update.checkpoint}_at"
                if update.checkpoint != "is_complete"
                else "completed_at"
            )

            set_parts = [
                f"{update.checkpoint} = TRUE",
                f"{timestamp_field} = NOW()",
                "updated_at = NOW()",
            ]
            params = []

            if update.checkpoint == "payment_received" and update.payment_amount:
                set_parts.append("payment_amount = %s")
                params.append(update.payment_amount)

                cur.execute(
                    "SELECT order_total FROM orders WHERE order_id = %s", (order_id,)
                )
                row = cur.fetchone()
                if row and row[0]:
                    shipping = update.payment_amount - float(row[0])
                    set_parts.append("shipping_cost = %s")
                    params.append(shipping)

            params.append(order_id)

            query = f"UPDATE orders SET {', '.join(set_parts)} WHERE order_id = %s"
            cur.execute(query, params)

            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Order not found")

            cur.execute(
                """
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    order_id,
                    update.checkpoint,
                    json.dumps(
                        {"payment_amount": update.payment_amount}
                        if update.payment_amount
                        else {}
                    ),
                    update.source,
                ),
            )

            return {"status": "ok", "checkpoint": update.checkpoint}


@orders_router.patch("/orders/{order_id}/set-status")
def set_order_status(order_id: str, status: str, source: str = "web_ui", _: bool = Depends(require_admin)):
    """
    Set order to a specific status by resetting all checkpoints and setting appropriate ones.
    Allows moving orders backwards in the workflow. [admin]
    """
    status_checkpoints = {
        "needs_payment_link": {},
        "awaiting_payment": {"payment_link_sent": True},
        "needs_warehouse_order": {
            "payment_link_sent": True,
            "payment_received": True,
        },
        "awaiting_warehouse": {
            "payment_link_sent": True,
            "payment_received": True,
            "sent_to_warehouse": True,
        },
        "needs_bol": {
            "payment_link_sent": True,
            "payment_received": True,
            "sent_to_warehouse": True,
            "warehouse_confirmed": True,
        },
        "awaiting_shipment": {
            "payment_link_sent": True,
            "payment_received": True,
            "sent_to_warehouse": True,
            "warehouse_confirmed": True,
            "bol_sent": True,
        },
        "complete": {
            "payment_link_sent": True,
            "payment_received": True,
            "sent_to_warehouse": True,
            "warehouse_confirmed": True,
            "bol_sent": True,
            "is_complete": True,
        },
    }

    if status not in status_checkpoints:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    checkpoints = status_checkpoints[status]

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE orders SET
                    payment_link_sent = %s,
                    payment_received = %s,
                    sent_to_warehouse = %s,
                    warehouse_confirmed = %s,
                    bol_sent = %s,
                    is_complete = %s,
                    updated_at = NOW()
                WHERE order_id = %s
                """,
                (
                    checkpoints.get("payment_link_sent", False),
                    checkpoints.get("payment_received", False),
                    checkpoints.get("sent_to_warehouse", False),
                    checkpoints.get("warehouse_confirmed", False),
                    checkpoints.get("bol_sent", False),
                    checkpoints.get("is_complete", False),
                    order_id,
                ),
            )

            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Order not found")

            cur.execute(
                """
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'status_change', %s, %s)
                """,
                (order_id, json.dumps({"new_status": status}), source),
            )

            return {"status": "ok", "new_status": status}


@orders_router.get("/orders/{order_id}/shipments")
def get_order_shipments(order_id: str):
    """Get all shipments for an order."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM order_shipments
                WHERE order_id = %s
                ORDER BY warehouse
                """,
                (order_id,),
            )
            shipments = cur.fetchall()
            return {"status": "ok", "shipments": shipments}


@orders_router.get("/orders/{order_id}/events")
def get_order_events(order_id: str):
    """Get event history for an order."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM order_events
                WHERE order_id = %s
                ORDER BY created_at DESC
                """,
                (order_id,),
            )
            events = cur.fetchall()
            return {"status": "ok", "events": events}


@orders_router.delete("/orders/{order_id}")
def delete_order(order_id: str, _: bool = Depends(require_admin)):
    """Delete an order and its shipments. [admin]"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM order_shipments WHERE order_id = %s", (order_id,)
            )
            cur.execute(
                "DELETE FROM order_line_items WHERE order_id = %s", (order_id,)
            )
            cur.execute("DELETE FROM orders WHERE order_id = %s", (order_id,))
            conn.commit()
    return {"status": "ok", "message": f"Order {order_id} deleted"}


@orders_router.post("/orders/{order_id}/run-check")
def run_lifecycle_check(order_id: str, _: bool = Depends(require_admin)):
    """
    Trigger lifecycle check for a single order. [admin]
    Evaluates inactivity thresholds and sends reminder emails if due.
    Wraps lifecycle_engine.process_order_lifecycle().
    """
    try:
        from lifecycle_engine import process_order_lifecycle
        result = process_order_lifecycle(order_id)
        if result.get("error"):
            raise HTTPException(status_code=404, detail=result["error"])
        return {"status": "ok", **result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lifecycle check error: {str(e)}")


@orders_router.post("/orders/{order_id}/reactivate")
def reactivate_order(order_id: str, _: bool = Depends(require_admin)):
    """
    Reactivate an inactive order — resets lifecycle clock to now. [admin]
    Sets last_customer_email_at = NOW(), lifecycle_status = active,
    clears all sent reminders so they re-fire from new baseline.
    Wraps lifecycle_engine.extend_deadline().
    """
    try:
        from lifecycle_engine import extend_deadline
        result = extend_deadline(order_id)
        if not result.get("success"):
            raise HTTPException(
                status_code=404,
                detail=result.get("error", "Order not found"),
            )
        return {"status": "ok", **result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reactivate error: {str(e)}")


# =============================================================================
# SHIPMENT MANAGEMENT
# =============================================================================

@orders_router.get("/shipments")
def list_all_shipments(include_complete: bool = False):
    """List all shipments with order info."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT s.*, o.customer_name, o.company_name, o.order_date,
                       o.street, o.street2, o.city, o.state, o.zip_code, o.phone,
                       o.payment_received, o.order_total
                FROM order_shipments s
                JOIN orders o ON s.order_id = o.order_id
                WHERE 1=1
            """
            if not include_complete:
                query += " AND s.status != 'delivered'"
            query += " ORDER BY o.order_date DESC, s.warehouse"

            cur.execute(query)
            shipments = cur.fetchall()

            for s in shipments:
                if s.get("order_total"):
                    s["order_total"] = float(s["order_total"])
                if s.get("weight"):
                    s["weight"] = float(s["weight"])

            return {"status": "ok", "count": len(shipments), "shipments": shipments}


@orders_router.patch("/shipments/{shipment_id}")
def update_shipment(
    shipment_id: str,
    status: Optional[str] = None,
    tracking: Optional[str] = None,
    pro_number: Optional[str] = None,
    weight: Optional[float] = None,
    ship_method: Optional[str] = None,
    bol_sent: Optional[bool] = None,
    origin_zip: Optional[str] = None,
    rl_quote_number: Optional[str] = None,
    rl_quote_price: Optional[float] = None,
    rl_customer_price: Optional[float] = None,
    rl_invoice_amount: Optional[float] = None,
    has_oversized: Optional[bool] = None,
    li_quote_price: Optional[float] = None,
    li_customer_price: Optional[float] = None,
    actual_cost: Optional[float] = None,
    quote_url: Optional[str] = None,
    ps_quote_url: Optional[str] = None,
    ps_quote_price: Optional[float] = None,
    tracking_number: Optional[str] = None,
    quote_price: Optional[float] = None,
    customer_price: Optional[float] = None,
    _: bool = Depends(require_admin),
):
    """Update shipment fields. [admin]"""
    valid_statuses = [
        "needs_order",
        "at_warehouse",
        "needs_bol",
        "ready_ship",
        "shipped",
        "delivered",
    ]
    if status and status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {valid_statuses}",
        )

    # Shippo added — all valid shipping methods
    valid_methods = ["LTL", "Shippo", "Pirateship", "Pickup", "BoxTruck", "LiDelivery", "Manual", None]
    if ship_method and ship_method not in valid_methods:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ship_method. Must be one of: {valid_methods}",
        )

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            updates = []
            params = []

            if status is not None:
                updates.append("status = %s")
                params.append(status)
                if status == "at_warehouse":
                    updates.append("sent_to_warehouse_at = NOW()")
                elif status == "needs_bol":
                    updates.append("warehouse_confirmed_at = NOW()")
                elif status == "shipped":
                    updates.append("shipped_at = NOW()")
                elif status == "delivered":
                    updates.append("delivered_at = NOW()")

            if tracking is not None:
                updates.append("tracking = %s"); params.append(tracking)
            if pro_number is not None:
                updates.append("pro_number = %s"); params.append(pro_number)
            if weight is not None:
                updates.append("weight = %s"); params.append(weight)
            if ship_method is not None:
                updates.append("ship_method = %s"); params.append(ship_method)
            if bol_sent is not None:
                updates.append("bol_sent = %s"); params.append(bol_sent)
                if bol_sent:
                    updates.append("bol_sent_at = NOW()")
            if origin_zip is not None:
                updates.append("origin_zip = %s"); params.append(origin_zip)
            if rl_quote_number is not None:
                updates.append("rl_quote_number = %s"); params.append(rl_quote_number)
            if rl_quote_price is not None:
                updates.append("rl_quote_price = %s"); params.append(rl_quote_price)
            if rl_customer_price is not None:
                updates.append("rl_customer_price = %s"); params.append(rl_customer_price)
            if rl_invoice_amount is not None:
                updates.append("rl_invoice_amount = %s"); params.append(rl_invoice_amount)
            if has_oversized is not None:
                updates.append("has_oversized = %s"); params.append(has_oversized)
            if li_quote_price is not None:
                updates.append("li_quote_price = %s"); params.append(li_quote_price)
            if li_customer_price is not None:
                updates.append("li_customer_price = %s"); params.append(li_customer_price)
            if actual_cost is not None:
                updates.append("actual_cost = %s"); params.append(actual_cost)
            if quote_url is not None:
                updates.append("quote_url = %s"); params.append(quote_url)
            if ps_quote_url is not None:
                updates.append("ps_quote_url = %s"); params.append(ps_quote_url)
            if ps_quote_price is not None:
                updates.append("ps_quote_price = %s"); params.append(ps_quote_price)
            if tracking_number is not None:
                updates.append("tracking_number = %s"); params.append(tracking_number)
            if quote_price is not None:
                updates.append("quote_price = %s"); params.append(quote_price)
            if customer_price is not None:
                updates.append("customer_price = %s"); params.append(customer_price)

            if not updates:
                return {"status": "ok", "message": "No updates provided"}

            updates.append("updated_at = NOW()")
            params.append(shipment_id)

            query = (
                f"UPDATE order_shipments SET {', '.join(updates)} "
                f"WHERE shipment_id = %s RETURNING *"
            )
            cur.execute(query, params)

            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=404, detail="Shipment not found")

            # Auto-complete order if all shipments delivered
            cur.execute(
                """
                SELECT COUNT(*) as total,
                       COUNT(*) FILTER (WHERE status = 'delivered') as delivered
                FROM order_shipments
                WHERE order_id = %s
                """,
                (result["order_id"],),
            )
            counts = cur.fetchone()
            if counts["total"] > 0 and counts["total"] == counts["delivered"]:
                cur.execute(
                    """
                    UPDATE orders
                    SET is_complete = TRUE, completed_at = NOW(), updated_at = NOW()
                    WHERE order_id = %s
                    """,
                    (result["order_id"],),
                )

            return {"status": "ok", "shipment": dict(result)}


@orders_router.get("/shipments/{shipment_id}/rl-quote-data")
def get_rl_quote_data(shipment_id: str):
    """Get pre-populated data for RL Carriers quote."""
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT s.*, o.customer_name, o.company_name, o.street, o.city,
                           o.state, o.zip_code, o.phone, o.email,
                           o.order_total, o.total_weight
                    FROM order_shipments s
                    JOIN orders o ON s.order_id = o.order_id
                    WHERE s.shipment_id = %s
                    """,
                    (shipment_id,),
                )
                shipment = cur.fetchone()
                if not shipment:
                    return {"status": "error", "message": f"Shipment {shipment_id} not found"}

                warehouse = shipment["warehouse"]
                origin_zip = WAREHOUSE_ZIPS.get(warehouse, "")

                if not origin_zip:
                    wh_lower = (
                        warehouse.lower()
                        .replace(" ", "")
                        .replace("&", "")
                        .replace("-", "")
                    )
                    for wh_name, wh_zip in WAREHOUSE_ZIPS.items():
                        wh_cmp = (
                            wh_name.lower()
                            .replace(" ", "")
                            .replace("&", "")
                            .replace("-", "")
                        )
                        if (
                            wh_cmp == wh_lower
                            or wh_lower in wh_cmp
                            or wh_cmp in wh_lower
                        ):
                            origin_zip = wh_zip
                            break

                cur.execute(
                    """
                    SELECT sku, product_name, quantity
                    FROM order_line_items
                    WHERE order_id = %s AND warehouse = %s
                    """,
                    (shipment["order_id"], warehouse),
                )
                line_items = cur.fetchall()

                has_oversized_flag = False
                oversized_items = []
                for item in line_items:
                    desc = (item.get("product_name") or "").upper()
                    if any(kw in desc for kw in OVERSIZED_KEYWORDS):
                        has_oversized_flag = True
                        oversized_items.append(
                            f"{item.get('sku')}: {item.get('product_name')}"
                        )

                cur.execute(
                    """
                    SELECT COUNT(DISTINCT warehouse) as warehouse_count
                    FROM order_line_items
                    WHERE order_id = %s AND warehouse IS NOT NULL
                    """,
                    (shipment["order_id"],),
                )
                wh_count = cur.fetchone()
                is_single_warehouse = wh_count and wh_count["warehouse_count"] <= 1

                order_weight = (
                    float(shipment["total_weight"])
                    if shipment.get("total_weight")
                    else 0
                )

                dest_zip = shipment.get("zip_code") or ""
                if "-" in dest_zip:
                    dest_zip = dest_zip.split("-")[0]
                dest_zip = dest_zip[:5]

                shipment_weight = (
                    float(shipment["weight"]) if shipment.get("weight") else None
                )
                needs_manual = False
                weight_note = None
                weight_allocation = None

                if shipment_weight:
                    weight_note = "from shipment"
                elif is_single_warehouse and order_weight > 0:
                    shipment_weight = round(order_weight, 1)
                    weight_note = "from order"
                elif not is_single_warehouse:
                    # Sales-based weight allocation (TEMPORARY — not production-ready)
                    # ⚠️ Real per-item weights needed from Lane C (WS5) before this is accurate.
                    # Uses price * quantity as fallback when line_total is null (sync_service gap).
                    if order_weight > 0:
                        cur.execute(
                            """
                            SELECT warehouse,
                                   SUM(COALESCE(line_total, price * quantity, 0)) AS warehouse_total
                            FROM order_line_items
                            WHERE order_id = %s AND warehouse IS NOT NULL
                            GROUP BY warehouse
                            """,
                            (shipment["order_id"],),
                        )
                        wh_totals = {
                            r["warehouse"]: float(r["warehouse_total"])
                            for r in cur.fetchall()
                        }
                        order_sales_total = sum(wh_totals.values())
                        this_wh_sales = wh_totals.get(warehouse, 0)
                        if order_sales_total > 0:
                            pct = this_wh_sales / order_sales_total
                            allocated_weight = round(order_weight * pct, 1)
                            weight_allocation = {
                                "this_warehouse_sales": round(this_wh_sales, 2),
                                "order_sales_total": round(order_sales_total, 2),
                                "pct": round(pct * 100, 2),
                                "allocated_weight": allocated_weight,
                            }
                            shipment_weight = allocated_weight
                            weight_note = (
                                f"Sales-allocated ({round(pct * 100, 1)}% of order)"
                                f" \u26a0\ufe0f Not production-ready \u2014 verify before use"
                            )
                        else:
                            needs_manual = True
                            weight_note = "Multi-warehouse \u2014 enter weight for this shipment"
                    else:
                        needs_manual = True
                        weight_note = "Multi-warehouse \u2014 no total weight on order"
                else:
                    needs_manual = True
                    weight_note = "No weight data available"

                return {
                    "status": "ok",
                    "shipment_id": shipment_id,
                    "order_id": shipment["order_id"],
                    "warehouse": warehouse,
                    "origin_zip": origin_zip,
                    "destination": {
                        "name": shipment.get("company_name")
                        or shipment.get("customer_name")
                        or "",
                        "street": shipment.get("street") or "",
                        "city": shipment.get("city") or "",
                        "state": shipment.get("state") or "",
                        "zip": dest_zip,
                        "email": shipment.get("email") or "",
                        "phone": shipment.get("phone") or "",
                    },
                    "weight": {
                        "value": shipment_weight,
                        "note": weight_note,
                        "needs_manual_entry": needs_manual,
                        "allocation": weight_allocation,
                    },
                    "oversized": {
                        "detected": has_oversized_flag,
                        "items": oversized_items,
                    },
                    "existing_quote": {
                        "quote_number": shipment.get("rl_quote_number"),
                        "quote_price": float(shipment["rl_quote_price"])
                        if shipment.get("rl_quote_price")
                        else None,
                        "customer_price": float(shipment["rl_customer_price"])
                        if shipment.get("rl_customer_price")
                        else None,
                        "quote_url": shipment.get("quote_url"),
                    },
                    "rl_quote_url": "https://www.rlcarriers.com/freight/shipping/rate-quote",
                }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# =============================================================================
# WAREHOUSE MAPPING
# =============================================================================

@orders_router.get("/warehouse-mapping")
def get_warehouse_mapping():
    """Get all warehouse mappings."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM warehouse_mapping ORDER BY sku_prefix")
            mappings = cur.fetchall()
            return {"status": "ok", "mappings": mappings}


@orders_router.post("/warehouse-mapping")
def add_warehouse_mapping(mapping: WarehouseMappingUpdate, _: bool = Depends(require_admin)):
    """Add or update warehouse mapping. [admin]"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO warehouse_mapping (sku_prefix, warehouse_name, warehouse_code)
                VALUES (%s, %s, %s)
                ON CONFLICT (sku_prefix) DO UPDATE SET
                    warehouse_name = EXCLUDED.warehouse_name,
                    warehouse_code = EXCLUDED.warehouse_code
                """,
                (
                    mapping.sku_prefix.upper(),
                    mapping.warehouse_name,
                    mapping.warehouse_code,
                ),
            )
            return {"status": "ok", "message": "Mapping saved"}


# =============================================================================
# TRUSTED CUSTOMERS
# =============================================================================

@orders_router.get("/trusted-customers")
def list_trusted_customers():
    """List all trusted customers."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM trusted_customers ORDER BY customer_name")
            customers = cur.fetchall()
            return {"status": "ok", "customers": customers}


@orders_router.post("/trusted-customers")
def add_trusted_customer(
    customer_name: str,
    company_name: Optional[str] = None,
    notes: Optional[str] = None,
    _: bool = Depends(require_admin),
):
    """Add a trusted customer. [admin]"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trusted_customers (customer_name, company_name, notes)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (customer_name, company_name, notes),
            )
            new_id = cur.fetchone()[0]
            return {"status": "ok", "id": new_id}


@orders_router.delete("/trusted-customers/{customer_id}")
def remove_trusted_customer(customer_id: int, _: bool = Depends(require_admin)):
    """Remove a trusted customer. [admin]"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM trusted_customers WHERE id = %s", (customer_id,)
            )
            return {"status": "ok"}
