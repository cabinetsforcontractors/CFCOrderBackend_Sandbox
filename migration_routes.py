"""
migration_routes.py
FastAPI router for DB migrations and debug schema endpoints.
All endpoints require admin token (X-Admin-Token header).
"""

from fastapi import APIRouter, Depends
from auth import require_admin
from db_helpers import get_db

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
        add_weight_column as _add_weight_column,
        add_is_residential_to_shipments as _add_is_residential,
        add_address_pending_to_checkouts as _add_address_pending,
        add_address_classification_to_checkouts as _add_address_classification,
        add_bol_columns_to_shipments as _add_bol_columns,
        add_ws6_supplier_workflow_fields as _add_ws6_fields,
        add_ws6_pickup_fields as _add_ws6_pickup_fields,
    )
    DB_MIGRATIONS_LOADED = True
except ImportError:
    DB_MIGRATIONS_LOADED = False

try:
    from schema import SCHEMA_SQL
    SCHEMA_LOADED = True
except ImportError:
    SCHEMA_LOADED = False
    SCHEMA_SQL = "-- Schema not loaded"


migration_router = APIRouter(tags=["migrations"])


def _run(fn):
    if DB_MIGRATIONS_LOADED:
        return fn()
    return {"status": "error", "message": "db_migrations module not loaded"}


# =============================================================================
# MIGRATION ENDPOINTS
# =============================================================================

@migration_router.post("/create-pending-checkouts-table")
def create_pending_checkouts_table(_: bool = Depends(require_admin)):
    return _run(_create_pending_checkouts)


@migration_router.post("/create-shipments-table")
def create_shipments_table(_: bool = Depends(require_admin)):
    return _run(_create_shipments)


@migration_router.post("/add-rl-fields")
def add_rl_shipping_fields(_: bool = Depends(require_admin)):
    return _run(_add_rl_fields)


@migration_router.post("/add-ps-fields")
def add_ps_fields(_: bool = Depends(require_admin)):
    return _run(_add_ps_fields)


@migration_router.post("/fix-shipment-columns")
def fix_shipment_columns(_: bool = Depends(require_admin)):
    return _run(_fix_shipment_columns)


@migration_router.post("/fix-sku-columns")
def fix_sku_columns(_: bool = Depends(require_admin)):
    return _run(_fix_sku_columns)


@migration_router.post("/fix-order-id-length")
def fix_order_id_length(_: bool = Depends(require_admin)):
    return _run(_fix_order_id_length)


@migration_router.post("/recreate-order-status-view")
def recreate_order_status_view(_: bool = Depends(require_admin)):
    return _run(_recreate_order_status_view)


@migration_router.post("/add-weight-column")
def add_weight_column(_: bool = Depends(require_admin)):
    return _run(_add_weight_column)


@migration_router.post("/add-is-residential")
def add_is_residential(_: bool = Depends(require_admin)):
    """Add is_residential column to order_shipments (WS6 — Smarty residential detection)."""
    return _run(_add_is_residential)


@migration_router.post("/add-address-pending")
def add_address_pending(_: bool = Depends(require_admin)):
    """Add address_pending + address_validation_error to pending_checkouts."""
    return _run(_add_address_pending)


@migration_router.post("/add-address-classification")
def add_address_classification(_: bool = Depends(require_admin)):
    """
    Add address classification columns to pending_checkouts.
    WS6 — drives the multi-step customer checkout flow.
    """
    return _run(_add_address_classification)


@migration_router.post("/add-bol-columns")
def add_bol_columns(_: bool = Depends(require_admin)):
    """
    Add bol_url and bol_number columns to order_shipments.
    Phase 8 — BOL generation via R+L API.
    """
    return _run(_add_bol_columns)


@migration_router.post("/add-ws6-supplier-fields")
def add_ws6_supplier_fields(_: bool = Depends(require_admin)):
    """
    WS6 Phase 9 — Add supplier workflow columns to order_shipments and orders:
      order_shipments.quote_number  — R+L rate quote number (saved at checkout, passed in BOL)
      order_shipments.close_time   — pickup window close time from supplier form
      order_shipments.pickup_scheduled_email_sent — customer pickup email flag
      orders.pro_number             — R+L PRO (separate from orders.tracking)
    Run once after deploy. Safe to re-run.
    """
    return _run(_add_ws6_fields)


@migration_router.post("/add-ws6-pickup-fields")
def add_ws6_pickup_fields(_: bool = Depends(require_admin)):
    """
    WS6 Warehouse Pickup — Add pickup workflow columns:
      order_shipments.pickup_type               — 'freight' or 'warehouse_pickup'
      order_shipments.pickup_ready_date         — when supplier says order is ready
      order_shipments.pickup_ready_time         — time ready for pickup
      order_shipments.customer_notified_ready_at — when customer was emailed
      order_shipments.pickup_confirm_poll_sent_at — when CFC asked 'Has customer picked up?'
      order_shipments.customer_pickup_confirmed  — TRUE when supplier confirms collected
      orders.is_pickup                           — TRUE for warehouse pickup orders
    Run once after deploy. Safe to re-run.
    """
    return _run(_add_ws6_pickup_fields)


@migration_router.post("/init-db")
def init_db(_: bool = Depends(require_admin)):
    """Initialize database schema (DESTRUCTIVE)."""
    if not SCHEMA_LOADED:
        return {"status": "error", "message": "Schema module not loaded"}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
    return {"status": "ok", "message": "Database schema initialized", "version": "5.8.0"}


# =============================================================================
# DEBUG
# =============================================================================

@migration_router.get("/debug/orders-columns")
def debug_orders_columns(_: bool = Depends(require_admin)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'orders'
                ORDER BY ordinal_position
            """)
            orders_cols = cur.fetchall()
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'pending_checkouts'
                ORDER BY ordinal_position
            """)
            checkout_cols = cur.fetchall()
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'order_shipments'
                ORDER BY ordinal_position
            """)
            shipment_cols = cur.fetchall()
            return {
                "orders_columns": [c[0] for c in orders_cols],
                "pending_checkouts_columns": [c[0] for c in checkout_cols],
                "order_shipments_columns": [c[0] for c in shipment_cols],
            }


@migration_router.get("/debug/shipment/{order_id}")
def debug_shipment(order_id: str, _: bool = Depends(require_admin)):
    """Show all order_shipments rows for an order_id, plus whether the orders row exists."""
    with get_db() as conn:
        with conn.cursor() as cur:
            # Check if order row exists
            cur.execute("SELECT order_id, customer_name, email FROM orders WHERE order_id = %s", (order_id,))
            order_row = cur.fetchone()

            # Get all shipments for this order
            cur.execute("""
                SELECT shipment_id, warehouse, status, pickup_type,
                       supplier_token, supplier_poll_1_sent_at,
                       pickup_ready_date, customer_pickup_confirmed,
                       created_at
                FROM order_shipments WHERE order_id = %s
                ORDER BY created_at
            """, (order_id,))
            rows = cur.fetchall()

            return {
                "order_id": order_id,
                "order_row_exists": order_row is not None,
                "order_customer": order_row[1] if order_row else None,
                "shipment_count": len(rows),
                "shipments": [
                    {
                        "shipment_id": r[0],
                        "warehouse": r[1],
                        "status": r[2],
                        "pickup_type": r[3],
                        "supplier_token": "SET" if r[4] else "NULL",
                        "poll_sent_at": str(r[5]) if r[5] else None,
                        "pickup_ready_date": str(r[6]) if r[6] else None,
                        "pickup_confirmed": r[7],
                        "created_at": str(r[8]),
                    }
                    for r in rows
                ],
            }
