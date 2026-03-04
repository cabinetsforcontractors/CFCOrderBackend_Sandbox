"""
CFC Order Workflow Backend - v6.0.0
Refactored with helper modules for better maintainability.

Phase 5 Backend Hardening (2026-03-04):
  - CORS whitelist: locked to known frontends (allow_origins=[\"*\"] removed)
  - orders_routes.py  : all /orders /shipments /warehouse-mapping /trusted-customers
  - shipping_routes.py: all /rl /shippo /rta
  - auth.py           : JWT-ready admin token dependency (apply with Depends(require_admin))

Remaining in main.py (Phase 5B will extract these):
  - DB migration endpoints
  - B2BWave / Gmail / Square sync
  - Email parsing + payment detection
  - Checkout flow

Line count target: ~1,500 (down from 3,101)
"""

import os
import re
import json
import base64
import urllib.request
import urllib.error
import urllib.parse
import threading
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, List
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# =============================================================================
# IMPORT HELPER MODULES
# =============================================================================

from config import (
    DATABASE_URL, B2BWAVE_URL, B2BWAVE_USERNAME, B2BWAVE_API_KEY,
    ANTHROPIC_API_KEY, SHIPPO_API_KEY,
    AUTO_SYNC_INTERVAL_MINUTES, AUTO_SYNC_DAYS_BACK,
    SUPPLIER_INFO, WAREHOUSE_ZIPS, OVERSIZED_KEYWORDS
)

from db_helpers import get_db

try:
    from email_parser import parse_b2bwave_email, get_warehouses_for_skus
    EMAIL_PARSER_LOADED = True
except ImportError:
    EMAIL_PARSER_LOADED = False
    print("[STARTUP] email_parser module not found")

try:
    from detection import (
        detect_square_payment_link, extract_rl_quote_number,
        extract_pro_number, parse_payment_notification,
        match_payment_to_order, record_payment_received,
        record_rl_quote, record_pro_number
    )
    DETECTION_MODULE_LOADED = True
except ImportError:
    DETECTION_MODULE_LOADED = False
    print("[STARTUP] detection module not found")

try:
    from ai_summary import call_anthropic_api, generate_order_summary, generate_comprehensive_summary
    AI_SUMMARY_LOADED = True
except ImportError:
    AI_SUMMARY_LOADED = False
    print("[STARTUP] ai_summary module not found")

try:
    from db_migrations import (
        create_pending_checkouts_table as _create_pending_checkouts,
        create_shipments_table as _create_shipments,
        add_rl_shipping_fields as _add_rl_fields,
        add_ps_fields as _add_ps_fields,
        fix_shipment_columns as _fix_shipment_columns,
        fix_sku_columns as _fix_sku_columns,
        fix_order_id_length as _fix_order_id_length,
        recreate_order_status_view as _recreate_order_status_view,
        add_weight_column as _add_weight_column
    )
    DB_MIGRATIONS_LOADED = True
except ImportError:
    DB_MIGRATIONS_LOADED = False
    print("[STARTUP] db_migrations module not found")

try:
    from sync_service import (
        b2bwave_api_request, sync_order_from_b2bwave,
        start_auto_sync_thread, get_sync_status,
        is_configured as b2bwave_is_configured
    )
    SYNC_SERVICE_LOADED = True
except ImportError:
    SYNC_SERVICE_LOADED = False
    print("[STARTUP] sync_service module not found")

try:
    import b2bwave_api
    B2BWAVE_MODULE_LOADED = True
except ImportError:
    B2BWAVE_MODULE_LOADED = False
    print("[STARTUP] b2bwave_api module not found")

try:
    from gmail_sync import run_gmail_sync, gmail_configured
except ImportError:
    print("[STARTUP] gmail_sync module not found, email sync disabled")
    def run_gmail_sync(conn, hours_back=2):
        return {"status": "disabled", "reason": "module_not_found"}
    def gmail_configured():
        return False

try:
    from square_sync import run_square_sync, square_configured
except ImportError:
    print("[STARTUP] square_sync module not found, payment sync disabled")
    def run_square_sync(conn, hours_back=24):
        return {"status": "disabled", "reason": "module_not_found"}
    def square_configured():
        return False

# Phase 2: RL-Quote proxy microservice (/proxy/*)
from rl_quote_proxy import router as rl_proxy_router

# Phase 3A: AlertsEngine (/alerts/*)
try:
    from alerts_routes import alerts_router
    ALERTS_ENGINE_LOADED = True
except ImportError:
    ALERTS_ENGINE_LOADED = False
    print("[STARTUP] alerts_routes module not found, AlertsEngine disabled")

# Phase 3B+4: Lifecycle + Email + AI Config
from startup_wiring import wire_all

# Phase 5: Extracted route modules
from orders_routes import orders_router, is_trusted_customer
from shipping_routes import shipping_router

# =============================================================================
# FASTAPI APP  — Phase 5: CORS whitelist (was allow_origins=["*"])
# =============================================================================

# Add extra allowed origins via CORS_ORIGINS env var (comma-separated).
_cors_env = os.environ.get("CORS_ORIGINS", "")
_extra_origins = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else []

ALLOWED_ORIGINS = [
    "https://cfc-orders-frontend.vercel.app",
    "https://cfcorderbackend-sandbox.onrender.com",   # checkout self-reference
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
] + _extra_origins

app = FastAPI(title="CFC Order Workflow", version="6.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Phase 2: RL-Quote proxy
app.include_router(rl_proxy_router)

# Phase 3A: Alerts
if ALERTS_ENGINE_LOADED:
    app.include_router(alerts_router)

# Phase 3B+4: Lifecycle + Email + AI Config
WIRING_STATUS = wire_all(app)

# Phase 5: Orders + Shipping
app.include_router(orders_router)
app.include_router(shipping_router)

# =============================================================================
# DATABASE SCHEMA
# =============================================================================

try:
    from schema import SCHEMA_SQL
    SCHEMA_LOADED = True
except ImportError:
    SCHEMA_LOADED = False
    SCHEMA_SQL = "-- Schema not loaded"
    print("[STARTUP] schema module not found")

# =============================================================================
# PYDANTIC MODELS (only those used by endpoints remaining in this file)
# =============================================================================

class ParseEmailRequest(BaseModel):
    email_body: str
    email_subject: str
    email_date: Optional[str] = None
    email_thread_id: Optional[str] = None


class ParseEmailResponse(BaseModel):
    status: str
    order_id: Optional[str]
    parsed_data: Optional[dict]
    warehouses: Optional[List[str]]
    message: Optional[str]


class CheckoutRequest(BaseModel):
    order_id: str
    shipping_address: Optional[dict] = None


# =============================================================================
# STARTUP EVENT
# =============================================================================

@app.on_event("startup")
def start_auto_sync():
    """Start background sync thread on app startup."""
    if SYNC_SERVICE_LOADED:
        start_auto_sync_thread(run_gmail_sync, run_square_sync)
    elif B2BWAVE_URL and B2BWAVE_USERNAME and B2BWAVE_API_KEY:
        print("[AUTO-SYNC] sync_service not loaded, auto-sync disabled")
    else:
        print("[AUTO-SYNC] B2BWave not configured, auto-sync disabled")


# =============================================================================
# ROOT / HEALTH
# =============================================================================

@app.get("/")
def root():
    sync_status = get_sync_status() if SYNC_SERVICE_LOADED else {
        "enabled": False,
        "interval_minutes": AUTO_SYNC_INTERVAL_MINUTES,
        "last_sync": None,
        "running": False,
    }
    return {
        "status": "ok",
        "service": "CFC Order Workflow",
        "version": "6.0.0",
        "auto_sync": sync_status,
        "gmail_sync": {"enabled": gmail_configured()},
        "square_sync": {"enabled": square_configured()},
        "alerts_engine": {"enabled": ALERTS_ENGINE_LOADED},
        "lifecycle_engine": {"enabled": WIRING_STATUS.get("lifecycle", False)},
        "email_engine": {"enabled": WIRING_STATUS.get("email", False)},
        "ai_configure": {"enabled": WIRING_STATUS.get("ai_configure", False)},
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": "6.0.0"}


# =============================================================================
# DATABASE MIGRATION ENDPOINTS  (logic in db_migrations.py)
# Phase 5B: Move to migration_routes.py
# =============================================================================

@app.post("/create-pending-checkouts-table")
def create_pending_checkouts_table():
    """Create pending_checkouts table for B2BWave checkout flow."""
    if DB_MIGRATIONS_LOADED:
        return _create_pending_checkouts()
    return {"status": "error", "message": "db_migrations module not loaded"}


@app.post("/create-shipments-table")
def create_shipments_table():
    """Create order_shipments table."""
    if DB_MIGRATIONS_LOADED:
        return _create_shipments()
    return {"status": "error", "message": "db_migrations module not loaded"}


@app.post("/add-rl-fields")
def add_rl_shipping_fields():
    """Add R+L Carriers shipping fields."""
    if DB_MIGRATIONS_LOADED:
        return _add_rl_fields()
    return {"status": "error", "message": "db_migrations module not loaded"}


@app.post("/add-ps-fields")
def add_ps_fields():
    """Add Pirateship fields."""
    if DB_MIGRATIONS_LOADED:
        return _add_ps_fields()
    return {"status": "error", "message": "db_migrations module not loaded"}


@app.post("/fix-shipment-columns")
def fix_shipment_columns():
    """Fix column lengths in order_shipments."""
    if DB_MIGRATIONS_LOADED:
        return _fix_shipment_columns()
    return {"status": "error", "message": "db_migrations module not loaded"}


@app.post("/fix-sku-columns")
def fix_sku_columns():
    """Fix SKU column lengths."""
    if DB_MIGRATIONS_LOADED:
        return _fix_sku_columns()
    return {"status": "error", "message": "db_migrations module not loaded"}


@app.post("/fix-order-id-length")
def fix_order_id_length():
    """Increase order_id column length."""
    if DB_MIGRATIONS_LOADED:
        return _fix_order_id_length()
    return {"status": "error", "message": "db_migrations module not loaded"}


@app.post("/recreate-order-status-view")
def recreate_order_status_view():
    """Recreate the order_status view."""
    if DB_MIGRATIONS_LOADED:
        return _recreate_order_status_view()
    return {"status": "error", "message": "db_migrations module not loaded"}


@app.post("/add-weight-column")
def add_weight_column():
    """Add total_weight column."""
    if DB_MIGRATIONS_LOADED:
        return _add_weight_column()
    return {"status": "error", "message": "db_migrations module not loaded"}


@app.get("/debug/orders-columns")
def debug_orders_columns():
    """Check what columns exist in orders table."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'orders'
                ORDER BY ordinal_position
            """)
            columns = cur.fetchall()

            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'order_status'
            """)
            view_columns = cur.fetchall()

            return {
                "orders_columns": [c[0] for c in columns],
                "view_columns": [c[0] for c in view_columns]
                if view_columns
                else "view does not exist",
            }


@app.post("/init-db")
def init_db():
    """Initialize database schema (destructive!)."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
    return {"status": "ok", "message": "Database schema initialized", "version": "5.6.1"}


# =============================================================================
# B2BWAVE SYNC
# Phase 5B: Move to sync_routes.py
# =============================================================================

@app.get("/b2bwave/test")
def test_b2bwave():
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


@app.post("/b2bwave/sync")
def sync_from_b2bwave(days_back: int = 14):
    """Sync orders from B2BWave API (last 14 days default)."""
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


@app.get("/b2bwave/order/{order_id}")
def get_b2bwave_order(order_id: str):
    """Fetch a specific order from B2BWave and sync it."""
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
# GMAIL / SQUARE SYNC
# =============================================================================

@app.post("/gmail/sync")
def sync_from_gmail(hours_back: int = 2):
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


@app.post("/square/sync")
def sync_from_square(hours_back: int = 24):
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


@app.get("/square/status")
def square_status():
    """Check Square API configuration status."""
    return {
        "configured": square_configured(),
        "message": "Square API configured"
        if square_configured()
        else "Set SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID environment variables",
    }


# =============================================================================
# EMAIL PARSING
# Phase 5B: Move to detection_routes.py
# =============================================================================

@app.post("/parse-email", response_model=ParseEmailResponse)
def parse_email(request: ParseEmailRequest):
    """Parse a B2BWave order email and create/update the order."""
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
                        company_name = COALESCE(%s, company_name),
                        email = COALESCE(%s, email),
                        phone = COALESCE(%s, phone),
                        street = COALESCE(%s, street),
                        city = COALESCE(%s, city),
                        state = COALESCE(%s, state),
                        zip_code = COALESCE(%s, zip_code),
                        order_total = COALESCE(%s, order_total),
                        comments = COALESCE(%s, comments),
                        warehouse_1 = COALESCE(%s, warehouse_1),
                        warehouse_2 = COALESCE(%s, warehouse_2),
                        updated_at = NOW()
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
                    conn, parsed["customer_name"] or "", parsed["company_name"] or ""
                )

                cur.execute(
                    """
                    INSERT INTO orders (
                        order_id, customer_name, company_name, email, phone,
                        street, city, state, zip_code,
                        order_date, order_total, comments,
                        warehouse_1, warehouse_2, email_thread_id,
                        is_trusted_customer
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
# Phase 5B: Move to detection_routes.py
# =============================================================================

@app.post("/detect-payment-link")
def detect_payment_link(order_id: str, email_body: str):
    """Detect if email contains Square payment link."""
    if "square.link" in email_body.lower():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE orders SET
                        payment_link_sent = TRUE,
                        payment_link_sent_at = NOW(),
                        updated_at = NOW()
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


@app.post("/detect-payment-received")
def detect_payment_received(email_subject: str, email_body: str):
    """
    Detect Square payment notification.
    Subject format: \"$4,913.99 payment received from Dylan Gentry\"
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
                        payment_received = TRUE,
                        payment_received_at = NOW(),
                        payment_amount = %s,
                        shipping_cost = %s,
                        updated_at = NOW()
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
# RL QUOTE / PRO NUMBER DETECTION
# =============================================================================

@app.post("/detect-rl-quote")
def detect_rl_quote(order_id: str, email_body: str):
    """Detect R+L quote number from email."""
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


@app.post("/detect-pro-number")
def detect_pro_number(order_id: str, email_body: str):
    """Detect R+L PRO number from email."""
    pro_match = re.search(
        r"PRO\s*(?:#|Number)?[:\s]*([A-Z]{0,2}\d{8,10}(?:-\d)?)",
        email_body, re.IGNORECASE,
    )
    if pro_match:
        pro_no = pro_match.group(1).upper()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE orders SET pro_number = %s, tracking = %s, updated_at = NOW()
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

@app.post("/check-payment-alerts")
def check_payment_alerts():
    """
    Check for trusted customers who shipped but haven't paid after 1 business day.
    Run daily via Render cron or external scheduler.
    """
    alerts_created = 0

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT o.order_id, o.customer_name, o.company_name, o.order_total,
                       o.sent_to_warehouse_at
                FROM orders o
                WHERE o.sent_to_warehouse = TRUE
                AND o.payment_received = FALSE
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


# =============================================================================
# CHECKOUT FLOW
# Phase 5B: Move to checkout_routes.py
# =============================================================================

try:
    from checkout import (
        calculate_order_shipping, fetch_b2bwave_order,
        create_square_payment_link, generate_checkout_token,
        verify_checkout_token, WAREHOUSES,
    )
    CHECKOUT_ENABLED = True
except ImportError as e:
    print(f"[STARTUP] checkout module not found: {e}")
    CHECKOUT_ENABLED = False

CHECKOUT_BASE_URL = os.environ.get("CHECKOUT_BASE_URL", "").strip()
GMAIL_SEND_ENABLED = os.environ.get("GMAIL_SEND_ENABLED", "false").lower() == "true"


@app.get("/checkout-status")
def checkout_status():
    """Debug endpoint to check checkout configuration."""
    try:
        from checkout import B2BWAVE_URL as _URL, B2BWAVE_USERNAME as _USER, B2BWAVE_API_KEY as _KEY
        checkout_b2bwave = f"{_URL} / {_USER} / {'set' if _KEY else 'not set'}"
    except Exception:
        checkout_b2bwave = "import failed"
    return {
        "checkout_enabled": CHECKOUT_ENABLED,
        "checkout_base_url": CHECKOUT_BASE_URL or "(not set)",
        "gmail_send_enabled": GMAIL_SEND_ENABLED,
        "checkout_b2bwave_config": checkout_b2bwave,
        "main_b2bwave_url": B2BWAVE_URL or "(not set)",
    }


@app.get("/debug/b2bwave-raw/{order_id}")
def debug_b2bwave_raw(order_id: str):
    try:
        data = b2bwave_api_request("orders", {"id_eq": order_id})
        return {"status": "ok", "raw_response": data}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/debug/warehouse-routing/{order_id}")
def debug_warehouse_routing(order_id: str):
    """Debug endpoint to test warehouse routing for an order."""
    try:
        from checkout import group_items_by_warehouse, get_warehouse_for_sku, WAREHOUSES as WH

        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "error": "Order not found in B2BWave"}

        line_items = order_data.get("line_items", [])
        item_routing = []
        for item in line_items:
            sku = item.get("sku", "")
            wh = get_warehouse_for_sku(sku)
            item_routing.append({
                "sku": sku,
                "name": item.get("product_name", ""),
                "qty": item.get("quantity", 0),
                "warehouse": wh,
                "warehouse_info": WH.get(wh, {}) if wh else None,
            })

        warehouse_groups = group_items_by_warehouse(line_items)

        return {
            "status": "ok",
            "order_id": order_id,
            "customer": order_data.get("customer_name", ""),
            "total_items": len(line_items),
            "item_routing": item_routing,
            "warehouse_groups": {
                wh: {
                    "warehouse_info": WH.get(wh, {}),
                    "item_count": len(items),
                    "items": [
                        {"sku": i.get("sku"), "name": i.get("product_name"), "qty": i.get("quantity")}
                        for i in items
                    ],
                }
                for wh, items in warehouse_groups.items()
            },
        }
    except Exception as e:
        import traceback
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


@app.get("/debug/test-checkout/{order_id}")
def debug_test_checkout(order_id: str):
    """Debug endpoint to test full checkout flow without webhook."""
    try:
        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "error": "Order not found in B2BWave"}

        token = generate_checkout_token(order_id)
        shipping_address = (
            order_data.get("shipping_address")
            or order_data.get("delivery_address")
            or {}
        )
        shipping_result = calculate_order_shipping(order_data, shipping_address)
        checkout_base = os.environ.get(
            "CHECKOUT_BASE_URL", "https://cfcorderbackend-sandbox.onrender.com"
        )
        checkout_url = f"{checkout_base}/checkout-ui/{order_id}?token={token}"
        return {
            "status": "ok",
            "order_id": order_id,
            "customer": order_data.get("customer_name"),
            "customer_email": order_data.get("customer_email"),
            "token": token,
            "checkout_url": checkout_url,
            "api_url": f"{checkout_base}/checkout/{order_id}?token={token}",
            "destination": shipping_address,
            "shipping": shipping_result,
        }
    except Exception as e:
        import traceback
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


@app.post("/webhook/b2bwave-order")
def b2bwave_order_webhook(payload: dict):
    """Webhook endpoint for B2BWave — triggered when order is placed."""
    if not CHECKOUT_ENABLED:
        return {"status": "error", "message": "Checkout module not enabled"}

    order_id = payload.get("id") or payload.get("order_id")
    customer_email = payload.get("customer_email") or payload.get("email")

    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order_id")

    token = generate_checkout_token(str(order_id))
    checkout_url = f"{CHECKOUT_BASE_URL}/checkout?order={order_id}&token={token}"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pending_checkouts (order_id, customer_email, checkout_token, created_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (order_id) DO UPDATE SET
                    customer_email = EXCLUDED.customer_email,
                    checkout_token = EXCLUDED.checkout_token,
                    created_at = NOW()
                """,
                (str(order_id), customer_email, token),
            )

    return {
        "status": "ok",
        "order_id": order_id,
        "checkout_url": checkout_url,
        "message": "Checkout link generated",
    }


@app.get("/checkout/payment-complete")
def payment_complete(order: str, transactionId: Optional[str] = None):
    """Payment completion callback from Square."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_checkouts
                SET payment_completed_at = NOW(), transaction_id = %s
                WHERE order_id = %s
                """,
                (transactionId, order),
            )
            cur.execute(
                """
                UPDATE orders SET
                    payment_received = TRUE,
                    payment_received_at = NOW(),
                    payment_method = 'Square Checkout',
                    updated_at = NOW()
                WHERE order_id = %s
                """,
                (order,),
            )
    return {"status": "ok", "message": "Payment completed", "order_id": order}


@app.get("/checkout/{order_id}")
def get_checkout_data(order_id: str, token: str):
    """Get checkout page data — order details with shipping quotes."""
    if not CHECKOUT_ENABLED:
        raise HTTPException(status_code=503, detail="Checkout not enabled")
    if not verify_checkout_token(order_id, token):
        raise HTTPException(status_code=403, detail="Invalid or expired checkout link")

    order_data = fetch_b2bwave_order(order_id)
    if not order_data:
        raise HTTPException(status_code=404, detail="Order not found")

    shipping_address = (
        order_data.get("shipping_address") or order_data.get("delivery_address") or {}
    )
    shipping_result = calculate_order_shipping(order_data, shipping_address)

    return {
        "status": "ok",
        "order_id": order_id,
        "order": {
            "id": order_id,
            "customer_name": order_data.get("customer_name"),
            "customer_email": order_data.get("customer_email"),
            "company_name": order_data.get("company_name"),
            "line_items": order_data.get("line_items", []),
            "subtotal": order_data.get("subtotal") or order_data.get("total_price"),
        },
        "shipping": shipping_result,
        "payment_ready": shipping_result.get("grand_total", 0) > 0,
    }


@app.post("/checkout/{order_id}/create-payment")
def create_checkout_payment(order_id: str, token: str):
    """Create Square payment link for the order."""
    if not CHECKOUT_ENABLED:
        raise HTTPException(status_code=503, detail="Checkout not enabled")
    if not verify_checkout_token(order_id, token):
        raise HTTPException(status_code=403, detail="Invalid checkout token")

    order_data = fetch_b2bwave_order(order_id)
    if not order_data:
        raise HTTPException(status_code=404, detail="Order not found")

    shipping_address = (
        order_data.get("shipping_address") or order_data.get("delivery_address") or {}
    )
    shipping_result = calculate_order_shipping(order_data, shipping_address)
    grand_total = shipping_result.get("grand_total", 0)

    if grand_total <= 0:
        raise HTTPException(status_code=400, detail="Invalid order total")

    payment_url = create_square_payment_link(
        int(grand_total * 100), order_id, order_data.get("customer_email", "")
    )
    if not payment_url:
        raise HTTPException(status_code=500, detail="Failed to create payment link")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_checkouts
                SET payment_link = %s, payment_amount = %s, payment_initiated_at = NOW()
                WHERE order_id = %s
                """,
                (payment_url, grand_total, order_id),
            )

    return {"status": "ok", "payment_url": payment_url, "amount": grand_total}


@app.get("/checkout-ui/{order_id}")
def checkout_ui(order_id: str, token: str):
    """Serve the checkout page HTML."""
    if not verify_checkout_token(order_id, token):
        return HTMLResponse(
            content="<h1>Invalid or expired checkout link</h1>", status_code=403
        )

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Complete Your Order - CFC</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 800px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); padding: 30px; }}
        h1 {{ color: #333; margin-bottom: 20px; }}
        h2 {{ color: #555; font-size: 18px; margin: 20px 0 10px; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
        .loading {{ text-align: center; padding: 40px; color: #666; }}
        .error {{ background: #fee; color: #c00; padding: 15px; border-radius: 4px; margin: 20px 0; }}
        .item {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #f0f0f0; }}
        .item-name {{ flex: 1; }}
        .item-qty {{ width: 60px; text-align: center; color: #666; }}
        .item-price {{ width: 100px; text-align: right; font-weight: 500; }}
        .shipment {{ background: #f9f9f9; padding: 15px; border-radius: 4px; margin: 10px 0; }}
        .shipment-header {{ font-weight: 600; color: #333; margin-bottom: 10px; }}
        .shipment-detail {{ font-size: 14px; color: #666; }}
        .totals {{ margin-top: 20px; padding-top: 20px; border-top: 2px solid #333; }}
        .total-row {{ display: flex; justify-content: space-between; padding: 8px 0; }}
        .total-row.grand {{ font-size: 20px; font-weight: 700; color: #333; }}
        .pay-button {{ display: block; width: 100%; background: #0066cc; color: white; padding: 15px; border: none; border-radius: 4px; font-size: 18px; cursor: pointer; margin-top: 20px; }}
        .pay-button:hover {{ background: #0055aa; }}
        .pay-button:disabled {{ background: #ccc; cursor: not-allowed; }}
        .residential-note {{ background: #fff3cd; padding: 10px; border-radius: 4px; margin: 10px 0; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Complete Your Order</h1>
        <div id="content" class="loading">Loading order details...</div>
    </div>
    <script>
        const ORDER_ID = "{order_id}";
        const TOKEN = "{token}";
        const API_BASE = window.location.origin;
        async function loadCheckout() {{
            try {{
                const resp = await fetch(`${{API_BASE}}/checkout/${{ORDER_ID}}?token=${{TOKEN}}`);
                const data = await resp.json();
                if (data.status !== 'ok') throw new Error(data.detail || 'Failed to load order');
                renderCheckout(data);
            }} catch (err) {{
                document.getElementById('content').innerHTML = `<div class="error">Error: ${{err.message}}</div>`;
            }}
        }}
        function renderCheckout(data) {{
            const order = data.order;
            const shipping = data.shipping;
            let html = `<h2>Order #${{ORDER_ID}}</h2><p style="color:#666; margin-bottom:20px;">${{order.customer_name || ''}} ${{order.company_name ? '(' + order.company_name + ')' : ''}}</p><h2>Items</h2>`;
            (order.line_items || []).forEach(item => {{
                const price = parseFloat(item.price || item.unit_price || 0);
                const qty = parseInt(item.quantity || 1);
                html += `<div class="item"><div class="item-name">${{item.name || item.product_name || item.sku}}</div><div class="item-qty">x${{qty}}</div><div class="item-price">${{(price * qty).toFixed(2)}}</div></div>`;
            }});
            html += `<h2>Shipping</h2>`;
            if (shipping.shipments && shipping.shipments.length > 0) {{
                shipping.shipments.forEach(ship => {{
                    const quoteOk = ship.quote && ship.quote.success;
                    html += `<div class="shipment"><div class="shipment-header">\ud83d\udce6 From: ${{ship.warehouse_name}} (${{ship.origin_zip}})</div><div class="shipment-detail">${{ship.items.length}} item(s) \u00b7 ${{ship.weight}} lbs</div><div class="shipment-detail" style="margin-top:8px;">${{quoteOk ? `<strong>Shipping: $${{ship.shipping_cost.toFixed(2)}}</strong>` : `<span style="color:#c00">Quote unavailable</span>`}}</div></div>`;
                }});
                if (shipping.shipments.some(s => s.shipping_method === 'ltl')) {{
                    html += `<div class="residential-note">\ud83c\udfe0 Residential delivery includes liftgate service</div>`;
                }}
            }}
            html += `<div class="totals"><div class="total-row"><span>Items Subtotal</span><span>$${{shipping.total_items.toFixed(2)}}</span></div><div class="total-row"><span>Shipping</span><span>$${{shipping.total_shipping.toFixed(2)}}</span></div><div class="total-row grand"><span>Total</span><span>$${{shipping.grand_total.toFixed(2)}}</span></div></div><button class="pay-button" onclick="initiatePayment()" id="payBtn">Pay $${{shipping.grand_total.toFixed(2)}} with Card</button>`;
            document.getElementById('content').innerHTML = html;
        }}
        async function initiatePayment() {{
            const btn = document.getElementById('payBtn');
            btn.disabled = true; btn.textContent = 'Creating payment link...';
            try {{
                const resp = await fetch(`${{API_BASE}}/checkout/${{ORDER_ID}}/create-payment?token=${{TOKEN}}`, {{method: 'POST'}});
                const data = await resp.json();
                if (data.payment_url) {{ window.location.href = data.payment_url; }}
                else {{ throw new Error(data.detail || 'Failed to create payment'); }}
            }} catch (err) {{
                alert('Payment error: ' + err.message);
                btn.disabled = false; btn.textContent = 'Pay with Card';
            }}
        }}
        loadCheckout();
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)


# =============================================================================
# SERVER STARTUP
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
