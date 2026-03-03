"""
CFC Order Workflow Backend - v6.0.0
Refactored with helper modules for better maintainability.
Added comprehensive AI summary for order popup.
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
from pydantic import BaseModel

# =============================================================================
# IMPORT HELPER MODULES
# =============================================================================

# Config module - all environment variables and constants
from config import (
    DATABASE_URL, B2BWAVE_URL, B2BWAVE_USERNAME, B2BWAVE_API_KEY,
    ANTHROPIC_API_KEY, SHIPPO_API_KEY,
    AUTO_SYNC_INTERVAL_MINUTES, AUTO_SYNC_DAYS_BACK,
    SUPPLIER_INFO, WAREHOUSE_ZIPS, OVERSIZED_KEYWORDS
)

# Database helpers
from db_helpers import get_db

# Email parsing
try:
    from email_parser import parse_b2bwave_email, get_warehouses_for_skus
    EMAIL_PARSER_LOADED = True
except ImportError:
    EMAIL_PARSER_LOADED = False
    print("[STARTUP] email_parser module not found")

# Detection functions
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

# AI Summary (Anthropic)
try:
    from ai_summary import call_anthropic_api, generate_order_summary, generate_comprehensive_summary
    AI_SUMMARY_LOADED = True
except ImportError:
    AI_SUMMARY_LOADED = False
    print("[STARTUP] ai_summary module not found")

# Database migrations
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

# Sync service (B2BWave sync + auto-sync scheduler)
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

# B2BWave API (legacy - keep for compatibility)
try:
    import b2bwave_api
    B2BWAVE_MODULE_LOADED = True
except ImportError:
    B2BWAVE_MODULE_LOADED = False
    print("[STARTUP] b2bwave_api module not found")

# Gmail sync module
try:
    from gmail_sync import run_gmail_sync, gmail_configured
except ImportError:
    print("[STARTUP] gmail_sync module not found, email sync disabled")
    def run_gmail_sync(conn, hours_back=2):
        return {"status": "disabled", "reason": "module_not_found"}
    def gmail_configured():
        return False

# Square payment sync module
try:
    from square_sync import run_square_sync, square_configured
except ImportError:
    print("[STARTUP] square_sync module not found, payment sync disabled")
    def run_square_sync(conn, hours_back=24):
        return {"status": "disabled", "reason": "module_not_found"}
    def square_configured():
        return False

# R+L Carriers direct API
try:
    from rl_carriers import (
        get_simple_quote as rl_get_simple_quote,
        get_rate_quote as rl_get_rate_quote,
        test_connection as rl_test_connection,
        is_configured as rl_is_configured,
        track_shipment as rl_track_shipment
    )
    RL_CARRIERS_LOADED = True
except ImportError:
    RL_CARRIERS_LOADED = False
    print("[STARTUP] rl_carriers module not found")

# =============================================================================
# FASTAPI APP
# =============================================================================

app = FastAPI(title="CFC Order Workflow", version="6.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global for tracking last sync
last_auto_sync = None
auto_sync_running = False

# =============================================================================
# DATABASE SCHEMA (imported from schema.py)
# =============================================================================

try:
    from schema import SCHEMA_SQL
    SCHEMA_LOADED = True
except ImportError:
    SCHEMA_LOADED = False
    SCHEMA_SQL = "-- Schema not loaded"
    print("[STARTUP] schema module not found")

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
    order_id: Optional[str]
    parsed_data: Optional[dict]
    warehouses: Optional[List[str]]
    message: Optional[str]

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
    checkpoint: str  # payment_link_sent, payment_received, sent_to_warehouse, warehouse_confirmed, bol_sent, is_complete
    source: Optional[str] = "api"
    payment_amount: Optional[float] = None

class WarehouseMappingUpdate(BaseModel):
    sku_prefix: str
    warehouse_name: str
    warehouse_code: Optional[str] = None

# =============================================================================
# NOTE: parse_b2bwave_email and get_warehouses_for_skus are now imported from email_parser.py
# NOTE: call_anthropic_api and generate_order_summary are now imported from ai_summary.py
# NOTE: b2bwave_api_request and sync_order_from_b2bwave are now imported from sync_service.py
# =============================================================================

# Global state for auto-sync (imported from sync_service but kept for backward compatibility)
last_auto_sync = None
auto_sync_running = False

@app.on_event("startup")
def start_auto_sync():
    """Start background sync thread on app startup"""
    if SYNC_SERVICE_LOADED:
        start_auto_sync_thread(run_gmail_sync, run_square_sync)
    elif B2BWAVE_URL and B2BWAVE_USERNAME and B2BWAVE_API_KEY:
        print("[AUTO-SYNC] sync_service not loaded, auto-sync disabled")
    else:
        print("[AUTO-SYNC] B2BWave not configured, auto-sync disabled")

# =============================================================================
# ROUTES
# =============================================================================

@app.get("/")
def root():
    sync_status = get_sync_status() if SYNC_SERVICE_LOADED else {
        "enabled": False,
        "interval_minutes": AUTO_SYNC_INTERVAL_MINUTES,
        "last_sync": None,
        "running": False
    }
    return {
        "status": "ok", 
        "service": "CFC Order Workflow", 
        "version": "6.0.0",
        "auto_sync": sync_status,
        "gmail_sync": {
            "enabled": gmail_configured()
        },
        "square_sync": {
            "enabled": square_configured()
        }
    }

@app.get("/health")
def health():
    return {"status": "ok", "version": "6.0.0"}

# =============================================================================
# DATABASE MIGRATION ENDPOINTS (logic in db_migrations.py)
# =============================================================================

@app.post("/create-pending-checkouts-table")
def create_pending_checkouts_table():
    """Create pending_checkouts table for B2BWave checkout flow"""
    if DB_MIGRATIONS_LOADED:
        return _create_pending_checkouts()
    return {"status": "error", "message": "db_migrations module not loaded"}

@app.post("/create-shipments-table")
def create_shipments_table():
    """Create order_shipments table"""
    if DB_MIGRATIONS_LOADED:
        return _create_shipments()
    return {"status": "error", "message": "db_migrations module not loaded"}

@app.post("/add-rl-fields")
def add_rl_shipping_fields():
    """Add RL Carriers shipping fields"""
    if DB_MIGRATIONS_LOADED:
        return _add_rl_fields()
    return {"status": "error", "message": "db_migrations module not loaded"}

@app.post("/add-ps-fields")
def add_ps_fields():
    """Add Pirateship fields"""
    if DB_MIGRATIONS_LOADED:
        return _add_ps_fields()
    return {"status": "error", "message": "db_migrations module not loaded"}

@app.post("/fix-shipment-columns")
def fix_shipment_columns():
    """Fix column lengths in order_shipments"""
    if DB_MIGRATIONS_LOADED:
        return _fix_shipment_columns()
    return {"status": "error", "message": "db_migrations module not loaded"}

@app.post("/fix-sku-columns")
def fix_sku_columns():
    """Fix SKU column lengths"""
    if DB_MIGRATIONS_LOADED:
        return _fix_sku_columns()
    return {"status": "error", "message": "db_migrations module not loaded"}

@app.post("/fix-order-id-length")
def fix_order_id_length():
    """Increase order_id column length"""
    if DB_MIGRATIONS_LOADED:
        return _fix_order_id_length()
    return {"status": "error", "message": "db_migrations module not loaded"}

@app.post("/recreate-order-status-view")
def recreate_order_status_view():
    """Recreate the order_status view"""
    if DB_MIGRATIONS_LOADED:
        return _recreate_order_status_view()
    return {"status": "error", "message": "db_migrations module not loaded"}

@app.post("/add-weight-column")
def add_weight_column():
    """Add total_weight column"""
    if DB_MIGRATIONS_LOADED:
        return _add_weight_column()
    return {"status": "error", "message": "db_migrations module not loaded"}

@app.get("/debug/orders-columns")
def debug_orders_columns():
    """Check what columns exist in orders table"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = 'orders'
                ORDER BY ordinal_position
            """)
            columns = cur.fetchall()
            
            # Check if view exists
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'order_status'
            """)
            view_columns = cur.fetchall()
            
            return {
                "orders_columns": [c[0] for c in columns],
                "view_columns": [c[0] for c in view_columns] if view_columns else "view does not exist"
            }

@app.post("/init-db")
def init_db():
    """Initialize database schema (destructive!)"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
    return {"status": "ok", "message": "Database schema initialized", "version": "5.6.1"}

# =============================================================================
# B2BWAVE SYNC ENDPOINTS
# =============================================================================

@app.get("/b2bwave/test")
def test_b2bwave():
    """Test B2BWave API connection"""
    if not B2BWAVE_URL or not B2BWAVE_USERNAME or not B2BWAVE_API_KEY:
        return {
            "status": "error",
            "message": "B2BWave API not configured",
            "config": {
                "url_set": bool(B2BWAVE_URL),
                "username_set": bool(B2BWAVE_USERNAME),
                "api_key_set": bool(B2BWAVE_API_KEY)
            }
        }
    
    try:
        # Try to fetch one order to test connection
        data = b2bwave_api_request("orders", {"submitted_at_gteq": "2024-01-01"})
        order_count = len(data) if isinstance(data, list) else 1
        return {
            "status": "ok",
            "message": f"B2BWave API connected. Found {order_count} orders.",
            "url": B2BWAVE_URL
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

@app.post("/b2bwave/sync")
def sync_from_b2bwave(days_back: int = 14):
    """
    Sync orders from B2BWave API.
    Default: last 14 days of orders.
    """
    # Calculate date range
    since_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    
    try:
        data = b2bwave_api_request("orders", {"submitted_at_gteq": since_date})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"B2BWave API error: {str(e)}")
    
    # Handle response format
    orders_list = data if isinstance(data, list) else [data]
    
    synced = []
    errors = []
    
    for order_data in orders_list:
        try:
            result = sync_order_from_b2bwave(order_data)
            synced.append(result)
        except Exception as e:
            order_id = order_data.get('order', order_data).get('id', 'unknown')
            errors.append({"order_id": order_id, "error": str(e)})
    
    return {
        "status": "ok",
        "synced_count": len(synced),
        "error_count": len(errors),
        "synced_orders": synced,
        "errors": errors if errors else None
    }

@app.post("/gmail/sync")
def sync_from_gmail(hours_back: int = 2):
    """
    Sync order status updates from Gmail.
    Scans for: payment links sent, payments received, RL quotes, tracking numbers.
    Default: last 2 hours of emails.
    """
    if not gmail_configured():
        raise HTTPException(status_code=400, detail="Gmail not configured. Set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN environment variables.")
    
    try:
        with get_db() as conn:
            results = run_gmail_sync(conn, hours_back=hours_back)
        return {"status": "ok", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gmail sync error: {str(e)}")


@app.post("/square/sync")
def sync_from_square(hours_back: int = 24):
    """
    Sync payments from Square API.
    Matches payments to orders by parsing order IDs from payment descriptions.
    Default: last 24 hours of payments.
    """
    if not square_configured():
        raise HTTPException(status_code=400, detail="Square not configured. Set SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID environment variables.")
    
    try:
        with get_db() as conn:
            results = run_square_sync(conn, hours_back=hours_back)
        return {"status": "ok", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Square sync error: {str(e)}")


@app.get("/square/status")
def square_status():
    """Check Square API configuration status"""
    return {
        "configured": square_configured(),
        "message": "Square API configured" if square_configured() else "Set SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID environment variables"
    }


# =============================================================================
# SHIPPO - Small Package Shipping Rates
# =============================================================================

# Import Shippo module
try:
    from shippo_rates import get_shipping_rates, get_simple_rate, validate_address, test_shippo
    SHIPPO_ENABLED = bool(SHIPPO_API_KEY)
except ImportError as e:
    print(f"[STARTUP] shippo_rates module not found: {e}")
    SHIPPO_ENABLED = False


@app.get("/shippo/status")
def shippo_status():
    """Check Shippo API configuration status"""
    return {
        "configured": SHIPPO_ENABLED,
        "api_key_set": bool(SHIPPO_API_KEY),
        "message": "Shippo API configured" if SHIPPO_ENABLED else "Set SHIPPO_API_KEY environment variable"
    }


@app.get("/shippo/rates")
def get_shippo_rates(
    origin_zip: str,
    dest_zip: str,
    weight_lbs: float,
    is_residential: bool = True
):
    """
    Get small package shipping rates from Shippo.
    
    Example: /shippo/rates?origin_zip=30071&dest_zip=33859&weight_lbs=10
    """
    if not SHIPPO_ENABLED:
        raise HTTPException(status_code=503, detail="Shippo API not configured")
    
    result = get_simple_rate(
        origin_zip=origin_zip,
        dest_zip=dest_zip,
        weight_lbs=weight_lbs,
        is_residential=is_residential
    )
    
    return result


@app.post("/shippo/test")
def test_shippo_api():
    """Test Shippo API connection"""
    if not SHIPPO_ENABLED:
        raise HTTPException(status_code=503, detail="Shippo API not configured")
    
    result = test_shippo()
    return result


# =============================================================================
# R+L CARRIERS - LTL Freight Rates (Direct API)
# =============================================================================

@app.get("/rl/status")
def rl_status():
    """Check R+L Carriers API configuration status"""
    if not RL_CARRIERS_LOADED:
        return {"configured": False, "message": "rl_carriers module not loaded"}
    
    # Check env var directly for debugging
    import os
    env_key = os.environ.get("RL_CARRIERS_API_KEY", "")
    
    return {
        "configured": rl_is_configured(),
        "module_loaded": True,
        "api_url": "https://api.rlc.com",
        "key_length": len(env_key) if env_key else 0,
        "key_prefix": env_key[:8] + "..." if len(env_key) > 8 else "not set"
    }

@app.get("/rl/test")
def rl_test():
    """Test R+L Carriers API connection"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")
    
    return rl_test_connection()

@app.get("/rl/quote")
def rl_quote(
    origin_zip: str,
    dest_zip: str,
    weight_lbs: int,
    freight_class: str = "70"
):
    """
    Get LTL freight quote from R+L Carriers.
    
    Example: /rl/quote?origin_zip=32256&dest_zip=33101&weight_lbs=500
    """
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")
    
    try:
        result = rl_get_simple_quote(
            origin_zip=origin_zip,
            dest_zip=dest_zip,
            weight_lbs=weight_lbs,
            freight_class=freight_class
        )
        return {
            "status": "ok",
            "quote": result
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

@app.get("/rl/track/{pro_number}")
def rl_track(pro_number: str):
    """Track shipment by PRO number"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")
    
    try:
        result = rl_track_shipment(pro_number)
        return {
            "status": "ok",
            "shipment": result
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }


# --- R+L BOL and Pickup Endpoints ---

class RLBolRequest(BaseModel):
    """Request model for creating BOL"""
    # Shipper
    shipper_name: str
    shipper_address: str
    shipper_city: str
    shipper_state: str
    shipper_zip: str
    shipper_phone: str
    shipper_address2: Optional[str] = ""
    # Consignee
    consignee_name: str
    consignee_address: str
    consignee_city: str
    consignee_state: str
    consignee_zip: str
    consignee_phone: str
    consignee_address2: Optional[str] = ""
    consignee_email: Optional[str] = ""
    # Shipment
    weight_lbs: int
    pieces: int = 1
    description: str = "RTA Cabinets"
    freight_class: str = "70"
    # Reference
    po_number: Optional[str] = ""
    quote_number: Optional[str] = ""
    special_instructions: Optional[str] = ""
    # Pickup
    include_pickup: bool = False
    pickup_date: Optional[str] = None
    pickup_ready_time: str = "09:00"
    pickup_close_time: str = "17:00"


@app.post("/rl/bol")
def rl_create_bol(request: RLBolRequest):
    """Create Bill of Lading with R+L Carriers"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")
    
    try:
        from rl_carriers import create_bol
        result = create_bol(
            shipper_name=request.shipper_name,
            shipper_address=request.shipper_address,
            shipper_address2=request.shipper_address2,
            shipper_city=request.shipper_city,
            shipper_state=request.shipper_state,
            shipper_zip=request.shipper_zip,
            shipper_phone=request.shipper_phone,
            consignee_name=request.consignee_name,
            consignee_address=request.consignee_address,
            consignee_address2=request.consignee_address2,
            consignee_city=request.consignee_city,
            consignee_state=request.consignee_state,
            consignee_zip=request.consignee_zip,
            consignee_phone=request.consignee_phone,
            consignee_email=request.consignee_email,
            weight_lbs=request.weight_lbs,
            pieces=request.pieces,
            description=request.description,
            freight_class=request.freight_class,
            po_number=request.po_number,
            quote_number=request.quote_number,
            special_instructions=request.special_instructions,
            include_pickup=request.include_pickup,
            pickup_date=request.pickup_date,
            pickup_ready_time=request.pickup_ready_time,
            pickup_close_time=request.pickup_close_time
        )
        return {"status": "ok", "bol": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/rl/bol/{pro_number}")
def rl_get_bol(pro_number: str):
    """Get BOL details by PRO number"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    try:
        from rl_carriers import get_bol
        result = get_bol(pro_number)
        return {"status": "ok", "bol": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/rl/bol/{pro_number}/pdf")
def rl_get_bol_pdf(pro_number: str):
    """Get BOL as PDF (base64 encoded)"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    try:
        from rl_carriers import print_bol_pdf
        pdf_base64 = print_bol_pdf(pro_number)
        return {"status": "ok", "pdf_base64": pdf_base64}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/rl/bol/{pro_number}/labels")
def rl_get_labels(pro_number: str, num_labels: int = 4):
    """Get shipping labels as PDF (base64 encoded)"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    try:
        from rl_carriers import print_shipping_labels
        pdf_base64 = print_shipping_labels(pro_number, num_labels)
        return {"status": "ok", "pdf_base64": pdf_base64}
    except Exception as e:
        return {"status": "error", "message": str(e)}


class RLPickupRequest(BaseModel):
    """Request model for creating pickup"""
    # Shipper
    shipper_name: str
    shipper_address: str
    shipper_city: str
    shipper_state: str
    shipper_zip: str
    shipper_phone: str
    shipper_address2: Optional[str] = ""
    # Destination
    dest_city: str
    dest_state: str
    dest_zip: str
    # Shipment
    weight_lbs: int
    pieces: int = 1
    # Schedule
    pickup_date: Optional[str] = None
    ready_time: str = "09:00"
    close_time: str = "17:00"
    # Contact
    contact_name: Optional[str] = ""
    contact_email: Optional[str] = ""
    additional_instructions: Optional[str] = ""


@app.post("/rl/pickup")
def rl_create_pickup(request: RLPickupRequest):
    """Create pickup request with R+L Carriers"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")
    
    try:
        from rl_carriers import create_pickup_request
        result = create_pickup_request(
            shipper_name=request.shipper_name,
            shipper_address=request.shipper_address,
            shipper_address2=request.shipper_address2,
            shipper_city=request.shipper_city,
            shipper_state=request.shipper_state,
            shipper_zip=request.shipper_zip,
            shipper_phone=request.shipper_phone,
            dest_city=request.dest_city,
            dest_state=request.dest_state,
            dest_zip=request.dest_zip,
            weight_lbs=request.weight_lbs,
            pieces=request.pieces,
            pickup_date=request.pickup_date,
            ready_time=request.ready_time,
            close_time=request.close_time,
            contact_name=request.contact_name,
            contact_email=request.contact_email,
            additional_instructions=request.additional_instructions
        )
        return {"status": "ok", "pickup": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/rl/pickup/pro/{pro_number}")
def rl_pickup_for_pro(
    pro_number: str,
    pickup_date: Optional[str] = None,
    ready_time: str = "09:00 AM",
    close_time: str = "05:00 PM",
    contact_name: Optional[str] = "",
    contact_email: Optional[str] = ""
):
    """
    Schedule pickup for an existing BOL by PRO number.
    Simpler than standalone pickup - just needs the PRO.
    
    Args:
        pro_number: R+L PRO number (from BOL creation)
        pickup_date: Date in MM/dd/yyyy format (optional, defaults to tomorrow)
        ready_time: Ready time in HH:MM AM/PM format (default 09:00 AM)
        close_time: Close time in HH:MM AM/PM format (default 05:00 PM)
    """
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")
    
    try:
        from rl_carriers import create_pickup_for_pro
        result = create_pickup_for_pro(
            pro_number=pro_number,
            pickup_date=pickup_date,
            ready_time=ready_time,
            close_time=close_time,
            contact_name=contact_name,
            contact_email=contact_email
        )
        return {"status": "ok", "pro_number": pro_number, "pickup": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/rl/pickup/{pickup_id}")
def rl_get_pickup(pickup_id: int):
    """Get pickup request details"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    try:
        from rl_carriers import get_pickup_request
        result = get_pickup_request(pickup_id)
        return {"status": "ok", "pickup": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/rl/pickup/{pickup_id}")
def rl_cancel_pickup(pickup_id: int, reason: str = "Order cancelled"):
    """Cancel a pickup request"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    try:
        from rl_carriers import cancel_pickup_request
        result = cancel_pickup_request(pickup_id, reason)
        return {"status": "ok", "result": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/rl/pickup/pro/{pro_number}")
def rl_get_pickup_by_pro(pro_number: str):
    """Get pickup request details by PRO number"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    try:
        from rl_carriers import get_pickup_by_pro
        result = get_pickup_by_pro(pro_number)
        return {"status": "ok", "pickup": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/rl/pickup/pro/{pro_number}")
def rl_cancel_pickup_by_pro(pro_number: str, reason: str = "Order cancelled"):
    """Cancel a pickup request by PRO number"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    try:
        from rl_carriers import cancel_pickup_by_pro
        result = cancel_pickup_by_pro(pro_number, reason)
        return {"status": "ok", "result": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/rl/pickup/pro/{pro_number}")
def rl_pickup_for_pro(
    pro_number: str,
    pickup_date: Optional[str] = None,
    ready_time: str = "09:00",
    close_time: str = "17:00",
    additional_instructions: Optional[str] = ""
):
    """
    Schedule pickup for an existing BOL/PRO number.
    Much simpler than creating a full pickup request.
    
    Args:
        pro_number: R+L PRO number (e.g., WZ4947057)
        pickup_date: Date in MM/dd/yyyy format (optional, defaults to tomorrow)
        ready_time: Ready time in HH:MM format (default 09:00)
        close_time: Close time in HH:MM format (default 17:00)
    """
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")
    
    try:
        from rl_carriers import create_pickup_for_pro
        result = create_pickup_for_pro(
            pro_number=pro_number,
            pickup_date=pickup_date,
            ready_time=ready_time,
            close_time=close_time,
            additional_instructions=additional_instructions
        )
        return {"status": "ok", "pickup": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


class RLNotificationRequest(BaseModel):
    """Request model for setting up notifications"""
    pro_number: str
    email_addresses: List[str]
    events: Optional[List[str]] = None  # Default: all events


@app.post("/rl/notify")
def rl_setup_notification(request: RLNotificationRequest):
    """Set up shipment notifications"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    try:
        from rl_carriers import setup_shipment_notification
        result = setup_shipment_notification(
            pro_number=request.pro_number,
            email_addresses=request.email_addresses,
            events=request.events
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/rl/notify/{pro_number}")
def rl_get_notification(pro_number: str):
    """Get notification settings for a shipment"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    try:
        from rl_carriers import get_shipment_notification
        result = get_shipment_notification(pro_number)
        return {"status": "ok", "notifications": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# --- R+L Order-Based BOL Creation ---

@app.post("/rl/order/{order_id}/create-bol")
def rl_create_order_bol(
    order_id: str,
    warehouse_code: str,
    include_pickup: bool = False,
    pickup_date: Optional[str] = None,
    special_instructions: Optional[str] = ""
):
    """
    Create BOL for a specific warehouse shipment from an order.
    Uses warehouse addresses from checkout.py and customer info from B2BWave.
    
    Args:
        order_id: B2BWave order ID
        warehouse_code: Warehouse code (e.g., 'Cabinet & Stone', 'ROC', 'L&C')
        include_pickup: Whether to include a pickup request
        pickup_date: Pickup date in MM/dd/yyyy format (optional)
        special_instructions: Special handling instructions
    """
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")
    
    try:
        from checkout import WAREHOUSES, fetch_b2bwave_order, calculate_order_shipping
        from rl_carriers import create_bol
        
        # Get warehouse info
        warehouse = WAREHOUSES.get(warehouse_code)
        if not warehouse:
            return {"status": "error", "message": f"Unknown warehouse: {warehouse_code}"}
        
        # Fetch order from B2BWave
        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "message": f"Order {order_id} not found"}
        
        # Get shipping address from fetch_b2bwave_order format
        # It returns: {address, city, state, zip, country}
        shipping = order_data.get('shipping_address', {})
        
        customer_name = order_data.get('customer_name', 'Customer')
        company_name = order_data.get('company_name') or customer_name
        
        # Calculate shipping to get weight for this warehouse
        dest_address = {
            'address': shipping.get('address', ''),
            'city': shipping.get('city', ''),
            'state': shipping.get('state', ''),
            'zip': shipping.get('zip', ''),
            'country': shipping.get('country', 'US')
        }
        
        shipping_calc = calculate_order_shipping(order_data, dest_address)
        
        # Find the shipment for this warehouse
        warehouse_shipment = None
        for shipment in shipping_calc.get('shipments', []):
            if shipment.get('warehouse') == warehouse_code:
                warehouse_shipment = shipment
                break
        
        if not warehouse_shipment:
            return {"status": "error", "message": f"No shipment found for warehouse {warehouse_code} in order {order_id}"}
        
        weight = warehouse_shipment.get('weight', 100)
        items = warehouse_shipment.get('items', [])
        pieces = len(items) if items else 1
        
        # Build item description
        item_descriptions = [f"{item.get('quantity', 1)}x {item.get('name', item.get('sku', 'Cabinet'))}" for item in items[:3]]
        description = "; ".join(item_descriptions)
        if len(items) > 3:
            description += f" +{len(items) - 3} more items"
        if len(description) > 100:
            description = f"RTA Cabinets - {len(items)} items"
        
        # Get quote number if available
        quote_number = ""
        if warehouse_shipment.get('quote', {}).get('quote', {}):
            quote_number = warehouse_shipment['quote']['quote'].get('quote_number', '')
        
        # Create BOL
        result = create_bol(
            # Shipper (warehouse)
            shipper_name=warehouse.get('name'),
            shipper_address=warehouse.get('address', ''),
            shipper_city=warehouse.get('city'),
            shipper_state=warehouse.get('state'),
            shipper_zip=warehouse.get('zip'),
            shipper_phone=warehouse.get('phone', ''),
            # Consignee (customer)
            consignee_name=company_name,
            consignee_address=shipping.get('address', ''),
            consignee_address2=shipping.get('address2', ''),
            consignee_city=shipping.get('city', ''),
            consignee_state=shipping.get('state', ''),
            consignee_zip=shipping.get('zip', ''),
            consignee_phone=order_data.get('customer_phone', ''),
            consignee_email=order_data.get('customer_email', ''),
            # Shipment details
            weight_lbs=int(weight),
            pieces=pieces,
            description=description,
            freight_class="70",
            po_number=order_id,
            quote_number=quote_number,
            special_instructions=special_instructions,
            # Pickup
            include_pickup=include_pickup,
            pickup_date=pickup_date
        )
        
        return {
            "status": "ok",
            "order_id": order_id,
            "warehouse": warehouse_code,
            "bol": result,
            "shipment_details": {
                "weight": weight,
                "pieces": pieces,
                "description": description
            }
        }
        
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/rl/order/{order_id}/pickup")
def rl_create_order_pickup(
    order_id: str,
    warehouse_code: str,
    pickup_date: Optional[str] = None,
    ready_time: str = "09:00",
    close_time: str = "17:00",
    additional_instructions: Optional[str] = ""
):
    """
    Create pickup request for a warehouse shipment.
    Uses warehouse addresses from checkout.py and customer info from B2BWave.
    
    Args:
        order_id: B2BWave order ID
        warehouse_code: Warehouse code (e.g., 'L&C', 'Cabinet & Stone')
        pickup_date: Pickup date in MM/dd/yyyy format (optional, defaults to tomorrow)
        ready_time: Ready time (default 09:00)
        close_time: Close time (default 17:00)
    """
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")
    
    try:
        from checkout import WAREHOUSES, fetch_b2bwave_order, calculate_order_shipping
        from rl_carriers import create_pickup_request
        
        # Get warehouse info
        warehouse = WAREHOUSES.get(warehouse_code)
        if not warehouse:
            return {"status": "error", "message": f"Unknown warehouse: {warehouse_code}"}
        
        # Fetch order from B2BWave
        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "message": f"Order {order_id} not found"}
        
        # Get shipping address
        shipping = order_data.get('shipping_address', {})
        
        # Calculate shipping to get weight
        dest_address = {
            'address': shipping.get('address', ''),
            'city': shipping.get('city', ''),
            'state': shipping.get('state', ''),
            'zip': shipping.get('zip', ''),
            'country': shipping.get('country', 'US')
        }
        
        shipping_calc = calculate_order_shipping(order_data, dest_address)
        
        # Find the shipment for this warehouse
        warehouse_shipment = None
        for shipment in shipping_calc.get('shipments', []):
            if shipment.get('warehouse') == warehouse_code:
                warehouse_shipment = shipment
                break
        
        if not warehouse_shipment:
            return {"status": "error", "message": f"No shipment found for warehouse {warehouse_code}"}
        
        weight = warehouse_shipment.get('weight', 100)
        items = warehouse_shipment.get('items', [])
        pieces = len(items) if items else 1
        
        # Create pickup request
        result = create_pickup_request(
            shipper_name=warehouse.get('name'),
            shipper_address=warehouse.get('address', ''),
            shipper_city=warehouse.get('city'),
            shipper_state=warehouse.get('state'),
            shipper_zip=warehouse.get('zip'),
            shipper_phone=warehouse.get('phone', ''),
            dest_city=shipping.get('city', ''),
            dest_state=shipping.get('state', ''),
            dest_zip=shipping.get('zip', ''),
            weight_lbs=int(weight),
            pieces=pieces,
            pickup_date=pickup_date,
            ready_time=ready_time,
            close_time=close_time,
            contact_name=warehouse.get('name'),
            contact_email=order_data.get('customer_email', ''),
            additional_instructions=additional_instructions or f"Order #{order_id}"
        )
        
        return {
            "status": "ok",
            "order_id": order_id,
            "warehouse": warehouse_code,
            "pickup": result
        }
        
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/rl/order/{order_id}/shipments")
def rl_get_order_shipments(order_id: str):
    """
    Get R+L-ready shipment info for an order (for BOL creation UI).
    Shows which warehouses need BOLs and their shipping details.
    """
    try:
        from checkout import WAREHOUSES, fetch_b2bwave_order, calculate_order_shipping
        
        # Fetch order
        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "message": f"Order {order_id} not found"}
        
        # Get shipping address from fetch_b2bwave_order format
        # It returns: {address, city, state, zip, country}
        shipping = order_data.get('shipping_address', {})
        
        dest_address = {
            'address': shipping.get('address', ''),
            'city': shipping.get('city', ''),
            'state': shipping.get('state', ''),
            'zip': shipping.get('zip', ''),
            'country': shipping.get('country', 'US')
        }
        
        # Calculate shipping
        shipping_calc = calculate_order_shipping(order_data, dest_address)
        
        # Build response with warehouse details
        shipments = []
        for s in shipping_calc.get('shipments', []):
            wh_code = s.get('warehouse')
            wh_info = WAREHOUSES.get(wh_code, {})
            
            shipments.append({
                "warehouse_code": wh_code,
                "warehouse_name": wh_info.get('name', wh_code),
                "warehouse_address": {
                    "address": wh_info.get('address', ''),
                    "city": wh_info.get('city', ''),
                    "state": wh_info.get('state', ''),
                    "zip": wh_info.get('zip', ''),
                    "phone": wh_info.get('phone', '')
                },
                "weight": s.get('weight', 0),
                "items_count": len(s.get('items', [])),
                "shipping_method": s.get('shipping_method'),
                "shipping_cost": s.get('shipping_cost', 0),
                "quote_number": s.get('quote', {}).get('quote', {}).get('quote_number'),
                "needs_bol": s.get('shipping_method') == 'ltl'
            })
        
        # Customer info - parse name from customer_name field
        customer_name = order_data.get('customer_name', '')
        
        return {
            "status": "ok",
            "order_id": order_id,
            "customer": {
                "name": customer_name,
                "email": order_data.get('customer_email', ''),
                "company": order_data.get('company_name', ''),
                "address": shipping.get('address', ''),
                "address2": shipping.get('address2', ''),
                "city": shipping.get('city', ''),
                "state": shipping.get('state', ''),
                "zip": shipping.get('zip', ''),
                "phone": order_data.get('customer_phone', '')
            },
            "shipments": shipments,
            "total_shipping": shipping_calc.get('total_shipping', 0),
            "ltl_shipments_count": sum(1 for s in shipments if s.get('needs_bol'))
        }
        
    except Exception as e:
        return {"status": "error", "message": str(e)}


# =============================================================================
# RTA DATABASE - SKU Weights and Shipping Rules
# =============================================================================

# Import RTA database module
try:
    from rta_database import (
        init_rta_table, get_sku_info, get_skus_info,
        calculate_order_weight_and_flags, get_rta_stats
    )
    RTA_DB_ENABLED = True
except ImportError as e:
    print(f"[STARTUP] rta_database module not found: {e}")
    RTA_DB_ENABLED = False


@app.get("/rta/status")
def rta_status():
    """Check RTA database status and stats"""
    if not RTA_DB_ENABLED:
        return {"configured": False, "message": "RTA database module not loaded"}
    
    try:
        stats = get_rta_stats()
        return {
            "configured": True,
            "stats": stats
        }
    except Exception as e:
        return {"configured": True, "error": str(e)}


@app.post("/rta/init")
def rta_init_table():
    """Initialize the RTA products table"""
    if not RTA_DB_ENABLED:
        raise HTTPException(status_code=503, detail="RTA database module not loaded")
    
    result = init_rta_table()
    return result


@app.get("/rta/sku/{sku}")
def rta_get_sku(sku: str):
    """Look up a single SKU"""
    if not RTA_DB_ENABLED:
        raise HTTPException(status_code=503, detail="RTA database module not loaded")
    
    info = get_sku_info(sku)
    if not info:
        raise HTTPException(status_code=404, detail=f"SKU {sku} not found")
    
    return info


@app.post("/rta/calculate-weight")
def rta_calculate_weight(request: dict):
    """
    Calculate total weight and check for long pallet items.
    
    Body: {"line_items": [{"sku": "NJGR-WF342", "quantity": 1}, ...]}
    """
    if not RTA_DB_ENABLED:
        raise HTTPException(status_code=503, detail="RTA database module not loaded")
    
    line_items = request.get("line_items", [])
    result = calculate_order_weight_and_flags(line_items)
    return result


@app.get("/b2bwave/order/{order_id}")
def get_b2bwave_order(order_id: str):
    """Fetch a specific order from B2BWave and sync it"""
    try:
        data = b2bwave_api_request("orders", {"id_eq": order_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"B2BWave API error: {str(e)}")
    
    if not data:
        raise HTTPException(status_code=404, detail="Order not found in B2BWave")
    
    # Handle response format
    order_data = data[0] if isinstance(data, list) else data
    
    result = sync_order_from_b2bwave(order_data)
    
    return {
        "status": "ok",
        "message": f"Order {order_id} synced from B2BWave",
        "order": result
    }

# =============================================================================
# EMAIL PARSING ENDPOINT
# =============================================================================

@app.post("/parse-email", response_model=ParseEmailResponse)
def parse_email(request: ParseEmailRequest):
    """
    Parse a B2BWave order email and create/update the order.
    This is the main entry point - Google Sheet just sends raw email here.
    """
    parsed = parse_b2bwave_email(request.email_body, request.email_subject)
    
    if not parsed['order_id']:
        return ParseEmailResponse(
            status="error",
            message="Could not extract order ID from email"
        )
    
    # Get warehouses from SKU prefixes
    warehouses = get_warehouses_for_skus(parsed.get('sku_prefixes', []))
    
    # Create or update order
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Check if order exists
            cur.execute("SELECT order_id FROM orders WHERE order_id = %s", (parsed['order_id'],))
            exists = cur.fetchone()
            
            if exists:
                # Update existing order (don't overwrite checkpoints)
                cur.execute("""
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
                """, (
                    parsed['customer_name'],
                    parsed['company_name'],
                    parsed['email'],
                    parsed['phone'],
                    parsed['street'],
                    parsed['city'],
                    parsed['state'],
                    parsed['zip_code'],
                    parsed['order_total'],
                    parsed['comments'],
                    warehouses[0] if len(warehouses) > 0 else None,
                    warehouses[1] if len(warehouses) > 1 else None,
                    parsed['order_id']
                ))
                
                return ParseEmailResponse(
                    status="updated",
                    order_id=parsed['order_id'],
                    parsed_data=parsed,
                    warehouses=warehouses,
                    message="Order updated"
                )
            else:
                # Create new order
                order_date = request.email_date or datetime.now(timezone.utc).isoformat()
                
                # Check if trusted customer
                trusted = is_trusted_customer(conn, parsed['customer_name'] or '', parsed['company_name'] or '')
                
                cur.execute("""
                    INSERT INTO orders (
                        order_id, customer_name, company_name, email, phone,
                        street, city, state, zip_code,
                        order_date, order_total, comments,
                        warehouse_1, warehouse_2, email_thread_id,
                        is_trusted_customer
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    parsed['order_id'],
                    parsed['customer_name'],
                    parsed['company_name'],
                    parsed['email'],
                    parsed['phone'],
                    parsed['street'],
                    parsed['city'],
                    parsed['state'],
                    parsed['zip_code'],
                    order_date,
                    parsed['order_total'],
                    parsed['comments'],
                    warehouses[0] if len(warehouses) > 0 else None,
                    warehouses[1] if len(warehouses) > 1 else None,
                    request.email_thread_id,
                    trusted
                ))
                
                # Log event
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'order_created', %s, 'email_parse')
                """, (parsed['order_id'], json.dumps(parsed)))
                
                return ParseEmailResponse(
                    status="created",
                    order_id=parsed['order_id'],
                    parsed_data=parsed,
                    warehouses=warehouses,
                    message="Order created"
                )

# =============================================================================
# PAYMENT DETECTION ENDPOINTS
# =============================================================================

@app.post("/detect-payment-link")
def detect_payment_link(order_id: str, email_body: str):
    """Detect if email contains Square payment link"""
    if 'square.link' in email_body.lower():
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE orders SET 
                        payment_link_sent = TRUE,
                        payment_link_sent_at = NOW(),
                        updated_at = NOW()
                    WHERE order_id = %s AND NOT payment_link_sent
                """, (order_id,))
                
                if cur.rowcount > 0:
                    cur.execute("""
                        INSERT INTO order_events (order_id, event_type, source)
                        VALUES (%s, 'payment_link_sent', 'email_detection')
                    """, (order_id,))
                    return {"status": "ok", "updated": True}
        
        return {"status": "ok", "updated": False, "message": "Already marked"}
    
    return {"status": "ok", "updated": False, "message": "No square link found"}

@app.post("/detect-payment-received")
def detect_payment_received(email_subject: str, email_body: str):
    """
    Detect Square payment notification.
    Subject format: "$4,913.99 payment received from Dylan Gentry"
    """
    # Extract amount from subject
    amount_match = re.search(r'\$([\d,]+\.?\d*)\s+payment received', email_subject, re.IGNORECASE)
    if not amount_match:
        return {"status": "ok", "updated": False, "message": "Not a payment notification"}
    
    payment_amount = float(amount_match.group(1).replace(',', ''))
    
    # Extract customer name
    name_match = re.search(r'payment received from (.+)$', email_subject, re.IGNORECASE)
    customer_name = name_match.group(1).strip() if name_match else None
    
    # Try to match to an order
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # First try exact amount match on unpaid orders
            cur.execute("""
                SELECT order_id, order_total, customer_name 
                FROM orders 
                WHERE NOT payment_received 
                AND order_total IS NOT NULL
                ORDER BY order_date DESC
                LIMIT 50
            """)
            orders = cur.fetchall()
            
            matched_order = None
            
            # Try to match by amount (payment should be >= order total)
            for order in orders:
                if order['order_total'] and payment_amount >= float(order['order_total']):
                    # Could be this order - check name similarity if we have it
                    if customer_name and order['customer_name']:
                        # Simple check - first name match
                        pay_first = customer_name.split()[0].lower()
                        order_first = order['customer_name'].split()[0].lower()
                        if pay_first == order_first:
                            matched_order = order
                            break
                    elif not matched_order:
                        # Take first amount match if no name match
                        matched_order = order
            
            if matched_order:
                order_total = float(matched_order['order_total']) if matched_order['order_total'] else 0
                shipping_cost = payment_amount - order_total if order_total else None
                
                cur.execute("""
                    UPDATE orders SET 
                        payment_received = TRUE,
                        payment_received_at = NOW(),
                        payment_amount = %s,
                        shipping_cost = %s,
                        updated_at = NOW()
                    WHERE order_id = %s
                """, (payment_amount, shipping_cost, matched_order['order_id']))
                
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'payment_received', %s, 'square_notification')
                """, (matched_order['order_id'], json.dumps({
                    'payment_amount': payment_amount,
                    'shipping_cost': shipping_cost,
                    'customer_name': customer_name
                })))
                
                return {
                    "status": "ok",
                    "updated": True,
                    "order_id": matched_order['order_id'],
                    "payment_amount": payment_amount,
                    "shipping_cost": shipping_cost
                }
            
            return {
                "status": "ok",
                "updated": False,
                "message": "Could not match payment to order",
                "payment_amount": payment_amount,
                "customer_name": customer_name
            }

# =============================================================================
# ORDER CRUD
# =============================================================================

@app.get("/orders")
def list_orders(
    status: Optional[str] = None,
    include_complete: bool = False,
    limit: int = 200
):
    """List orders with optional filters, including shipments"""
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
            
            # Get shipments for all orders
            order_ids = [o['order_id'] for o in orders]
            shipments_by_order = {}
            if order_ids:
                cur.execute("""
                    SELECT * FROM order_shipments 
                    WHERE order_id = ANY(%s)
                    ORDER BY warehouse
                """, (order_ids,))
                for ship in cur.fetchall():
                    oid = ship['order_id']
                    if oid not in shipments_by_order:
                        shipments_by_order[oid] = []
                    # Convert decimals
                    if ship.get('weight'):
                        ship['weight'] = float(ship['weight'])
                    shipments_by_order[oid].append(dict(ship))
            
            # Convert decimals to floats for JSON and attach shipments
            for order in orders:
                for key in ['order_total', 'payment_amount', 'shipping_cost']:
                    if order.get(key):
                        order[key] = float(order[key])
                order['shipments'] = shipments_by_order.get(order['order_id'], [])
            
            return {"status": "ok", "count": len(orders), "orders": orders}

@app.get("/orders/{order_id}")
def get_order(order_id: str):
    """Get single order details"""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT o.*, s.current_status, s.days_open
                FROM orders o
                JOIN order_status s ON o.order_id = s.order_id
                WHERE o.order_id = %s
            """, (order_id,))
            order = cur.fetchone()
            
            if not order:
                raise HTTPException(status_code=404, detail="Order not found")
            
            # Convert decimals
            for key in ['order_total', 'payment_amount', 'shipping_cost']:
                if order.get(key):
                    order[key] = float(order[key])
            
            return {"status": "ok", "order": order}

@app.post("/orders/{order_id}/generate-summary")
def generate_summary_endpoint(order_id: str, force: bool = False):
    """
    Generate SHORT AI summary for order card display.
    If force=False and summary exists and is less than 1 hour old, returns cached.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Check for existing recent summary
            cur.execute("""
                SELECT ai_summary, ai_summary_updated_at 
                FROM orders 
                WHERE order_id = %s
            """, (order_id,))
            order = cur.fetchone()
            
            if not order:
                raise HTTPException(status_code=404, detail="Order not found")
            
            # Return cached if recent and not forcing refresh
            if not force and order.get('ai_summary') and order.get('ai_summary_updated_at'):
                age = datetime.now(timezone.utc) - order['ai_summary_updated_at']
                if age < timedelta(hours=1):
                    return {
                        "status": "ok", 
                        "summary": order['ai_summary'],
                        "cached": True,
                        "updated_at": order['ai_summary_updated_at'].isoformat()
                    }
    
    # Generate new SHORT summary
    summary = generate_order_summary(order_id)
    
    # Save to database
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orders 
                SET ai_summary = %s, ai_summary_updated_at = NOW(), updated_at = NOW()
                WHERE order_id = %s
            """, (summary, order_id))
    
    return {
        "status": "ok",
        "summary": summary,
        "cached": False,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }


@app.post("/orders/{order_id}/comprehensive-summary")
def generate_comprehensive_summary_endpoint(order_id: str, force: bool = False):
    """
    Generate COMPREHENSIVE AI summary for order popup - full history analysis.
    This provides detailed timeline, communications, shipping status, and issues.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Check if order exists
            cur.execute("SELECT order_id FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()
            
            if not order:
                raise HTTPException(status_code=404, detail="Order not found")
    
    # Generate COMPREHENSIVE summary (always fresh for now)
    summary = generate_comprehensive_summary(order_id)
    
    return {
        "status": "ok",
        "summary": summary,
        "cached": False,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

@app.post("/orders/{order_id}/add-email-snippet")
def add_email_snippet(
    order_id: str,
    email_from: str,
    email_subject: str,
    email_snippet: str,
    email_date: Optional[str] = None,
    snippet_type: str = "general"
):
    """Add an email snippet for an order (called by Google Script)"""
    with get_db() as conn:
        with conn.cursor() as cur:
            # Parse date
            parsed_date = None
            if email_date:
                try:
                    parsed_date = datetime.fromisoformat(email_date.replace('Z', '+00:00'))
                except:
                    parsed_date = datetime.now(timezone.utc)
            else:
                parsed_date = datetime.now(timezone.utc)
            
            cur.execute("""
                INSERT INTO order_email_snippets 
                (order_id, email_from, email_subject, email_snippet, email_date, snippet_type)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (order_id, email_from, email_subject, email_snippet[:1000], parsed_date, snippet_type))
    
    return {"status": "ok", "message": "Email snippet added"}

@app.get("/orders/{order_id}/supplier-sheet-data")
def get_supplier_sheet_data(order_id: str):
    """
    Get order data organized by warehouse for supplier sheet generation.
    Returns data formatted for creating Google Sheet with tabs per supplier.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get order details
            cur.execute("""
                SELECT * FROM orders WHERE order_id = %s
            """, (order_id,))
            order = cur.fetchone()
            
            if not order:
                raise HTTPException(status_code=404, detail="Order not found")
            
            # Get line items
            cur.execute("""
                SELECT * FROM order_line_items WHERE order_id = %s
            """, (order_id,))
            line_items = cur.fetchall()
    
    # Build customer info
    customer_name = order.get('customer_name') or ''
    company_name = order.get('company_name') or ''
    customer_display = company_name if company_name else customer_name
    if company_name and customer_name:
        customer_display = f"{company_name} ({customer_name})"
    
    street = order.get('street') or ''
    street2 = order.get('street2') or ''
    city = order.get('city') or ''
    state = order.get('state') or ''
    zip_code = order.get('zip_code') or ''
    phone = order.get('phone') or ''
    email = order.get('email') or ''
    
    address_parts = [street]
    if street2:
        address_parts.append(street2)
    address_parts.append(f"{city}, {state} {zip_code}")
    customer_address = ', '.join(filter(None, address_parts))
    
    comments = order.get('comments') or ''
    
    # Group items by warehouse
    warehouses = {}
    for item in line_items:
        wh = item.get('warehouse') or 'Unknown'
        if wh not in warehouses:
            # Get supplier info
            supplier_info = SUPPLIER_INFO.get(wh, {
                'name': wh,
                'address': '',
                'contact': '',
                'email': ''
            })
            warehouses[wh] = {
                'supplier_name': supplier_info['name'],
                'supplier_address': supplier_info['address'],
                'supplier_contact': supplier_info['contact'],
                'supplier_email': supplier_info['email'],
                'items': []
            }
        
        warehouses[wh]['items'].append({
            'quantity': item.get('quantity') or 1,
            'product_code': item.get('sku') or '',
            'product_name': item.get('product_name') or ''
        })
    
    return {
        "status": "ok",
        "order_id": order_id,
        "customer_name": customer_display,
        "customer_address": customer_address,
        "customer_phone": phone,
        "customer_email": email,
        "comments": comments,
        "warehouses": warehouses
    }

@app.patch("/orders/{order_id}")
def update_order(order_id: str, update: OrderUpdate):
    """Update order fields"""
    with get_db() as conn:
        with conn.cursor() as cur:
            # Build dynamic update
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

@app.patch("/orders/{order_id}/checkpoint")
def update_checkpoint(order_id: str, update: CheckpointUpdate):
    """Update order checkpoint"""
    valid_checkpoints = [
        'payment_link_sent', 'payment_received', 'sent_to_warehouse',
        'warehouse_confirmed', 'bol_sent', 'is_complete'
    ]
    
    if update.checkpoint not in valid_checkpoints:
        raise HTTPException(status_code=400, detail=f"Invalid checkpoint. Must be one of: {valid_checkpoints}")
    
    with get_db() as conn:
        with conn.cursor() as cur:
            timestamp_field = f"{update.checkpoint}_at" if update.checkpoint != 'is_complete' else 'completed_at'
            
            # Build update query
            set_parts = [f"{update.checkpoint} = TRUE", f"{timestamp_field} = NOW()", "updated_at = NOW()"]
            params = []
            
            # Handle payment amount if provided
            if update.checkpoint == 'payment_received' and update.payment_amount:
                set_parts.append("payment_amount = %s")
                params.append(update.payment_amount)
                
                # Calculate shipping cost
                cur.execute("SELECT order_total FROM orders WHERE order_id = %s", (order_id,))
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
            
            # Log event
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, %s, %s, %s)
            """, (
                order_id,
                update.checkpoint,
                json.dumps({'payment_amount': update.payment_amount} if update.payment_amount else {}),
                update.source
            ))
            
            return {"status": "ok", "checkpoint": update.checkpoint}

@app.patch("/orders/{order_id}/set-status")
def set_order_status(order_id: str, status: str, source: str = "web_ui"):
    """
    Set order to a specific status by resetting all checkpoints and setting appropriate ones.
    This allows moving orders backwards in the workflow.
    """
    # Map status to which checkpoints should be TRUE
    status_checkpoints = {
        'needs_payment_link': {},  # All false
        'awaiting_payment': {'payment_link_sent': True},
        'needs_warehouse_order': {'payment_link_sent': True, 'payment_received': True},
        'awaiting_warehouse': {'payment_link_sent': True, 'payment_received': True, 'sent_to_warehouse': True},
        'needs_bol': {'payment_link_sent': True, 'payment_received': True, 'sent_to_warehouse': True, 'warehouse_confirmed': True},
        'awaiting_shipment': {'payment_link_sent': True, 'payment_received': True, 'sent_to_warehouse': True, 'warehouse_confirmed': True, 'bol_sent': True},
        'complete': {'payment_link_sent': True, 'payment_received': True, 'sent_to_warehouse': True, 'warehouse_confirmed': True, 'bol_sent': True, 'is_complete': True}
    }
    
    if status not in status_checkpoints:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    
    checkpoints = status_checkpoints[status]
    
    with get_db() as conn:
        with conn.cursor() as cur:
            # Reset all checkpoints first, then set the ones we need
            cur.execute("""
                UPDATE orders SET
                    payment_link_sent = %s,
                    payment_received = %s,
                    sent_to_warehouse = %s,
                    warehouse_confirmed = %s,
                    bol_sent = %s,
                    is_complete = %s,
                    updated_at = NOW()
                WHERE order_id = %s
            """, (
                checkpoints.get('payment_link_sent', False),
                checkpoints.get('payment_received', False),
                checkpoints.get('sent_to_warehouse', False),
                checkpoints.get('warehouse_confirmed', False),
                checkpoints.get('bol_sent', False),
                checkpoints.get('is_complete', False),
                order_id
            ))
            
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Order not found")
            
            # Log event
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'status_change', %s, %s)
            """, (order_id, json.dumps({'new_status': status}), source))
            
            return {"status": "ok", "new_status": status}

# =============================================================================
# SHIPMENT MANAGEMENT
# =============================================================================

@app.get("/orders/{order_id}/shipments")
def get_order_shipments(order_id: str):
    """Get all shipments for an order"""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM order_shipments 
                WHERE order_id = %s 
                ORDER BY warehouse
            """, (order_id,))
            shipments = cur.fetchall()
            return {"status": "ok", "shipments": shipments}

@app.get("/shipments")
def list_all_shipments(include_complete: bool = False):
    """List all shipments with order info"""
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
            
            # Convert decimals
            for s in shipments:
                if s.get('order_total'):
                    s['order_total'] = float(s['order_total'])
                if s.get('weight'):
                    s['weight'] = float(s['weight'])
            
            return {"status": "ok", "count": len(shipments), "shipments": shipments}

@app.patch("/shipments/{shipment_id}")
def update_shipment(shipment_id: str, 
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
                    customer_price: Optional[float] = None):
    """Update shipment fields"""
    
    valid_statuses = ['needs_order', 'at_warehouse', 'needs_bol', 'ready_ship', 'shipped', 'delivered']
    if status and status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")
    
    valid_methods = ['LTL', 'Pirateship', 'Pickup', 'BoxTruck', 'LiDelivery', None]
    if ship_method and ship_method not in valid_methods:
        raise HTTPException(status_code=400, detail=f"Invalid ship_method. Must be one of: {valid_methods}")
    
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Build dynamic update
            updates = []
            params = []
            
            if status is not None:
                updates.append("status = %s")
                params.append(status)
                # Set timestamp based on status
                if status == 'at_warehouse':
                    updates.append("sent_to_warehouse_at = NOW()")
                elif status == 'needs_bol':
                    updates.append("warehouse_confirmed_at = NOW()")
                elif status == 'shipped':
                    updates.append("shipped_at = NOW()")
                elif status == 'delivered':
                    updates.append("delivered_at = NOW()")
            
            if tracking is not None:
                updates.append("tracking = %s")
                params.append(tracking)
            
            if pro_number is not None:
                updates.append("pro_number = %s")
                params.append(pro_number)
            
            if weight is not None:
                updates.append("weight = %s")
                params.append(weight)
            
            if ship_method is not None:
                updates.append("ship_method = %s")
                params.append(ship_method)
            
            if bol_sent is not None:
                updates.append("bol_sent = %s")
                params.append(bol_sent)
                if bol_sent:
                    updates.append("bol_sent_at = NOW()")
            
            # RL Carriers fields
            if origin_zip is not None:
                updates.append("origin_zip = %s")
                params.append(origin_zip)
            
            if rl_quote_number is not None:
                updates.append("rl_quote_number = %s")
                params.append(rl_quote_number)
            
            if rl_quote_price is not None:
                updates.append("rl_quote_price = %s")
                params.append(rl_quote_price)
            
            if rl_customer_price is not None:
                updates.append("rl_customer_price = %s")
                params.append(rl_customer_price)
            
            if rl_invoice_amount is not None:
                updates.append("rl_invoice_amount = %s")
                params.append(rl_invoice_amount)
            
            if has_oversized is not None:
                updates.append("has_oversized = %s")
                params.append(has_oversized)
            
            # Li Delivery fields
            if li_quote_price is not None:
                updates.append("li_quote_price = %s")
                params.append(li_quote_price)
            
            if li_customer_price is not None:
                updates.append("li_customer_price = %s")
                params.append(li_customer_price)
            
            if actual_cost is not None:
                updates.append("actual_cost = %s")
                params.append(actual_cost)
            
            if quote_url is not None:
                updates.append("quote_url = %s")
                params.append(quote_url)

            if ps_quote_url is not None:
                updates.append("ps_quote_url = %s")
                params.append(ps_quote_url)

            if ps_quote_price is not None:
                updates.append("ps_quote_price = %s")
                params.append(ps_quote_price)
            
            if tracking_number is not None:
                updates.append("tracking_number = %s")
                params.append(tracking_number)
            
            if quote_price is not None:
                updates.append("quote_price = %s")
                params.append(quote_price)
            
            if customer_price is not None:
                updates.append("customer_price = %s")
                params.append(customer_price)
            
            if not updates:
                return {"status": "ok", "message": "No updates provided"}
            
            updates.append("updated_at = NOW()")
            params.append(shipment_id)
            
            query = f"UPDATE order_shipments SET {', '.join(updates)} WHERE shipment_id = %s RETURNING *"
            cur.execute(query, params)
            
            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=404, detail="Shipment not found")
            
            # Check if all shipments for this order are delivered
            cur.execute("""
                SELECT COUNT(*) as total, 
                       COUNT(*) FILTER (WHERE status = 'delivered') as delivered
                FROM order_shipments 
                WHERE order_id = %s
            """, (result['order_id'],))
            counts = cur.fetchone()
            
            # If all delivered, mark order complete
            if counts['total'] > 0 and counts['total'] == counts['delivered']:
                cur.execute("""
                    UPDATE orders SET is_complete = TRUE, completed_at = NOW(), updated_at = NOW()
                    WHERE order_id = %s
                """, (result['order_id'],))
            
            return {"status": "ok", "shipment": dict(result)}

# =============================================================================
# WAREHOUSE MAPPING
# =============================================================================

@app.get("/warehouse-mapping")
def get_warehouse_mapping():
    """Get all warehouse mappings"""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM warehouse_mapping ORDER BY sku_prefix")
            mappings = cur.fetchall()
            return {"status": "ok", "mappings": mappings}

@app.post("/warehouse-mapping")
def add_warehouse_mapping(mapping: WarehouseMappingUpdate):
    """Add or update warehouse mapping"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO warehouse_mapping (sku_prefix, warehouse_name, warehouse_code)
                VALUES (%s, %s, %s)
                ON CONFLICT (sku_prefix) DO UPDATE SET
                    warehouse_name = EXCLUDED.warehouse_name,
                    warehouse_code = EXCLUDED.warehouse_code
            """, (mapping.sku_prefix.upper(), mapping.warehouse_name, mapping.warehouse_code))
            
            return {"status": "ok", "message": "Mapping saved"}

# =============================================================================
# STATUS SUMMARY
# =============================================================================

@app.get("/shipments/{shipment_id}/rl-quote-data")
def get_rl_quote_data(shipment_id: str):
    """Get pre-populated data for RL Carriers quote"""
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Get shipment and order info
                cur.execute("""
                    SELECT s.*, o.customer_name, o.company_name, o.street, o.city, o.state, o.zip_code,
                           o.phone, o.email, o.order_total, o.total_weight
                    FROM order_shipments s
                    JOIN orders o ON s.order_id = o.order_id
                    WHERE s.shipment_id = %s
                """, (shipment_id,))
                
                shipment = cur.fetchone()
                if not shipment:
                    return {"status": "error", "message": f"Shipment {shipment_id} not found"}
                
                # Get warehouse zip
                warehouse = shipment['warehouse']
                origin_zip = WAREHOUSE_ZIPS.get(warehouse, '')
                
                # If warehouse not in our list, try fuzzy match
                if not origin_zip:
                    warehouse_lower = warehouse.lower().replace(' ', '').replace('&', '').replace('-', '')
                    for wh_name, wh_zip in WAREHOUSE_ZIPS.items():
                        wh_compare = wh_name.lower().replace(' ', '').replace('&', '').replace('-', '')
                        if wh_compare == warehouse_lower or warehouse_lower in wh_compare or wh_compare in warehouse_lower:
                            origin_zip = wh_zip
                            break
                
                # Get line items for this warehouse to check total weight and oversized
                cur.execute("""
                    SELECT sku, product_name, quantity
                    FROM order_line_items
                    WHERE order_id = %s AND warehouse = %s
                """, (shipment['order_id'], warehouse))
                line_items = cur.fetchall()
                
                # Calculate weight for this shipment's items
                total_weight = 0
                has_oversized = False
                oversized_items = []
                
                for item in line_items:
                    # Check for oversized keywords in product_name
                    desc = (item.get('product_name') or '').upper()
                    for keyword in OVERSIZED_KEYWORDS:
                        if keyword in desc:
                            has_oversized = True
                            oversized_items.append(f"{item.get('sku')}: {item.get('product_name')}")
                            break
                
                # Check if single warehouse order
                cur.execute("""
                    SELECT COUNT(DISTINCT warehouse) as warehouse_count
                    FROM order_line_items
                    WHERE order_id = %s AND warehouse IS NOT NULL
                """, (shipment['order_id'],))
                wh_count = cur.fetchone()
                is_single_warehouse = wh_count and wh_count['warehouse_count'] <= 1
                
                # Get order total weight directly from the joined query
                order_weight = float(shipment['total_weight']) if shipment.get('total_weight') else 0
                
                # Clean ZIP code - strip to 5 digits
                dest_zip = shipment.get('zip_code') or ''
                if '-' in dest_zip:
                    dest_zip = dest_zip.split('-')[0]
                dest_zip = dest_zip[:5]  # Take first 5 chars
                
                # Determine weight display
                shipment_weight = float(shipment['weight']) if shipment.get('weight') else None
                needs_manual = False
                weight_note = None
                
                if shipment_weight:
                    weight_note = "from shipment"
                elif is_single_warehouse and order_weight > 0:
                    shipment_weight = round(order_weight, 1)
                    weight_note = "from order"
                elif not is_single_warehouse:
                    needs_manual = True
                    weight_note = "Multi-warehouse - enter weight for this shipment"
                else:
                    needs_manual = True
                    weight_note = "No weight data available"
                
                return {
                    "status": "ok",
                    "shipment_id": shipment_id,
                    "order_id": shipment['order_id'],
                    "warehouse": warehouse,
                    "origin_zip": origin_zip,
                    "destination": {
                        "name": shipment.get('company_name') or shipment.get('customer_name') or '',
                        "street": shipment.get('street') or '',
                        "city": shipment.get('city') or '',
                        "state": shipment.get('state') or '',
                        "zip": dest_zip,
                        "email": shipment.get('email') or '',
                        "phone": shipment.get('phone') or ''
                    },
                    "weight": {
                        "value": shipment_weight,
                        "note": weight_note,
                        "needs_manual_entry": needs_manual
                    },
                    "oversized": {
                        "detected": has_oversized,
                        "items": oversized_items
                    },
                    "existing_quote": {
                        "quote_number": shipment.get('rl_quote_number'),
                        "quote_price": float(shipment['rl_quote_price']) if shipment.get('rl_quote_price') else None,
                        "customer_price": float(shipment['rl_customer_price']) if shipment.get('rl_customer_price') else None,
                        "quote_url": shipment.get('quote_url')
                    },
                    "rl_quote_url": "https://www.rlcarriers.com/freight/shipping/rate-quote"
                }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/orders/status/summary")
def status_summary():
    """Get count of orders by status"""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
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
            """)
            summary = cur.fetchall()
            return {"status": "ok", "summary": summary}

@app.get("/orders/{order_id}/events")
def get_order_events(order_id: str):
    """Get event history for an order"""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM order_events 
                WHERE order_id = %s 
                ORDER BY created_at DESC
            """, (order_id,))
            events = cur.fetchall()
            return {"status": "ok", "events": events}

# =============================================================================
# TRUSTED CUSTOMERS
# =============================================================================

@app.get("/trusted-customers")
def list_trusted_customers():
    """List all trusted customers"""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM trusted_customers ORDER BY customer_name")
            customers = cur.fetchall()
            return {"status": "ok", "customers": customers}

@app.post("/trusted-customers")
def add_trusted_customer(customer_name: str, company_name: Optional[str] = None, notes: Optional[str] = None):
    """Add a trusted customer"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trusted_customers (customer_name, company_name, notes)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (customer_name, company_name, notes))
            new_id = cur.fetchone()[0]
            return {"status": "ok", "id": new_id}

@app.delete("/orders/{order_id}")
def delete_order(order_id: str):
    """Delete an order and its shipments"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM order_shipments WHERE order_id = %s", (order_id,))
            cur.execute("DELETE FROM order_line_items WHERE order_id = %s", (order_id,))
            cur.execute("DELETE FROM orders WHERE order_id = %s", (order_id,))
            conn.commit()
    return {"status": "ok", "message": f"Order {order_id} deleted"}
@app.delete("/trusted-customers/{customer_id}")
def remove_trusted_customer(customer_id: int):
    """Remove a trusted customer"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM trusted_customers WHERE id = %s", (customer_id,))
            return {"status": "ok"}

def is_trusted_customer(conn, customer_name: str, company_name: str = None) -> bool:
    """Check if customer is in trusted list"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM trusted_customers 
            WHERE LOWER(customer_name) = LOWER(%s)
            OR (company_name IS NOT NULL AND LOWER(company_name) = LOWER(%s))
        """, (customer_name, company_name or ''))
        return cur.fetchone() is not None

# =============================================================================
# ALERTS
# =============================================================================

@app.get("/alerts")
def list_alerts(include_resolved: bool = False):
    """List order alerts"""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT a.*, o.customer_name, o.company_name, o.order_total
                FROM order_alerts a
                JOIN orders o ON a.order_id = o.order_id
            """
            if not include_resolved:
                query += " WHERE NOT a.is_resolved"
            query += " ORDER BY a.created_at DESC"
            
            cur.execute(query)
            alerts = cur.fetchall()
            return {"status": "ok", "alerts": alerts}

@app.post("/alerts")
def create_alert(order_id: str, alert_type: str, alert_message: str):
    """Create an alert for an order"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO order_alerts (order_id, alert_type, alert_message)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (order_id, alert_type, alert_message))
            new_id = cur.fetchone()[0]
            return {"status": "ok", "id": new_id}

@app.patch("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int):
    """Resolve an alert"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE order_alerts 
                SET is_resolved = TRUE, resolved_at = NOW()
                WHERE id = %s
            """, (alert_id,))
            return {"status": "ok"}

# =============================================================================
# RL QUOTE DETECTION
# =============================================================================

@app.post("/detect-rl-quote")
def detect_rl_quote(order_id: str, email_body: str):
    """Detect R+L quote number from email"""
    # Pattern: "RL Quote No: 9075654" or "Quote: 9075654" or "Quote #9075654"
    quote_match = re.search(r'(?:RL\s+)?Quote\s*(?:No|#)?[:\s]*(\d{6,10})', email_body, re.IGNORECASE)
    
    if quote_match:
        quote_no = quote_match.group(1)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE orders SET rl_quote_no = %s, updated_at = NOW()
                    WHERE order_id = %s
                """, (quote_no, order_id))
                
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'rl_quote_captured', %s, 'email_detection')
                """, (order_id, json.dumps({'quote_no': quote_no})))
                
                return {"status": "ok", "quote_no": quote_no}
    
    return {"status": "ok", "quote_no": None, "message": "No quote number found"}

@app.post("/detect-pro-number")
def detect_pro_number(order_id: str, email_body: str):
    """Detect R+L PRO number from email"""
    # Pattern: "PRO 74408602-5" or "PRO# 74408602-5" or "Pro Number: 74408602-5"
    pro_match = re.search(r'PRO\s*(?:#|Number)?[:\s]*([A-Z]{0,2}\d{8,10}(?:-\d)?)', email_body, re.IGNORECASE)
    
    if pro_match:
        pro_no = pro_match.group(1).upper()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE orders SET pro_number = %s, tracking = %s, updated_at = NOW()
                    WHERE order_id = %s
                """, (pro_no, f"R+L PRO {pro_no}", order_id))
                
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'pro_number_captured', %s, 'email_detection')
                """, (order_id, json.dumps({'pro_number': pro_no})))
                
                return {"status": "ok", "pro_number": pro_no}
    
    return {"status": "ok", "pro_number": None, "message": "No PRO number found"}

# =============================================================================
# TRUSTED CUSTOMER ALERT CHECK
# =============================================================================

@app.post("/check-payment-alerts")
def check_payment_alerts():
    """
    Check for trusted customers who shipped but haven't paid after 1 business day.
    Should be called periodically (e.g., daily at 9 AM).
    """
    alerts_created = 0
    
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Find orders: sent to warehouse, not paid, trusted customer, > 1 day old
            cur.execute("""
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
            """)
            
            orders = cur.fetchall()
            
            for order in orders:
                cur.execute("""
                    INSERT INTO order_alerts (order_id, alert_type, alert_message)
                    VALUES (%s, 'trusted_unpaid', %s)
                """, (
                    order['order_id'],
                    f"Trusted customer {order['customer_name']} - shipped but unpaid for 1+ day. Total: ${order['order_total']}"
                ))
                alerts_created += 1
    
    return {"status": "ok", "alerts_created": alerts_created}

# =============================================================================
# CHECKOUT FLOW - B2BWave Order + R+L Shipping + Square Payment
# =============================================================================

# Import checkout module
try:
    from checkout import (
        calculate_order_shipping, fetch_b2bwave_order, 
        create_square_payment_link, generate_checkout_token,
        verify_checkout_token, WAREHOUSES
    )
    CHECKOUT_ENABLED = True
except ImportError as e:
    print(f"[STARTUP] checkout module not found: {e}")
    CHECKOUT_ENABLED = False

CHECKOUT_BASE_URL = os.environ.get("CHECKOUT_BASE_URL", "").strip()
GMAIL_SEND_ENABLED = os.environ.get("GMAIL_SEND_ENABLED", "false").lower() == "true"


@app.get("/checkout-status")
def checkout_status():
    """Debug endpoint to check checkout configuration"""
    # Import the checkout module's config to see what it has
    try:
        from checkout import B2BWAVE_URL as CHECKOUT_B2BWAVE_URL
        from checkout import B2BWAVE_USERNAME as CHECKOUT_B2BWAVE_USERNAME
        from checkout import B2BWAVE_API_KEY as CHECKOUT_B2BWAVE_API_KEY
        checkout_b2bwave = f"{CHECKOUT_B2BWAVE_URL} / {CHECKOUT_B2BWAVE_USERNAME} / {'set' if CHECKOUT_B2BWAVE_API_KEY else 'not set'}"
    except:
        checkout_b2bwave = "import failed"
    
    return {
        "checkout_enabled": CHECKOUT_ENABLED,
        "checkout_base_url": CHECKOUT_BASE_URL or "(not set)",
        "gmail_send_enabled": GMAIL_SEND_ENABLED,
        "checkout_b2bwave_config": checkout_b2bwave,
        "main_b2bwave_url": B2BWAVE_URL or "(not set)"
    }


@app.get("/debug/b2bwave-raw/{order_id}")
def debug_b2bwave_raw(order_id: str):
    """Debug endpoint to see raw B2BWave API response"""
    try:
        data = b2bwave_api_request("orders", {"id_eq": order_id})
        return {"status": "ok", "raw_response": data}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/debug/warehouse-routing/{order_id}")
def debug_warehouse_routing(order_id: str):
    """Debug endpoint to test warehouse routing for an order - no token required"""
    try:
        from checkout import group_items_by_warehouse, get_warehouse_for_sku, WAREHOUSES
        
        # Fetch order from B2BWave
        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "error": "Order not found in B2BWave"}
        
        line_items = order_data.get('line_items', [])
        
        # Show warehouse routing for each item
        item_routing = []
        for item in line_items:
            sku = item.get('sku', '')
            warehouse = get_warehouse_for_sku(sku)
            item_routing.append({
                "sku": sku,
                "name": item.get('product_name', ''),
                "qty": item.get('quantity', 0),
                "warehouse": warehouse,
                "warehouse_info": WAREHOUSES.get(warehouse, {}) if warehouse else None
            })
        
        # Group by warehouse
        warehouse_groups = group_items_by_warehouse(line_items)
        
        return {
            "status": "ok",
            "order_id": order_id,
            "customer": order_data.get('customer_name', ''),
            "total_items": len(line_items),
            "item_routing": item_routing,
            "warehouse_groups": {
                wh: {
                    "warehouse_info": WAREHOUSES.get(wh, {}),
                    "item_count": len(items),
                    "items": [{"sku": i.get('sku'), "name": i.get('product_name'), "qty": i.get('quantity')} for i in items]
                }
                for wh, items in warehouse_groups.items()
            }
        }
    except Exception as e:
        import traceback
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


class CheckoutRequest(BaseModel):
    order_id: str
    shipping_address: Optional[dict] = None


@app.get("/debug/test-checkout/{order_id}")
def debug_test_checkout(order_id: str):
    """
    Debug endpoint to test full checkout flow without webhook.
    Generates token and returns checkout URL + shipping data.
    """
    try:
        from checkout import generate_checkout_token, fetch_b2bwave_order, calculate_order_shipping
        
        # Fetch order from B2BWave
        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "error": "Order not found in B2BWave"}
        
        # Generate token
        token = generate_checkout_token(order_id)
        
        # Get shipping address
        shipping_address = order_data.get('shipping_address') or order_data.get('delivery_address') or {}
        
        # Calculate shipping
        shipping_result = calculate_order_shipping(order_data, shipping_address)
        
        # Build checkout URL
        checkout_base = os.environ.get("CHECKOUT_BASE_URL", "https://cfcorderbackend-sandbox.onrender.com")
        checkout_url = f"{checkout_base}/checkout-ui/{order_id}?token={token}"
        
        return {
            "status": "ok",
            "order_id": order_id,
            "customer": order_data.get('customer_name'),
            "customer_email": order_data.get('customer_email'),
            "token": token,
            "checkout_url": checkout_url,
            "api_url": f"{checkout_base}/checkout/{order_id}?token={token}",
            "destination": shipping_address,
            "shipping": shipping_result
        }
    except Exception as e:
        import traceback
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


@app.post("/webhook/b2bwave-order")
def b2bwave_order_webhook(payload: dict):
    """
    Webhook endpoint for B2BWave - triggered when order is placed.
    Calculates shipping and sends checkout email to customer.
    """
    if not CHECKOUT_ENABLED:
        return {"status": "error", "message": "Checkout module not enabled"}
    
    order_id = payload.get('id') or payload.get('order_id')
    customer_email = payload.get('customer_email') or payload.get('email')
    
    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order_id")
    
    # Generate checkout token
    token = generate_checkout_token(str(order_id))
    checkout_url = f"{CHECKOUT_BASE_URL}/checkout?order={order_id}&token={token}"
    
    # Store pending checkout in database
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pending_checkouts (order_id, customer_email, checkout_token, created_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (order_id) DO UPDATE SET 
                    customer_email = EXCLUDED.customer_email,
                    checkout_token = EXCLUDED.checkout_token,
                    created_at = NOW()
            """, (str(order_id), customer_email, token))
    
    # TODO: Send email with checkout link
    # For now, just return the URL
    
    return {
        "status": "ok",
        "order_id": order_id,
        "checkout_url": checkout_url,
        "message": "Checkout link generated"
    }


@app.get("/checkout/payment-complete")
def payment_complete(order: str, transactionId: Optional[str] = None):
    """
    Payment completion callback from Square.
    """
    # Mark checkout as complete
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pending_checkouts 
                SET payment_completed_at = NOW(), transaction_id = %s
                WHERE order_id = %s
            """, (transactionId, order))
            
            # Also update the main order if it exists
            cur.execute("""
                UPDATE orders 
                SET payment_received = TRUE, 
                    payment_received_at = NOW(),
                    payment_method = 'Square Checkout',
                    updated_at = NOW()
                WHERE order_id = %s
            """, (order,))
    
    return {
        "status": "ok",
        "message": "Payment completed",
        "order_id": order
    }


@app.get("/checkout/{order_id}")
def get_checkout_data(order_id: str, token: str):
    """
    Get checkout page data - order details with shipping quotes.
    Called by the checkout frontend page.
    """
    if not CHECKOUT_ENABLED:
        raise HTTPException(status_code=503, detail="Checkout not enabled")
    
    # Verify token
    if not verify_checkout_token(order_id, token):
        raise HTTPException(status_code=403, detail="Invalid or expired checkout link")
    
    # Fetch order from B2BWave
    order_data = fetch_b2bwave_order(order_id)
    if not order_data:
        raise HTTPException(status_code=404, detail="Order not found")
    
    # Extract shipping address
    shipping_address = order_data.get('shipping_address') or order_data.get('delivery_address') or {}
    
    # Calculate shipping
    shipping_result = calculate_order_shipping(order_data, shipping_address)
    
    return {
        "status": "ok",
        "order_id": order_id,
        "order": {
            "id": order_id,
            "customer_name": order_data.get('customer_name'),
            "customer_email": order_data.get('customer_email'),
            "company_name": order_data.get('company_name'),
            "line_items": order_data.get('line_items', []),
            "subtotal": order_data.get('subtotal') or order_data.get('total_price'),
        },
        "shipping": shipping_result,
        "payment_ready": shipping_result.get('grand_total', 0) > 0
    }


@app.post("/checkout/{order_id}/create-payment")
def create_checkout_payment(order_id: str, token: str):
    """
    Create Square payment link for the order.
    Called after customer reviews shipping and clicks Pay.
    """
    if not CHECKOUT_ENABLED:
        raise HTTPException(status_code=503, detail="Checkout not enabled")
    
    # Verify token
    if not verify_checkout_token(order_id, token):
        raise HTTPException(status_code=403, detail="Invalid checkout token")
    
    # Get checkout data to calculate total
    order_data = fetch_b2bwave_order(order_id)
    if not order_data:
        raise HTTPException(status_code=404, detail="Order not found")
    
    shipping_address = order_data.get('shipping_address') or order_data.get('delivery_address') or {}
    shipping_result = calculate_order_shipping(order_data, shipping_address)
    
    grand_total = shipping_result.get('grand_total', 0)
    if grand_total <= 0:
        raise HTTPException(status_code=400, detail="Invalid order total")
    
    # Create Square payment link
    amount_cents = int(grand_total * 100)
    customer_email = order_data.get('customer_email', '')
    
    payment_url = create_square_payment_link(amount_cents, order_id, customer_email)
    
    if not payment_url:
        raise HTTPException(status_code=500, detail="Failed to create payment link")
    
    # Store payment attempt
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pending_checkouts 
                SET payment_link = %s, payment_amount = %s, payment_initiated_at = NOW()
                WHERE order_id = %s
            """, (payment_url, grand_total, order_id))
    
    return {
        "status": "ok",
        "payment_url": payment_url,
        "amount": grand_total
    }


@app.get("/checkout-ui/{order_id}")
def checkout_ui(order_id: str, token: str):
    """
    Serve the checkout page HTML.
    This is a simple HTML page that calls the API endpoints.
    """
    if not verify_checkout_token(order_id, token):
        return HTMLResponse(content="<h1>Invalid or expired checkout link</h1>", status_code=403)
    
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
                
                if (data.status !== 'ok') {{
                    throw new Error(data.detail || 'Failed to load order');
                }}
                
                renderCheckout(data);
            }} catch (err) {{
                document.getElementById('content').innerHTML = `<div class="error">Error: ${{err.message}}</div>`;
            }}
        }}
        
        function renderCheckout(data) {{
            const order = data.order;
            const shipping = data.shipping;
            
            let html = `
                <h2>Order #${{ORDER_ID}}</h2>
                <p style="color:#666; margin-bottom:20px;">
                    ${{order.customer_name || ''}} ${{order.company_name ? '(' + order.company_name + ')' : ''}}
                </p>
                
                <h2>Items</h2>
            `;
            
            // Line items
            (order.line_items || []).forEach(item => {{
                const price = parseFloat(item.price || item.unit_price || 0);
                const qty = parseInt(item.quantity || 1);
                html += `
                    <div class="item">
                        <div class="item-name">${{item.name || item.product_name || item.sku}}</div>
                        <div class="item-qty">x${{qty}}</div>
                        <div class="item-price">$${{(price * qty).toFixed(2)}}</div>
                    </div>
                `;
            }});
            
            // Shipping
            html += `<h2>Shipping</h2>`;
            
            if (shipping.shipments && shipping.shipments.length > 0) {{
                shipping.shipments.forEach(ship => {{
                    const quoteOk = ship.quote && ship.quote.success;
                    const methodLabel = ship.shipping_method === 'small_package' ? '📦 UPS/USPS' : '🚚 LTL Freight';
                    const methodNote = ship.shipping_method === 'small_package' ? 
                        (ship.quote && ship.quote.cheapest ? `via ${{ship.quote.cheapest.provider}} ${{ship.quote.cheapest.service}}` : '') :
                        '(R+L Carriers)';
                    html += `
                        <div class="shipment">
                            <div class="shipment-header">📦 From: ${{ship.warehouse_name}} (${{ship.origin_zip}})</div>
                            <div class="shipment-detail">
                                ${{ship.items.length}} item(s) · ${{ship.weight}} lbs
                                ${{ship.is_oversized ? ' · <strong>Oversized</strong>' : ''}}
                            </div>
                            <div class="shipment-detail" style="margin-top:8px;">
                                ${{quoteOk ? 
                                    `<strong>Shipping: $${{ship.shipping_cost.toFixed(2)}}</strong> <span style="color:#666; font-size:0.9em;">${{methodLabel}} ${{methodNote}}</span>` : 
                                    `<span style="color:#c00">Quote unavailable</span>`
                                }}
                            </div>
                        </div>
                    `;
                }});
                
                // Show residential note only for LTL shipments
                const hasLtl = shipping.shipments.some(s => s.shipping_method === 'ltl');
                if (hasLtl) {{
                    html += `<div class="residential-note">🏠 Residential delivery includes liftgate service</div>`;
                }}
            }}
            
            // Totals
            html += `
                <div class="totals">
                    <div class="total-row">
                        <span>Items Subtotal</span>
                        <span>$${{shipping.total_items.toFixed(2)}}</span>
                    </div>
                    <div class="total-row">
                        <span>Shipping</span>
                        <span>$${{shipping.total_shipping.toFixed(2)}}</span>
                    </div>
                    <div class="total-row grand">
                        <span>Total</span>
                        <span>$${{shipping.grand_total.toFixed(2)}}</span>
                    </div>
                </div>
                
                <button class="pay-button" onclick="initiatePayment()" id="payBtn">
                    Pay $${{shipping.grand_total.toFixed(2)}} with Card
                </button>
            `;
            
            document.getElementById('content').innerHTML = html;
        }}
        
        async function initiatePayment() {{
            const btn = document.getElementById('payBtn');
            btn.disabled = true;
            btn.textContent = 'Creating payment link...';
            
            try {{
                const resp = await fetch(`${{API_BASE}}/checkout/${{ORDER_ID}}/create-payment?token=${{TOKEN}}`, {{
                    method: 'POST'
                }});
                const data = await resp.json();
                
                if (data.payment_url) {{
                    window.location.href = data.payment_url;
                }} else {{
                    throw new Error(data.detail || 'Failed to create payment');
                }}
            }} catch (err) {{
                alert('Payment error: ' + err.message);
                btn.disabled = false;
                btn.textContent = 'Pay with Card';
            }}
        }}
        
        loadCheckout();
    </script>
</body>
</html>
    """
    
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


# Add HTMLResponse import at top of file
from fastapi.responses import HTMLResponse


# =============================================================================
# SERVER STARTUP
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

