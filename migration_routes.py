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

    WS6 — drives the multi-step customer checkout flow:
      address_classification_needed  — Cases B/C: customer must classify
      address_initially_found        — FALSE = Smarty found nothing, open edit box
      address_type_confirmed         — what customer selected in Step 2
      is_residential_customer_confirmed — derived bool, used as shipping override
    """
    return _run(_add_address_classification)


@migration_router.post("/init-db")
def init_db(_: bool = Depends(require_admin)):
    """Initialize database schema (DESTRUCTIVE)."""
    if not SCHEMA_LOADED:
        return {"status": "error", "message": "Schema module not loaded"}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
    return {"status": "ok", "message": "Database schema initialized", "version": "5.7.0"}


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
            return {
                "orders_columns": [c[0] for c in orders_cols],
                "pending_checkouts_columns": [c[0] for c in checkout_cols],
            }
