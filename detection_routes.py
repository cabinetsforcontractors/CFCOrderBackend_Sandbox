"""
detection_routes.py
FastAPI router for email parsing, payment detection, and alert checks.

Phase 5B: Extracted from main.py

All endpoints require admin token (X-Admin-Token: <token> header).

Mount in main.py with:
    from detection_routes import detection_router
    app.include_router(detection_router)

Endpoints:
    POST /parse-email               — parse B2BWave order email, create/update order
    POST /detect-payment-link       — flag Square payment link in email
    POST /detect-payment-received   — match Square payment notification to order
    POST /detect-rl-quote           — capture R+L quote number from email
    POST /detect-pro-number         — capture R+L PRO tracking number from email
    POST /check-payment-alerts      — alert on trusted customers shipped-but-unpaid 1+ day
"""

import re
import json
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from psycopg2.extras import RealDictCursor

from auth import require_admin
from db_helpers import get_db
from orders_routes import is_trusted_customer

try:
    from email_parser import parse_b2bwave_email, get_warehouses_for_skus
    EMAIL_PARSER_LOADED = True
except ImportError:
    EMAIL_PARSER_LOADED = False

    def parse_b2bwave_email(body, subject):
        return {"order_id": None}

    def get_warehouses_for_skus(prefixes):
        return []


detection_router = APIRouter(tags=["detection"])


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class ParseEmailRequest(BaseModel):
    email_body: str
    email_subject: str
    email_date: Optional[str] = None
    email_thread_id: Optional[str] = None


class ParseEmailResponse(BaseModel):
    status: str
    order_id: Optional[str] = None
    parsed_data: Optional[dict] = None
    warehouses: Optional[List[str]] = None
    message: Optional[str] = None


# =============================================================================
# EMAIL PARSING
# =============================================================================

@detection_router.post("/parse-email", response_model=ParseEmailResponse)
def parse_email(request: ParseEmailRequest, _: bool = Depends(require_admin)):
    """Parse a B2BWave order email and create or update the order."""
    if not EMAIL_PARSER_LOADED:
        return ParseEmailResponse(status="error", message="email_parser module not loaded")

    parsed = parse_b2bwave_email(request.email_body, request.email_subject)

    if not parsed["order_id"]:
        return ParseEmailResponse(
            status="error",
            message="Could not extract order ID from email",
        )

    warehouses = get_warehouses_for_skus(parsed.get("sku_prefixes", []))

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT order_id FROM orders WHERE order_id = %s", (parsed["order_id"],)
            )
            exists = cur.fetchone()

            if exists:
                cur.execute(
                    """
                    UPDATE orders SET
                        customer_name = COALESCE(%s, customer_name),
                        company_name  = COALESCE(%s, company_name),
                        email         = COALESCE(%s, email),
                        phone         = COALESCE(%s, phone),
                        street        = COALESCE(%s, street),
                        city          = COALESCE(%s, city),
                        state         = COALESCE(%s, state),
                        zip_code      = COALESCE(%s, zip_code),
                        order_total   = COALESCE(%s, order_total),
                        comments      = COALESCE(%s, comments),
                        warehouse_1   = COALESCE(%s, warehouse_1),
                        warehouse_2   = COALESCE(%s, warehouse_2),
                        updated_at    = NOW()
                    WHERE order_id = %s
                    """,
                    (
                        parsed["customer_name"], parsed["company_name"],
                        parsed["email"], parsed["phone"],
                        parsed["street"], parsed["city"],
                        parsed["state"], parsed["zip_code"],
                        parsed["order_total"], parsed["comments"],
                        warehouses[0] if len(warehouses) > 0 else None,
                        warehouses[1] if len(warehouses) > 1 else None,
                        parsed["order_id"],
                    ),
                )
                return ParseEmailResponse(
                    status="updated",
                    order_id=parsed["order_id"],
                    parsed_data=parsed,
                    warehouses=warehouses,
                    message="Order updated",
                )
            else:
                order_date = request.email_date or datetime.now(timezone.utc).isoformat()
                trusted = is_trusted_customer(
                    conn,
                    parsed["customer_name"] or "",
                    parsed["company_name"] or "",
                )

                cur.execute(
                    """
                    INSERT INTO orders (
                        order_id, customer_name, company_name, email, phone,
                        street, city, state, zip_code,
                        order_date, order_total, comments,
                        warehouse_1, warehouse_2, email_thread_id,
                        is_trusted_customer
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        parsed["order_id"], parsed["customer_name"], parsed["company_name"],
                        parsed["email"], parsed["phone"],
                        parsed["street"], parsed["city"], parsed["state"], parsed["zip_code"],
                        order_date, parsed["order_total"], parsed["comments"],
                        warehouses[0] if len(warehouses) > 0 else None,
                        warehouses[1] if len(warehouses) > 1 else None,
                        request.email_thread_id, trusted,
                    ),
                )

                cur.execute(
                    """
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'order_created', %s, 'email_parse')
                    """,
                    (parsed["order_id"], json.dumps(parsed)),
                )

                return ParseEmailResponse(
                    status="created",
                    order_id=parsed["order_id"],
                    parsed_data=parsed,
                    warehouses=warehouses,
                    message="Order created",
                )


# =============================================================================
# PAYMENT DETECTION
# =============================================================================

@detection_router.post("/detect-payment-link")
def detect_payment_link(order_id: str, email_body: str, _: bool = Depends(require_admin)):
    """Detect if email body contains a Square payment link and flag the order."""
    if "square.link" in email_body.lower():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE orders SET
                        payment_link_sent    = TRUE,
                        payment_link_sent_at = NOW(),
                        updated_at           = NOW()
                    WHERE order_id = %s AND NOT payment_link_sent
                    """,
                    (order_id,),
                )
                if cur.rowcount > 0:
                    cur.execute(
                        """
                        INSERT INTO order_events (order_id, event_type, source)
                        VALUES (%s, 'payment_link_sent', 'email_detection')
                        """,
                        (order_id,),
                    )
                    return {"status": "ok", "updated": True}
        return {"status": "ok", "updated": False, "message": "Already marked"}

    return {"status": "ok", "updated": False, "message": "No square link found"}


@detection_router.post("/detect-payment-received")
def detect_payment_received(
    email_subject: str, email_body: str, _: bool = Depends(require_admin)
):
    """
    Detect Square payment notification and match to open order.
    Expected subject format: "$4,913.99 payment received from Dylan Gentry"
    """
    amount_match = re.search(
        r"\$([\d,]+\.?\d*)\s+payment received", email_subject, re.IGNORECASE
    )
    if not amount_match:
        return {"status": "ok", "updated": False, "message": "Not a payment notification"}

    payment_amount = float(amount_match.group(1).replace(",", ""))

    name_match = re.search(r"payment received from (.+)$", email_subject, re.IGNORECASE)
    customer_name = name_match.group(1).strip() if name_match else None

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT order_id, order_total, customer_name
                FROM orders
                WHERE NOT payment_received
                  AND order_total IS NOT NULL
                ORDER BY order_date DESC
                LIMIT 50
                """
            )
            orders = cur.fetchall()
            matched_order = None

            for order in orders:
                if order["order_total"] and payment_amount >= float(order["order_total"]):
                    if customer_name and order["customer_name"]:
                        pay_first = customer_name.split()[0].lower()
                        order_first = order["customer_name"].split()[0].lower()
                        if pay_first == order_first:
                            matched_order = order
                            break
                    elif not matched_order:
                        matched_order = order

            if matched_order:
                order_total = float(matched_order["order_total"]) if matched_order["order_total"] else 0
                shipping_cost = payment_amount - order_total if order_total else None

                cur.execute(
                    """
                    UPDATE orders SET
                        payment_received    = TRUE,
                        payment_received_at = NOW(),
                        payment_amount      = %s,
                        shipping_cost       = %s,
                        updated_at          = NOW()
                    WHERE order_id = %s
                    """,
                    (payment_amount, shipping_cost, matched_order["order_id"]),
                )
                cur.execute(
                    """
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'payment_received', %s, 'square_notification')
                    """,
                    (
                        matched_order["order_id"],
                        json.dumps({
                            "payment_amount": payment_amount,
                            "shipping_cost": shipping_cost,
                            "customer_name": customer_name,
                        }),
                    ),
                )
                return {
                    "status": "ok",
                    "updated": True,
                    "order_id": matched_order["order_id"],
                    "payment_amount": payment_amount,
                    "shipping_cost": shipping_cost,
                }

            return {
                "status": "ok",
                "updated": False,
                "message": "Could not match payment to order",
                "payment_amount": payment_amount,
                "customer_name": customer_name,
            }


# =============================================================================
# R+L QUOTE / PRO NUMBER DETECTION
# =============================================================================

@detection_router.post("/detect-rl-quote")
def detect_rl_quote(order_id: str, email_body: str, _: bool = Depends(require_admin)):
    """Detect R+L quote number from email body and attach to order."""
    quote_match = re.search(
        r"(?:RL\s+)?Quote\s*(?:No|#)?[:\s]*(\d{6,10})", email_body, re.IGNORECASE
    )
    if quote_match:
        quote_no = quote_match.group(1)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE orders SET rl_quote_no = %s, updated_at = NOW() WHERE order_id = %s",
                    (quote_no, order_id),
                )
                cur.execute(
                    """
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'rl_quote_captured', %s, 'email_detection')
                    """,
                    (order_id, json.dumps({"quote_no": quote_no})),
                )
        return {"status": "ok", "quote_no": quote_no}
    return {"status": "ok", "quote_no": None, "message": "No quote number found"}


@detection_router.post("/detect-pro-number")
def detect_pro_number(order_id: str, email_body: str, _: bool = Depends(require_admin)):
    """Detect R+L PRO tracking number from email body and attach to order."""
    pro_match = re.search(
        r"PRO\s*(?:#|Number)?[:\s]*([A-Z]{0,2}\d{8,10}(?:-\d)?)",
        email_body,
        re.IGNORECASE,
    )
    if pro_match:
        pro_no = pro_match.group(1).upper()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE orders SET
                        pro_number = %s,
                        tracking   = %s,
                        updated_at = NOW()
                    WHERE order_id = %s
                    """,
                    (pro_no, f"R+L PRO {pro_no}", order_id),
                )
                cur.execute(
                    """
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'pro_number_captured', %s, 'email_detection')
                    """,
                    (order_id, json.dumps({"pro_number": pro_no})),
                )
        return {"status": "ok", "pro_number": pro_no}
    return {"status": "ok", "pro_number": None, "message": "No PRO number found"}


# =============================================================================
# TRUSTED CUSTOMER ALERT CHECK
# =============================================================================

@detection_router.post("/check-payment-alerts")
def check_payment_alerts(_: bool = Depends(require_admin)):
    """
    Create alerts for trusted customers who shipped but haven't paid after 1 day.
    Run daily via Render cron or external scheduler.
    """
    alerts_created = 0

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT o.order_id, o.customer_name, o.order_total
                FROM orders o
                WHERE o.sent_to_warehouse = TRUE
                  AND o.payment_received  = FALSE
                  AND o.is_trusted_customer = TRUE
                  AND o.sent_to_warehouse_at < NOW() - INTERVAL '1 day'
                  AND NOT EXISTS (
                      SELECT 1 FROM order_alerts a
                      WHERE a.order_id = o.order_id
                        AND a.alert_type = 'trusted_unpaid'
                        AND NOT a.is_resolved
                  )
                """
            )
            orders = cur.fetchall()

            for order in orders:
                cur.execute(
                    """
                    INSERT INTO order_alerts (order_id, alert_type, alert_message)
                    VALUES (%s, 'trusted_unpaid', %s)
                    """,
                    (
                        order["order_id"],
                        f"Trusted customer {order['customer_name']} "
                        f"— shipped but unpaid for 1+ day. Total: ${order['order_total']}",
                    ),
                )
                alerts_created += 1

    return {"status": "ok", "alerts_created": alerts_created}
