"""
migration_routes.py
FastAPI router for DB migrations and debug schema endpoints.
All endpoints require admin token (X-Admin-Token header).
"""

import os
from typing import List, Optional
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from auth import require_admin
from db_helpers import get_db
from config import B2BWAVE_URL

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
        add_quote_tracking_columns as _add_quote_tracking,
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


@migration_router.post("/add-quote-tracking-columns")
def add_quote_tracking_columns_endpoint(_: bool = Depends(require_admin)):
    return _run(_add_quote_tracking)


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
# WAREHOUSE MAPPING CLEANUP
# =============================================================================

class WarehouseMappingCleanup(BaseModel):
    prefixes: List[str]


@migration_router.post("/debug/warehouse-mapping-cleanup")
def warehouse_mapping_cleanup(
    body: WarehouseMappingCleanup,
    _: bool = Depends(require_admin),
    x_allow_destructive: Optional[str] = Header(None, alias="X-Allow-Destructive"),
):
    """
    Delete an explicit list of sku_prefix rows from warehouse_mapping.
    No wildcards — every prefix must be named. Requires `X-Allow-Destructive: yes`.
    Added 2026-07-14 for the SOT warehouse-map cleanup (stale name-keyed and
    retired-prefix rows); safe to reuse for future map maintenance.
    """
    if (x_allow_destructive or "").strip().lower() != "yes":
        return {"status": "error", "message": "X-Allow-Destructive: yes header required"}
    if not body.prefixes:
        return {"status": "error", "message": "prefixes list is empty"}

    deleted, not_found = [], []
    with get_db() as conn:
        with conn.cursor() as cur:
            for p in body.prefixes:
                cur.execute(
                    "DELETE FROM warehouse_mapping WHERE UPPER(sku_prefix) = UPPER(%s) RETURNING sku_prefix",
                    (p,),
                )
                row = cur.fetchone()
                (deleted if row else not_found).append(p)
        conn.commit()

    return {"status": "ok", "deleted_count": len(deleted), "deleted": deleted, "not_found": not_found}


# =============================================================================
# R+L READ-ONLY PASSTHROUGH (calibration research)
# =============================================================================

@migration_router.get("/debug/rl-get")
def debug_rl_get(path: str, _: bool = Depends(require_admin)):
    """
    Generic READ-ONLY passthrough to api.rlc.com. `path` = everything after
    https://api.rlc.com/ (caller URL-encodes). GET only; no mutations possible.
    Added 2026-07-15 for pallet-multiplier calibration / charged-vs-paid audit —
    lets calibration iterate on ShipmentTracing / DocumentRetrieval params
    without a redeploy per variant.
    """
    import urllib.error
    import urllib.request

    key = os.environ.get("RL_CARRIERS_API_KEY", "")
    if not key:
        return {"status": "error", "message": "RL_CARRIERS_API_KEY not configured"}
    if "://" in path or path.startswith("/") or ".." in path:
        return {"status": "error", "message": "path must be a relative api.rlc.com path"}
    try:
        req = urllib.request.Request(f"https://api.rlc.com/{path}")
        req.add_header("apiKey", key)
        with urllib.request.urlopen(req, timeout=60) as r:
            return {"http": r.status, "body": r.read().decode(errors="replace")[:100000]}
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode(errors="replace")[:2000]}
    except Exception as e:
        return {"error": str(e)}


class RLPostRequest(BaseModel):
    path: str
    body: dict


@migration_router.post("/debug/rl-post")
def debug_rl_post(req_in: RLPostRequest, _: bool = Depends(require_admin)):
    """
    Query-style POST passthrough to api.rlc.com for R+L services whose queries
    require POST bodies. WHITELISTED read-only-in-effect paths only — BOL,
    pickup and claim creation endpoints are NOT allowed. Added 2026-07-15 for
    pallet-multiplier calibration / charged-vs-paid audit.
    """
    import json as _json
    import urllib.error
    import urllib.request

    ALLOWED = {
        "ActivityHistory/ShipmentHistory",
        "DocumentRetrieval/GetDocumentTypes",
        "DocumentRetrieval",
        "ShipmentTracing",
        "PickupRequestHistory",
        "TransitTimes",
    }
    key = os.environ.get("RL_CARRIERS_API_KEY", "")
    if not key:
        return {"status": "error", "message": "RL_CARRIERS_API_KEY not configured"}
    if req_in.path not in ALLOWED:
        return {"status": "error", "message": f"path not in read-only whitelist: {sorted(ALLOWED)}"}
    try:
        data = _json.dumps(req_in.body).encode()
        req = urllib.request.Request(f"https://api.rlc.com/{req_in.path}", data=data, method="POST")
        req.add_header("apiKey", key)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=90) as r:
            return {"http": r.status, "body": r.read().decode(errors="replace")[:200000]}
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode(errors="replace")[:2000]}
    except Exception as e:
        return {"error": str(e)}
