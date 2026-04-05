"""
migration_routes.py
FastAPI router for DB migrations and debug schema endpoints.

Phase 5B: Extracted from main.py

All endpoints require admin token (X-Admin-Token: <token> header).

Mount in main.py with:
    from migration_routes import migration_router
    app.include_router(migration_router)

Endpoints:
    POST /create-pending-checkouts-table
    POST /create-shipments-table
    POST /add-rl-fields
    POST /add-ps-fields
    POST /fix-shipment-columns
    POST /fix-sku-columns
    POST /fix-order-id-length
    POST /recreate-order-status-view
    POST /add-weight-column
    POST /add-is-residential
    POST /init-db
    GET  /debug/orders-columns
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


def _run_migration(fn):
    """Wrapper that handles missing db_migrations module gracefully."""
    if DB_MIGRATIONS_LOADED:
        return fn()
    return {"status": "error", "message": "db_migrations module not loaded"}


# =============================================================================
# MIGRATION ENDPOINTS  (all admin-gated)
# =============================================================================

@migration_router.post("/create-pending-checkouts-table")
def create_pending_checkouts_table(_: bool = Depends(require_admin)):
    """Create pending_checkouts table for B2BWave checkout flow."""
    return _run_migration(_create_pending_checkouts)


@migration_router.post("/create-shipments-table")
def create_shipments_table(_: bool = Depends(require_admin)):
    """Create order_shipments table."""
    return _run_migration(_create_shipments)


@migration_router.post("/add-rl-fields")
def add_rl_shipping_fields(_: bool = Depends(require_admin)):
    """Add R+L Carriers shipping fields."""
    return _run_migration(_add_rl_fields)


@migration_router.post("/add-ps-fields")
def add_ps_fields(_: bool = Depends(require_admin)):
    """Add Pirateship fields."""
    return _run_migration(_add_ps_fields)


@migration_router.post("/fix-shipment-columns")
def fix_shipment_columns(_: bool = Depends(require_admin)):
    """Fix column lengths in order_shipments."""
    return _run_migration(_fix_shipment_columns)


@migration_router.post("/fix-sku-columns")
def fix_sku_columns(_: bool = Depends(require_admin)):
    """Fix SKU column lengths."""
    return _run_migration(_fix_sku_columns)


@migration_router.post("/fix-order-id-length")
def fix_order_id_length(_: bool = Depends(require_admin)):
    """Increase order_id column length."""
    return _run_migration(_fix_order_id_length)


@migration_router.post("/recreate-order-status-view")
def recreate_order_status_view(_: bool = Depends(require_admin)):
    """Recreate the order_status view."""
    return _run_migration(_recreate_order_status_view)


@migration_router.post("/add-weight-column")
def add_weight_column(_: bool = Depends(require_admin)):
    """Add total_weight column."""
    return _run_migration(_add_weight_column)


@migration_router.post("/add-is-residential")
def add_is_residential(_: bool = Depends(require_admin)):
    """Add is_residential column to order_shipments (WS6 — Smarty residential detection)."""
    return _run_migration(_add_is_residential)


@migration_router.post("/init-db")
def init_db(_: bool = Depends(require_admin)):
    """Initialize database schema (DESTRUCTIVE — drops and recreates)."""
    if not SCHEMA_LOADED:
        return {"status": "error", "message": "Schema module not loaded"}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
    return {"status": "ok", "message": "Database schema initialized", "version": "5.6.1"}


# =============================================================================
# DEBUG / SCHEMA INSPECTION  (admin-gated)
# =============================================================================

@migration_router.get("/debug/orders-columns")
def debug_orders_columns(_: bool = Depends(require_admin)):
    """Check what columns exist in orders and order_status tables."""
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
                "view_columns": (
                    [c[0] for c in view_columns]
                    if view_columns
                    else "view does not exist"
                ),
            }
