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
    return _run(_add_is_residential)


@migration_router.post("/add-address-pending")
def add_address_pending(_: bool = Depends(require_admin)):
    return _run(_add_address_pending)


@migration_router.post("/add-address-classification")
def add_address_classification(_: bool = Depends(require_admin)):
    return _run(_add_address_classification)


@migration_router.post("/add-bol-columns")
def add_bol_columns(_: bool = Depends(require_admin)):
    return _run(_add_bol_columns)


@migration_router.post("/add-ws6-supplier-fields")
def add_ws6_supplier_fields(_: bool = Depends(require_admin)):
    return _run(_add_ws6_fields)


@migration_router.post("/add-ws6-pickup-fields")
def add_ws6_pickup_fields(_: bool = Depends(require_admin)):
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
            cur.execute("SELECT order_id, customer_name, email FROM orders WHERE order_id = %s", (order_id,))
            order_row = cur.fetchone()

            cur.execute("""
                SELECT shipment_id, warehouse, status, pickup_type,
                       supplier_token, supplier_poll_1_sent_at,
                       pickup_ready_date, customer_pickup_confirmed,
                       created_at
                FROM order_shipments WHERE order_id = %s
                ORDER BY created_at
            """, (order_id,))
            rows = cur.fetchall()

            cur.execute(
                "SELECT shipment_id, order_id, pickup_type FROM order_shipments WHERE shipment_id LIKE %s",
                (f"{order_id}-%",)
            )
            pattern_rows = cur.fetchall()

            return {
                "order_id": order_id,
                "order_row_exists": order_row is not None,
                "order_customer": order_row[1] if order_row else None,
                "shipment_count_by_order_id": len(rows),
                "shipment_count_by_pattern": len(pattern_rows),
                "shipments_by_order_id": [
                    {
                        "shipment_id": r[0], "warehouse": r[1], "status": r[2],
                        "pickup_type": r[3], "has_token": bool(r[4]),
                        "poll_sent": str(r[5]) if r[5] else None,
                        "pickup_ready": str(r[6]) if r[6] else None,
                        "confirmed": r[7], "created": str(r[8]),
                    }
                    for r in rows
                ],
                "shipments_by_pattern": [
                    {"shipment_id": r[0], "order_id": r[1], "pickup_type": r[2]}
                    for r in pattern_rows
                ],
            }


@migration_router.post("/debug/insert-pickup-shipment/{order_id}")
def debug_insert_pickup_shipment(order_id: str, _: bool = Depends(require_admin)):
    """
    Debug: manually attempt a pickup shipment INSERT for order_id.
    Returns the exact error if it fails — use this to diagnose constraint issues.
    NOTE: quote_number intentionally excluded — pickups have no R+L quote.
    """
    shipment_id = f"{order_id}-Cabinetry-Distribution"
    results = {}

    # Step 1: Check orders row
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT order_id FROM orders WHERE order_id = %s", (order_id,))
                row = cur.fetchone()
                results["orders_row_exists"] = row is not None
    except Exception as e:
        results["orders_check_error"] = str(e)

    # Step 2: Check if shipment already exists
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, order_id FROM order_shipments WHERE shipment_id = %s", (shipment_id,))
                row = cur.fetchone()
                results["shipment_already_exists"] = row is not None
                results["existing_shipment_order_id"] = row[1] if row else None
    except Exception as e:
        results["shipment_check_error"] = str(e)

    # Step 3: Attempt INSERT WITH pickup_type (no quote_number — pickups have no R+L quote)
    if not results.get("shipment_already_exists"):
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO order_shipments
                           (order_id, shipment_id, warehouse, status, origin_zip,
                            weight, has_oversized, is_residential, pickup_type)
                           VALUES (%s, %s, 'Cabinetry Distribution', 'needs_order', '32148',
                                   600, FALSE, FALSE, 'warehouse_pickup')
                           RETURNING id""",
                        (order_id, shipment_id)
                    )
                    row = cur.fetchone()
                    results["insert_with_pickup_type"] = "SUCCESS"
                    results["new_shipment_id_pk"] = row[0] if row else None
        except Exception as e:
            results["insert_with_pickup_type"] = f"FAILED: {str(e)}"

            # Fallback: without pickup_type column
            try:
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO order_shipments
                               (order_id, shipment_id, warehouse, status, origin_zip,
                                weight, has_oversized, is_residential)
                               VALUES (%s, %s, 'Cabinetry Distribution', 'needs_order', '32148',
                                       600, FALSE, FALSE)
                               RETURNING id""",
                            (order_id, shipment_id)
                        )
                        row = cur.fetchone()
                        results["insert_fallback"] = "SUCCESS"
                        results["new_shipment_id_pk"] = row[0] if row else None
            except Exception as e2:
                results["insert_fallback"] = f"FAILED: {str(e2)}"
    else:
        results["insert_skipped"] = "Shipment already exists"

    return results


@migration_router.post("/debug/b2bwave-status-check")
def debug_b2bwave_status_check(_: bool = Depends(require_admin)):
    """TEMP: Check B2BWave order statuses and status list. Remove after investigation."""
    import os, base64, urllib.request, json as _json

    B2BWAVE_URL = os.environ.get("B2BWAVE_URL", "").strip().rstrip("/")
    B2BWAVE_USERNAME = os.environ.get("B2BWAVE_USERNAME", "").strip()
    B2BWAVE_API_KEY = os.environ.get("B2BWAVE_API_KEY", "").strip()

    if not all([B2BWAVE_URL, B2BWAVE_USERNAME, B2BWAVE_API_KEY]):
        return {"error": "B2BWave env vars not set"}

    creds = base64.b64encode(f"{B2BWAVE_USERNAME}:{B2BWAVE_API_KEY}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}
    results = {}

    for label, path in [
        ("order_statuses", "/api/order_statuses.json"),
        ("recent_orders_sample", "/api/orders.json?submitted_at_gteq=2026-03-01"),
    ]:
        url = B2BWAVE_URL + path
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
                if label == "recent_orders_sample" and isinstance(data, list):
                    # Extract just status info from each order
                    status_info = []
                    seen_statuses = {}
                    for item in data:
                        order = item.get("order", item)
                        sid = order.get("status_order_id")
                        sname = order.get("status_order_name", "")
                        if sid not in seen_statuses:
                            seen_statuses[sid] = sname
                        status_info.append({
                            "order_id": order.get("id"),
                            "status_order_id": sid,
                            "status_order_name": sname,
                            "submitted_at": order.get("submitted_at", "")[:10],
                            "gross_total": order.get("gross_total"),
                            "shipping_option_name": order.get("shipping_option_name", ""),
                        })
                    results[label] = {
                        "distinct_statuses": seen_statuses,
                        "order_count": len(data),
                        "sample": status_info[:20],
                    }
                else:
                    results[label] = data
        except Exception as e:
            results[label] = {"error": str(e)}

    return results
